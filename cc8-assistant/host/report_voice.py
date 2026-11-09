"""Explicit local report intents and one bounded, RAM-only follow-up snapshot."""

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
import re
import time
from typing import Callable, ContextManager
from dot_client import Cancelled, DotError
from work_reports import BEIJING, MAX_CARDS, ReportWindow
from report_time import ReportRange, ReportTimeError, parse_range, range_compound, window_label
from report_progress import (
    DeliveryPlan,
    DeliveryToken,
    ReportCapacityError,
    ReportProgress,
    SINCE_NOTES,
    key,
    version,
)
from result_provenance import validate as validate_provenance

PERIOD_NAMES = {
    '上次汇报后': 'since_last',
    '上次汇报之后': 'since_last',
    '自上次汇报以来': 'since_last',
    '从上次汇报到现在': 'since_last',
    '过去半小时': 'half_hour',
    '最近半小时': 'half_hour',
    '近半小时': 'half_hour',
    '过去两小时': 'two_hours',
    '最近两小时': 'two_hours',
    '近两小时': 'two_hours',
    '过去两个小时': 'two_hours',
    '最近两个小时': 'two_hours',
    '今天': 'today',
    '过去2小时': 'two_hours',
    '最近2小时': 'two_hours',
    '近2小时': 'two_hours',
    '过去2个小时': 'two_hours',
    '最近2个小时': 'two_hours',
    '过去30分钟': 'half_hour',
    '最近30分钟': 'half_hour',
    '过去三十分钟': 'half_hour',
    '最近三十分钟': 'half_hour',
}
PERIOD_LABELS = {
    'half_hour': '过去半小时',
    'two_hours': '过去两小时',
    'today': '今天',
    'since_last': '上次汇报后的新增及待补播结果',
}
PERIOD_PATTERN = '|'.join(PERIOD_NAMES)
PREFIX = r'(?:请)?(?:帮我)?'
REPORT = re.compile(
    PREFIX
    + r'(?:汇报|播报|总结|查看)(?:一下)?('
    + PERIOD_PATTERN
    + r')的?(?:工作|工作成果|工作结果|完成的工作|成果)(?:汇报|情况)?'
)
QUERY = re.compile(
    PREFIX
    + '('
    + PERIOD_PATTERN
    + r')(?:都)?(?:做了什么|完成了什么|做了哪些工作|完成了哪些工作|有哪些工作成果)'
)
COMPOUND = re.compile(
    '(?:' + REPORT.pattern + '|' + QUERY.pattern + r')[，,；;]*(?:然后|并且|并|再|接着|之后).*'
)
INDEX = r'([0-9]{1,3}|[零〇一二两三四五六七八九十百]{1,4})'
DETAIL = re.compile(
    PREFIX
    + r'(?:详细说说|详细介绍|展开|读一下|查看|说说|把|将)?(?:本次汇报的|这次汇报的|汇报的)?第'
    + INDEX
    + r'项(?:的?(?:详情|详细结果|完整结果|详细内容|内容|结果|情况))?(?:读给我听|读一下)?'
)
CONTEXT_ITEM = re.compile(
    PREFIX + r'(?:把|将|关于|对于)?(?:本次汇报的|这次汇报的|汇报的)?第' + INDEX + r'项'
)
SOURCE_ACTION = re.compile(
    r'继续|推进|接着做|执行|做完|重做|修改|修复|修一下|删除|取消|发送|发给|追加|提交|运行'
)
CONTINUE = re.compile(
    PREFIX
    + r'(?:(?:继续|推进|继续推进|继续做|接着做)(?:第'
    + INDEX
    + r'项)?|第'
    + INDEX
    + r'项(?:继续|继续做|继续推进)|下一步)'
)


@dataclass(frozen=True)
class ReportCommand:
    kind: str
    value: str | int | ReportRange = ''


def ordinal(text):
    if text.isascii() and text.isdigit():
        return int(text)
    digits = dict(zip('零〇一二两三四五六七八九', (0, 0, 1, 2, 2, 3, 4, 5, 6, 7, 8, 9)))
    if len(text) == 1 and text in digits:
        return digits[text]
    if re.fullmatch(r'[一二两三四五六七八九]?十[一二三四五六七八九]?', text):
        tens, ones = text.split('十')
        return digits.get(tens, 1) * 10 + digits.get(ones, 0)
    return 0


