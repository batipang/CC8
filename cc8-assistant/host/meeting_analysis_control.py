"""Passive meeting document admission, serial jobs and immutable result ownership."""

from dataclasses import dataclass, field
from typing import Callable, ContextManager

from meeting_analysis import MeetingAnalyzer, AnalysisRecord, Source, source
from meeting_protocol import MAX_REVISION, integer, token
from minimax_client import Cancelled
from reply_types import MeetingReply
from diagnostics import error_kind
from failure_feedback import REASONS


class AnalysisUnavailable(ValueError):
    """Fixed local guidance, never a provider exception or transcript excerpt."""


def owner_equal(first, second):
    return (
        first is not None
        and second is not None
        and first[0] is second[0]
        and first[1:] == second[1:]
    )


@dataclass(frozen=True)
class AnalysisHooks:
    lock: ContextManager
    model: Callable
    owner: Callable
    idle: Callable
    submit: Callable | None
    changed: Callable
    workspace: Callable | None = None
    publish: Callable = lambda: None
    diagnostics: Callable | None = None


@dataclass
class AnalysisJob:
    ident: str
    kind: str
    language: str
    owner: tuple = field(repr=False)
    source: Source = field(repr=False)
    generation: int
    state: str = 'queued'
    reason: str = ''
    message: str = ''
    client: object = field(default=None, repr=False)
    task: str | None = None


