"""Read/prepare a subscription notice without changing manual reports or starting media."""

from dataclasses import dataclass
from contextlib import nullcontext
from report_checkpoint import source_from
from report_progress import key, version
from report_voice import ReportVoice
from report_watch_store import MAX_NOTICE, MAX_SEEN, pair
from report_watch_narration import segments
from work_reports import ReportWindow
from dot_client import Cancelled


@dataclass(frozen=True)
class Notice:
    watch_id: str
    revision: int
    read_end: int
    pairs: tuple[tuple[tuple[str, ...], str], ...]
    known_new: int
    partial: bool
    text: str
    segments: tuple[str, ...] = ()


class WatchReader:
    """The caller owns foreground/idle/quiet admission and runs this outside its UI lock.

    Check cancellation and exact store revision between RPC pages through ``read``'s
    callback. The returned notice is a proposal, not a delivery or a send permission.
    """

    def __init__(self, store, read, source_current, *, commit_lock=None, results=None):
        self.store, self.read, self.source_current = store, read, source_current
        self.commit_lock = commit_lock if commit_lock is not None else nullcontext()
        self.results = results

    def clear_results(self):
        if self.results is not None:
            self.results.clear()

    def expire_results(self):
        if self.results is not None:
            self.results.expire()

    def prepare(self, read_end, current):
        state = self.store.snapshot()
        watch = state['watch']
        if watch is None or not watch['enabled'] or watch['attempt'] is not None:
            return None
        revision, ident = state['revision'], watch['id']
        source = source_from(watch['source'])

        def owned():
            if not current():
                return False
            live = self.store.snapshot()
            return (
                live['revision'] == revision
                and live['watch'] is not None
                and live['watch']['id'] == ident
                and live['watch']['enabled']
                and live['watch']['attempt'] is None
                and self.source_current(source)
            )

        def stop(status):
            # A removed source can be recorded as such without declaring it current.
            def unmodified():
                return current() and self.store.snapshot()['revision'] == revision

            # UI mutations take the owner lock before the store lock. Maintain
            # that order for read observations whose current callback uses it.
            with self.commit_lock:
                self.store.checked(revision, status, current=unmodified)
            return None

        if not current():
            raise Cancelled('Report subscription cancelled')
        with self.commit_lock:
            self.clear_results()
        previous = watch['last_check']
        if (
            type(read_end) is not int
            or read_end <= watch['started_at']
            or (previous is not None and read_end < previous['at'])
        ):
            return stop('clock_error')
        if read_end - watch['started_at'] > 31 * 86400:
            return stop('age_limit')
        if len(watch['seen']) == MAX_SEEN:
            return stop('capacity')
        if not self.source_current(source):
            return stop('source_changed')
        window = ReportWindow(watch['started_at'], read_end)
        try:
            report = self.read(source, window, owned)
        except Cancelled:
            raise
        except Exception:
            if not owned():
                raise Cancelled('Report subscription owner changed') from None
            return stop('read_error')
        if not owned():
            raise Cancelled('Report subscription owner changed')
        if not ReportVoice.valid_report(source, report) or (
            report['window_start'],
            report['window_end'],
        ) != (window.start, window.end):
            return stop('read_error')
        with self.commit_lock:
            if not owned():
                raise Cancelled('Report subscription owner changed')
            if self.results is not None:
                self.results.remember(report)
        seen = {pair(row, watch['source']) for row in watch['seen']}
        rows = [dict(key=list(key(item)), fingerprint=version(item)) for item in report['items']]
        unseen_items = [
            (row, item)
            for row, item in zip(rows, report['items'])
            if pair(row, watch['source']) not in seen
        ]
        unseen = [row for row, item in unseen_items]
        partial = not report['coverage_complete']
        if not unseen:
            with self.commit_lock:
                self.store.checked(
                    revision,
                    'partial' if partial else 'empty',
                    confirmed_new=None if partial else 0,
                    current=owned,
                )
            return None
        selected = unseen[:MAX_NOTICE]
        if len(watch['seen']) + len(selected) > MAX_SEEN:
            return stop('capacity')
        text = (
            f'工作进展提示：{source.source_title}，本次已读取的结果中发现{len(selected)}项尚未提示的完成答复。'
            + (
                '还有已读取的新增答复，将在之后的检查中继续提示。'
                if len(unseen) > MAX_NOTICE
                else ''
            )
            + ('本次读取覆盖不完整，不能据此断言没有其他成果。' if partial else '')
            + '原答复尚未独立核验，可明确查询工作汇报查看内容。'
        )
        return Notice(
            ident,
            revision,
            read_end,
            tuple(pair(row, watch['source']) for row in selected),
            len(unseen),
            partial,
            text,
            segments(
                source.source_title,
                [item for row, item in unseen_items[:MAX_NOTICE]],
                partial=partial,
                more=len(unseen) > MAX_NOTICE,
            )
            if watch.get('mode') == 'summary'
            else (),
        )

    def claim(self, notice, current):
        """Recheck the exact proposal before making the durable pre-send claim."""
        if not isinstance(notice, Notice):
            raise ValueError('Invalid work report notice')

        def owned():
            if not current():
                return False
            state = self.store.snapshot()
            watch = state['watch']
            return (
                state['revision'] == notice.revision
                and watch is not None
                and watch['id'] == notice.watch_id
                and watch['enabled']
                and watch['attempt'] is None
                and self.source_current(source_from(watch['source']))
            )

        with self.commit_lock:
            return self.store.claim(
                notice.revision,
                [dict(key=list(key), fingerprint=fingerprint) for key, fingerprint in notice.pairs],
                notice.read_end,
                current=owned,
            )