def report_command(text, *, followup=False):
    if not isinstance(text, str) or len(text) > 500:
        return None
    text = re.sub(r'^(?:大黄蜂|bumblebee)[，,：:\s]*', '', text.strip(), flags=re.I)
    text = ''.join(text.split()).rstrip('。.!！?？')
    if match := REPORT.fullmatch(text) or QUERY.fullmatch(text):
        return ReportCommand('report', PERIOD_NAMES[match[1]])
    if COMPOUND.fullmatch(text) or range_compound(text):
        return ReportCommand('continue')
    if value := parse_range(text):
        return ReportCommand('custom', value)
    if match := DETAIL.fullmatch(text):
        if followup or '汇报' in text:
            return ReportCommand('item', ordinal(match[1]))
    if followup and CONTINUE.fullmatch(text):
        return ReportCommand('continue')
    if followup or '汇报' in text:
        if re.fullmatch(
            PREFIX
            + r'(?:继续|推进|继续推进|继续做|接着做)?(?:刚才那个|刚才的|之前那个|之前的|那个)(?:项目|工作|任务).*',
            text,
        ):
            return ReportCommand('continue' if SOURCE_ACTION.search(text) else 'item_choice')
        if match := CONTEXT_ITEM.match(text):
            if SOURCE_ACTION.search(text[match.end() :]) or text.startswith(('把', '将')):
                return ReportCommand('continue')
            if re.search('第' + INDEX + '项', text[match.end() :]):
                return ReportCommand('item_choice')
            return ReportCommand('item', ordinal(match[1]))
    if re.fullmatch(
        PREFIX
        + r'(?:汇报|播报|总结|查看)(?:一下)?[^。？！]{0,40}(?:工作|工作汇报|工作成果|工作结果|成果)',
        text,
    ):
        return ReportCommand('period')
    return None


@dataclass(frozen=True)
class ReportVoiceHooks:
    lock: ContextManager[object]
    context: Callable[[], tuple]
    read: Callable


@dataclass(frozen=True)
class ReportReply:
    text: str
    spoken: tuple[str, ...]
    source_title: str = ''
    operation: str = 'clarification'
    items: int = 0
    delivery: tuple[DeliveryToken | None, ...] = ()
    provenance: dict | None = None


def report_source(source, turn_id: str | None = None):
    return validate_provenance(
        dict(
            version=1,
            provider='work_report',
            host='local',
            thread_id=source.source_id,
            turn_id=turn_id,
            ephemeral=False,
            original_source=dict(thread_id=source.source_id, title=source.source_title),
            files=[],
            files_truncated=False,
        )
    )


def item_text(item, number):
    evidence = item['evidence']
    completed = datetime.fromtimestamp(item['completed_at'], BEIJING).strftime('%m-%d %H:%M:%S')
    lines = [
        f'第{number}项 · 原答复摘录',
        '已记录完成时间（北京时间）：' + completed,
        item['excerpt'],
    ]
    if item.get('since_status') in SINCE_NOTES:
        lines.insert(1, SINCE_NOTES[item['since_status']])
    if item['excerpt_truncated']:
        lines.append('摘录已截短，本次没有读取完整原答复。')
    lines.append(
        f'终端成功／失败回执：{evidence["command_successes"]}／{evidence["command_failures"]}；'
        f'文件变更成功／失败回执：{evidence["file_change_successes"]}／{evidence["file_change_failures"]}；'
        f'待确认：{evidence["unconfirmed_tool_items"]}。'
    )
    if not item['evidence_complete']:
        lines.append('执行回执未读取完整。')
    lines.append('回合完成不代表任务已验证，以上原答复尚未独立核验。')
    return '\n'.join(lines)