class MeetingAnalyses:
    def __init__(self, hooks, *, analyzer=None):
        self.hooks, self.analyzer = hooks, analyzer or MeetingAnalyzer()
        self.job = None
        self.records = {}

    def busy(self):
        return self.job is not None and self.job.state in ('queued', 'running')

    def digest(self):
        model = self.hooks.model()
        try:
            return source(model, model.ident, model.revision).sha256 if model else None
        except ValueError:
            return None

    def metadata(self):
        digest = self.digest() if self.records else None
        return dict(
            analyses=[record.metadata(digest) for record in self.records.values()],
            analysis_job=None
            if self.job is None
            else dict(
                id=self.job.ident,
                kind=self.job.kind,
                language=self.job.language,
                state=self.job.state,
                reason=self.job.reason,
                message=self.job.message,
                source_revision=self.job.source.revision,
            ),
        )

    def exported(self):
        digest = self.digest() if self.records else None
        return tuple(record.export(digest) for record in self.records.values())

    def current(self, job):
        with self.hooks.lock:
            model = self.hooks.model()
            return (
                self.job is job
                and job.state in ('queued', 'running')
                and owner_equal(job.owner, self.hooks.owner())
                and model is not None
                and model.ident == job.source.meeting
                and model.revision == job.source.revision
                and model.state == 'ended'
            )

    def submit(self, request, owner):
        if not self.hooks.idle() or self.busy() or self.hooks.submit is None:
            raise AnalysisUnavailable('请先结束会议、暂停日常语音，并等待当前任务和转写结束。')
        model = self.hooks.model()
        try:
            frozen = source(model, request['id'], request['revision'])
        except ValueError:
            raise AnalysisUnavailable(
                '会议文字尚未就绪或修订已变化，请结束转写、核对正文后重新选择。'
            ) from None
        kind = request['action']
        language = request.get('language', 'zh')
        label = (
            '会议总结'
            if kind == 'summarize'
            else ('会议翻译为中文' if language == 'zh' else '会议翻译为英文')
        )
        admitted = []

        def factory(generation):
            current = self.hooks.owner()
            if (
                current is None
                or not owner_equal(owner[:2], current[:2])
                or current[2] != generation
                or model is not self.hooks.model()
                or model.revision != frozen.revision
            ):
                raise ValueError('Meeting document owner changed before admission')
            job = AnalysisJob(request['request_id'], kind, language, current, frozen, generation)
            self.job = job
            admitted.append(job)
            return MeetingReply(generation, label, job)

        result = self.hooks.submit(label, factory)
        if not isinstance(result, dict) or result.get('ok') is not True or len(admitted) != 1:
            if admitted and self.job is admitted[0]:
                self.invalidate('cancelled')
            raise ValueError('Meeting document admission unconfirmed')
        return dict(analysis_id=admitted[0].ident)

    def invalidate(self, reason='cancelled'):
        with self.hooks.lock:
            if not self.busy():
                return
            job = self.job
            job.state = 'cancelled'
            job.reason = (
                reason if reason in ('source_changed', 'background', 'route_lost') else 'cancelled'
            )
            if self.hooks.workspace and job.task:
                self.hooks.workspace().task_finish(job.task, 'cancelled')
            if job.client is not None:
                try:
                    job.client.cancel()
                except Exception:
                    pass
            self.hooks.changed()

    def clear(self):
        with self.hooks.lock:
            self.invalidate()
            self.records.clear()
            self.job = None

    def cancel(self, ident):
        if self.job is None or self.job.ident != ident:
            raise ValueError('Meeting analysis changed')
        self.invalidate()

    def page(self, ident, offset):
        if not token(ident) or not integer(offset, 512 * 1024):
            raise ValueError('Invalid meeting analysis cursor')
        record = next((item for item in self.records.values() if item.ident == ident), None)
        if record is None or offset > len(record.text):
            raise ValueError('Meeting analysis unavailable')
        end = min(offset + 1024, len(record.text))
        return dict(
            record.metadata(self.digest()),
            offset=offset,
            text=record.text[offset:end],
            next_offset=end if end < len(record.text) else None,
        )

    def execute(self, reply):
        job = reply.run
        with self.hooks.lock:
            if not isinstance(job, AnalysisJob) or not self.current(job):
                return

        def set_client(client):
            with self.hooks.lock:
                if not self.current(job):
                    raise Cancelled('Meeting document superseded')
                job.client = client

        def accepted(record):
            with self.hooks.lock:
                if (
                    not self.current(job)
                    or not isinstance(record, AnalysisRecord)
                    or record.source is not job.source
                    or record.kind != job.kind
                    or record.language != job.language
                ):
                    return False
                model = self.hooks.model()
                if model.revision >= MAX_REVISION:
                    raise ValueError('Meeting revision exhausted')
                model.note_analysis()
                self.records[(record.kind, record.language)] = record
                job.state = 'completed'
                job.reason = ''
                if self.hooks.workspace and job.task:
                    self.hooks.workspace().task_finish(
                        job.task, 'completed', '会议文字处理已完成，请在会议页查看完整结果。'
                    )
                if self.hooks.diagnostics:
                    self.hooks.diagnostics().task_result(job.generation, 'completed')
                self.hooks.changed()
                return True

        try:
            with self.hooks.lock:
                if not self.current(job):
                    return
                job.state = 'running'
                if self.hooks.workspace:
                    job.task = self.hooks.workspace().task_start(job.generation, reply.text)
                self.hooks.changed()
            self.analyzer.run(
                job.source, job.kind, job.language, lambda: self.current(job), accepted, set_client
            )
            with self.hooks.lock:
                if job.state == 'running':
                    raise ValueError('Meeting document returned no final receipt')
        except Exception as error:
            with self.hooks.lock:
                if self.job is job and job.state in ('queued', 'running'):
                    job.state = 'cancelled' if isinstance(error, Cancelled) else 'failed'
                    job.reason = (
                        'cancelled' if isinstance(error, Cancelled) else 'processing_unconfirmed'
                    )
                    if job.state == 'failed':
                        kind = (
                            'provider_or_runtime'
                            if isinstance(error, ValueError)
                            else error_kind(error)
                        )
                        job.message = (
                            'Dot '
                            + REASONS.get(kind, REASONS['provider_or_runtime'])
                            + '，未取得完整文字结果。请检查服务后重新生成。'
                        )
                    if self.hooks.workspace and job.task:
                        self.hooks.workspace().task_finish(job.task, job.state)
                    if self.hooks.diagnostics and job.state == 'failed':
                        self.hooks.diagnostics().failure(job.generation, 'brain', error)
                    self.hooks.changed()
        finally:
            with self.hooks.lock:
                job.client = None
                if self.hooks.diagnostics:
                    self.hooks.diagnostics().finish(job.generation, job.state)
                try:
                    self.hooks.publish()
                except (OSError, EOFError):
                    pass
