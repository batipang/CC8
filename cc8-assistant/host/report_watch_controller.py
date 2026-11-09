"""Source-bound consent and truthful status for optional work notifications."""

import time
import uuid
from report_checkpoint import describe, source_from
from report_watch_results import WatchResults
from report_display import paged_text
from report_voice import report_text
from report_watch_reader import WatchReader
from report_watch_scheduler import WatchScheduler
from report_watch_protocol import MUTATIONS

STATUS = {
    'empty': '本次读取覆盖内没有尚未提示的完成答复',
    'partial': '读取覆盖不完整，不能断言没有新成果',
    'read_error': '未确认读取结果，未当作零成果',
    'source_changed': '来源已变化，提示已暂停',
    'capacity': '已提示版本达到容量，提示已暂停',
    'clock_error': '时间异常，提示已暂停',
    'age_limit': '范围超过31天，提示已暂停',
    'claimed': '已认领通知，尚未确认送达',
    'delivered': '所选渠道已确认提示；不表示摘要已播完或原工作已验证',
    'delivery_unconfirmed': '送达未确认，提示已暂停，不自动重发',
}


class WatchController:
    def __init__(self, chat, store, *, clock=time.monotonic):
        self.chat, self.store, self.clock = chat, store, clock
        self.offer = self.scheduler = None
        self.results = WatchResults(self.result_context, clock=clock)

    def result_context(self):
        scheduler = self.scheduler
        if scheduler is None or not scheduler.owner_current(scheduler.grant):
            return None
        watch = self.store.snapshot()['watch']
        if (
            watch is None
            or watch['id'] != scheduler.grant[1]
            or not self.chat.scope_current(source_from(watch['source']))
        ):
            return None
        connection, route = scheduler.grant[0]
        return (connection, route, watch['id'], watch['source'])

    def bind(self, hooks):
        reader = WatchReader(
            self.store,
            self.chat.read_subscription,
            self.chat.scope_current,
            commit_lock=self.chat.hooks.lock,
            results=self.results,
        )
        self.scheduler = WatchScheduler(self.store, reader, hooks)

    def context(self):
        hooks = self.chat.hooks
        return (
            hooks.generation(),
            hooks.connection(),
            hooks.transport_revision(),
            self.chat.voice.revision,
        )

    def offered(self, ident, revision):
        offer = self.offer
        if (
            offer is None
            or offer['id'] != ident
            or offer['revision'] != revision
            or offer['context'] != self.context()
            or self.clock() >= offer['deadline']
            or offer['source'] is not self.chat.report_context_source()
            or not self.chat.scope_current(offer['source'])
        ):
            raise ValueError('Report subscription source offer changed')
        return offer['source']

    def run(self, action, data, current):
        if self.store is None or self.scheduler is None:
            raise ValueError('Report subscriptions unavailable')
        chat = self.chat
        if not current():
            raise ValueError('Report subscription request changed')
        if action in ('watch_result', 'watch_result_page'):
            report = self.results.read(data.get('snapshot_id'))
            fields = dict(
                watch_result=True,
                snapshot_id=report['snapshot_id'],
                watch_result_ttl_ms=self.results.remaining_ms(),
            )
            return paged_text(
                '最近定时检查 · 只读快照，非实时。可能含已提示结果；不改变手动汇报编号和补播进度。\n\n'
                + report_text(report),
                dict(request_id=data['request_id'], ok=True, action=action, **chat.state()),
                page=data.get('page', 0),
                fields=fields,
            )
        if action in MUTATIONS and (
            not current() or not chat.hooks.capture_stopped() or not chat.hooks.idle()
        ):
            raise ValueError('Pause voice before changing subscriptions')
        if action == 'configure_watch':
            if data.get('mode') == 'summary' and self.scheduler.hooks.send_summary is None:
                raise ValueError('Scheduled summary delivery unavailable')
            source = self.offered(data['offer_id'], data['revision'])

            def confirmed():
                return current() and source is self.offered(data['offer_id'], data['revision'])

            state = self.store.configure(
                data['revision'],
                describe(source),
                data['period_seconds'],
                data['channel'],
                mode=data.get('mode', 'count'),
                current=confirmed,
            )
            self.offer = None
            self.results.clear()
            self.scheduler.arm()
        elif action in ('pause_watch', 'clear_watch'):
            change = self.store.pause if action == 'pause_watch' else self.store.clear
            state = change(data['revision'], current=current)
            self.scheduler.revoke()
        else:
            state = self.store.snapshot()
        result = dict(
            report_watch=self.summary(state),
            watch_text=self.description(state),
            watch_has_results=self.results.available(),
            watch_summary_supported=self.scheduler.hooks.send_summary is not None,
        )
        self.offer = None
        source = chat.report_context_source()
        if source is not None and chat.hooks.capture_stopped() and chat.hooks.idle():
            self.offer = dict(
                id=uuid.uuid4().hex,
                revision=state['revision'],
                context=self.context(),
                deadline=self.clock() + 120,
                source=source,
            )
            result.update(
                watch_offer_id=self.offer['id'], watch_offer_text=source_description(source)
            )
        return result

    def summary(self, state):
        watch = state['watch']
        return dict(
            schema=1,
            revision=state['revision'],
            configured=watch is not None,
            enabled=watch is not None and watch['enabled'],
        )

    def description(self, state):
        watch = state['watch']
        if watch is None:
            return '定时工作进展提示默认关闭。请先暂停语音，明确选择工作汇报来源，再确认周期和渠道。没有自动读取聊天、采集或通知。'
        status = '已启用，等待下次允许检查' if watch['enabled'] else '已暂停'
        if watch['last_check'] is not None:
            status += '\n' + (
                '本批摘要已按逐项设备回执确认播完，不表示原工作已验证'
                if watch.get('mode') == 'summary' and watch['last_check']['status'] == 'delivered'
                else STATUS.get(watch['last_check']['status'], '等待读取')
            )
        if watch['attempt'] is not None:
            pending = self.scheduler.pending
            status += (
                '\n正在等待渠道送达回执'
                if pending is not None and pending[:2] == (watch['id'], watch['attempt']['id'])
                else '\n遗留送达未确认，不自动重发；可明确重新设置'
            )
            if watch['attempt']['detail'] == 'not_authorized':
                status += '\n桌面通知尚未获得系统授权；本功能没有自动请求权限'
        if watch['enabled'] and not self.scheduler.owner_current(self.scheduler.grant):
            status += '\n当前没有前台准入，正常开始语音后才恢复检查；重连本身不启用'
        return (
            f'来源：{watch["source"]["source_title"]}\n每{watch["period_seconds"] // 60}分钟检查 · '
            + ('桌面通知' if watch['channel'] == 'desktop' else 'CC8播报')
            + (' · 原答复摘要' if watch.get('mode') == 'summary' else ' · 只提示数量')
            + f'\n已确认提示{len(watch["seen"])}/256个结果版本\n'
            + status
            + (
                '\n摘要只按逐项播完确认记账，打断保留未确认项，不自动重播；不表示工作已验证。'
                if watch.get('mode') == 'summary'
                else '\n通知索引不表示摘要播完。'
            )
            + '不改变手动补播账本或原工作。保留至手动清除，不保存结果正文。'
        )


def source_description(source):
    metadata = describe(source)
    members = metadata['members']
    return (
        '来源：'
        + source.source_title
        + '\n'
        + (
            '\n'.join(m['title'] + ' · ' + m['project'] + ' · ' + m['thread_id'] for m in members)
            if members
            else metadata['source_id']
        )
    )