def report_text(report):
    def date(value):
        return datetime.fromtimestamp(value, BEIJING).strftime('%Y-%m-%d %H:%M:%S')

    lines = [
        '所选聊天工作汇报 · 只读快照',
        '来源：' + report['source_title'],
        f'北京时间：{date(report["window_start"])} 至 {date(report["window_end"])}（含起点，不含终点）',
    ]
    since = report.get('view') == 'since_last_unheard'
    if since:
        lines.append(
            f'上次读取截止：{date(report["since_last_end"])}；重新检查原范围以收集迟到结果。'
            f'本次来源读到{report["source_completed_items_read"]}项，跳过{report["delivered_items_skipped"]}项已确认播完的摘要；'
            f'内存账本待播{report["pending_items"]}项，本页最多{MAX_CARDS}项。'
        )
    if not report['coverage_complete']:
        lines.append('覆盖不完整，不能据此断言没有其他成果。')
    lines.append(
        f'缺完成时间：{report["unknown_completion_time"]}；缺最终答复：{report["missing_final_answer"]}；'
        f'窗口内失败：{report["failed_turns"]}；中断：{report["interrupted_turns"]}；'
        f'读取时仍进行中：{report["active_turns_seen"]}。'
    )
    if report['empty_confirmed']:
        lines.append(
            '本次没有新增或待补播结果。' if since else '已读取范围内没有本窗口的已完成答复。'
        )
    lines.extend(item_text(item, n + 1) for n, item in enumerate(report['items']))
    return '\n\n'.join(lines)


class ReportVoice:
    def __init__(self, hooks, *, clock=time.monotonic, wall_clock=time.time):
        self.hooks, self.clock, self.wall_clock = hooks, clock, wall_clock
        self.snapshot = None
        self.followup = False
        self.revision = 0
        self.progress = self.delivery_plan = None

    def command(self, text):
        with self.hooks.lock:
            return report_command(text, followup=self.followup)

    def forget(self):
        with self.hooks.lock:
            self.snapshot, self.followup = None, False
            self.delivery_plan = None
            self.revision += 1

    def clear(self):
        """Source changes and shutdown erase both content and delivery state."""
        with self.hooks.lock:
            self.forget()
            self.progress = None

    def begin(self):
        with self.hooks.lock:
            self.snapshot, self.followup = None, True
            self.delivery_plan = None
            self.revision += 1
            return self.revision

    @staticmethod
    def valid_report(source, report):
        try:
            if (
                report['source_id'] != source.source_id
                or report['source_title'] != source.source_title
            ):
                return False
            ReportWindow(report['window_start'], report['window_end'])
            if not isinstance(report['items'], list) or len(report['items']) > MAX_CARDS:
                return False
            for name in ('coverage_complete', 'empty_confirmed'):
                if type(report[name]) is not bool:
                    return False
            if report['empty_confirmed'] != (report['coverage_complete'] and not report['items']):
                return False
            for name in (
                'unknown_completion_time',
                'missing_final_answer',
                'failed_turns',
                'interrupted_turns',
                'active_turns_seen',
            ):
                if type(report[name]) is not int or not 0 <= report[name] <= 1000:
                    return False
            seen = set()
            for item in report['items']:
                ident = key(item)
                if ident in seen:
                    return False
                seen.add(ident)
                if (
                    not isinstance(item['excerpt'], str)
                    or not 0 < len(item['excerpt']) <= 500
                    or item['verification'] != 'not_assessed'
                ):
                    return False
                if (
                    type(item['completed_at']) is not int
                    or not report['window_start'] <= item['completed_at'] < report['window_end']
                ):
                    return False
                if any(
                    type(item[k]) is not bool for k in ('excerpt_truncated', 'evidence_complete')
                ):
                    return False
                for name in (
                    'command_successes',
                    'command_failures',
                    'file_change_successes',
                    'file_change_failures',
                    'unconfirmed_tool_items',
                ):
                    if (
                        type(item['evidence'][name]) is not int
                        or not 0 <= item['evidence'][name] <= 100
                    ):
                        return False
                if item['evidence_complete'] and item['evidence']['unconfirmed_tool_items']:
                    return False
            if report['coverage_complete'] and (
                report['unknown_completion_time']
                or report['missing_final_answer']
                or any(not item['evidence_complete'] for item in report['items'])
            ):
                return False
            return True
        except (KeyError, TypeError, ValueError, AttributeError):
            return False

    def remember(self, source, report, expected_revision=None, progress=None):
        with self.hooks.lock:
            if expected_revision is not None and expected_revision != self.revision:
                return False
            context = self.hooks.context()
            if (
                context[0] is not source
                or context[1] is None
                or not self.valid_report(source, report)
            ):
                return False
            self.snapshot = (context, self.clock() + 600, deepcopy(report))
            self.progress = progress if progress is not None else ReportProgress(source, report)
            self.delivery_plan = None
            self.followup = True
            return True

    def confirm(self, token):
        with self.hooks.lock:
            if self.progress is not None and self.progress.source is not self.hooks.context()[0]:
                self.clear()
            if self.delivery_plan is not None and self.progress is not None:
                self.delivery_plan.confirm(token, self.progress)

    def delivery(self, messages, requirements):
        self.delivery_plan = DeliveryPlan(self.progress, requirements)
        return tuple(self.delivery_plan.token(index) for index in range(len(messages)))

    def resolve(self, command):
        if command.kind == 'custom':
            if not isinstance(command.value, ReportRange):
                raise ValueError('Invalid report range')
            return command.value.resolve(self.wall_clock())
        if command.value != 'since_last':
            return command.value
        if self.progress is None:
            raise ReportTimeError(
                '尚无本来源的汇报基线，请先明确查询“汇报今天的工作”或指定起止时间；重启或更换来源后需重新建立基线。'
            )
        now = self.wall_clock()
        if type(now) not in (int, float) or not self.progress.end < now < 253402300799:
            raise ReportTimeError(
                '当前时间尚未晚于上次读取截止，请检查Mac系统时间或稍后重试；未播记录保留。'
            )
        if int(now) - self.progress.start > 31 * 86400:
            raise ReportTimeError(
                '原汇报范围已超过31天，请先查询待播编号详情，再明确指定新的时间范围；未播记录保留。'
            )
        return ReportWindow(self.progress.start, int(now))

    @staticmethod
    def same_context(left, right):
        return left[0] is right[0] and left[1] is right[1] and left[2] == right[2]

    @staticmethod
    def notice(text):
        return ReportReply(text, (text,))

    def answer(self, command, is_current):
        if not is_current():
            raise Cancelled('Report cancelled')
        with self.hooks.lock:
            if self.progress is not None and self.progress.source is not self.hooks.context()[0]:
                self.clear()
        if command.kind == 'period':
            return self.notice(
                '请明确说“汇报过去半小时的工作”、“汇报今天的工作”、“汇报今天上午八点到上午十点的工作”或“汇报上次汇报后的工作”。'
            )
        if command.kind == 'continue':
            return self.notice(
                '当前支持只读工作汇报及编号详情，原聊天续办尚未接入；没有向原聊天追加请求，请分开提出查询和后续操作。'
            )
        if command.kind == 'item_choice':
            return self.notice(
                '请先指定本次汇报中的一个编号，例如“详细说说第二项”；当前不会推测或合并多个项目执行。'
            )
        if command.kind == 'item':
            with self.hooks.lock:
                context = self.hooks.context()
                if (
                    self.snapshot is None
                    or not self.same_context(self.snapshot[0], context)
                    or self.clock() >= self.snapshot[1]
                ):
                    self.snapshot = None
                    return self.notice('本次汇报尚未取得或已失效，请重新请求工作汇报。')
                report = self.snapshot[2]
                number = command.value
                if type(number) is not int or not 1 <= number <= len(report['items']):
                    return self.notice('这份汇报没有该编号，请按屏幕上本次汇报的编号查询。')
                text = item_text(report['items'][number - 1], number)
                item = report['items'][number - 1]
                evidence = item['evidence']
                messages = [f'第{number}项原答复摘录。', item['excerpt']]
                if item.get('since_status') in SINCE_NOTES:
                    messages.insert(1, SINCE_NOTES[item['since_status']])
                if item['excerpt_truncated']:
                    messages.append('摘录已截短，本次没有读取完整原答复。')
                messages.append(
                    f'终端成功{evidence["command_successes"]}条、失败{evidence["command_failures"]}条回执；文件变更成功{evidence["file_change_successes"]}条、失败{evidence["file_change_failures"]}条回执，待确认{evidence["unconfirmed_tool_items"]}条。'
                )
                if not item['evidence_complete']:
                    messages.append('执行回执未读取完整。')
                messages.append('回合完成不代表任务已验证，以上原答复尚未独立核验。')
                requirements = {(key(item), version(item)): set(range(len(messages)))}
                return ReportReply(
                    text,
                    tuple(messages),
                    report['source_title'],
                    'item',
                    1,
                    self.delivery(messages, requirements),
                    report_source(context[0], item['turn_id']),
                )
        if command.kind not in ('report', 'custom'):
            raise ValueError('Invalid report command')
        with self.hooks.lock:
            context = self.hooks.context()
            source = context[0]
            if source is None or context[1] is None:
                return self.notice('请先在设置的“Dot 聊天接续”中选择一个聊天，作为工作汇报来源。')
            try:
                window = self.resolve(command)
            except ReportTimeError as error:
                return self.notice(str(error))
            previous = self.snapshot
            revision = self.begin()
        report = self.hooks.read(source, window, is_current)
        with self.hooks.lock:
            if (
                not is_current()
                or self.revision != revision
                or not self.same_context(self.hooks.context(), context)
            ):
                raise Cancelled('Report cancelled')
            if not self.valid_report(source, report) or (
                isinstance(window, ReportWindow)
                and (report['window_start'], report['window_end']) != (window.start, window.end)
            ):
                raise DotError('Work report result is unconfirmed')
            since = command.value == 'since_last'
            progress = None
            if since:
                try:
                    progress, report = self.progress.merge(report)
                except ReportCapacityError:
                    if (
                        previous is not None
                        and self.clock() < previous[1]
                        and self.same_context(previous[0], context)
                    ):
                        self.snapshot = previous
                    return self.notice(
                        '汇报账本已达256项容量，原待播记录保留；请先查看待播编号详情，再明确查询新的时间范围。'
                    )
            if not self.remember(source, report, revision, progress):
                raise DotError('Work report result is unconfirmed')
            text = report_text(report)
            label = (
                window_label(window)
                if isinstance(window, ReportWindow) and not since
                else PERIOD_LABELS[command.value]
            )
            messages = [f'这是{label}所选聊天的只读汇报。原答复尚未独立核验。']
            if since:
                messages.append('重新检查原汇报范围，保留未确认播完的摘要，并收集迟到结果。')
            if not report['coverage_complete']:
                messages.append('读取覆盖不完整，不能据此判断其他成果。')
            if report['empty_confirmed']:
                messages.append(
                    '本次没有新增或待补播结果。'
                    if since
                    else '已读取范围内没有本窗口的已完成答复。'
                )
            elif not report['items']:
                messages.append('这次未取得可播报的完成答复，不能据此判断没有成果。')
            else:
                messages.append(
                    f'本次读到{len(report["items"])}项已完成答复。'
                    + ('先播报前三项。' if len(report['items']) > 3 else '')
                )
            requirements = {}
            for number, item in enumerate(report['items'][:3], 1):
                if item.get('since_status') in SINCE_NOTES:
                    messages.append(SINCE_NOTES[item['since_status']])
                indices = {len(messages)}
                if item.get('since_status') in SINCE_NOTES:
                    indices.add(len(messages) - 1)
                requirements[key(item), version(item)] = indices
                messages.append(
                    f'第{number}项，原答复摘录：'
                    + item['excerpt'][:80]
                    + (
                        '。摘录已截短。'
                        if item['excerpt_truncated'] or len(item['excerpt']) > 80
                        else ''
                    )
                )
            if report['failed_turns'] or report['interrupted_turns']:
                messages.append(
                    f'窗口内另有{report["failed_turns"]}个失败回合、{report["interrupted_turns"]}个中断回合。'
                )
            if report['active_turns_seen']:
                messages.append(
                    f'还读到{report["active_turns_seen"]}个仍进行中的回合，不计为完成成果。'
                )
            messages.append('本次结果卡可在最近 Dot 完整结果中查看；可按屏幕上的编号查询详情。')
            return ReportReply(
                text,
                tuple(messages),
                report['source_title'],
                'report',
                len(report['items']),
                self.delivery(messages, requirements),
                report_source(source),
            )
