"""Consent and ownership for delivery checkpoint UI; no RPC, media or task launch."""

from copy import deepcopy
import time
import uuid

from report_checkpoint import payload, record, restore, matches
from report_checkpoint_protocol import MUTATIONS


class CheckpointController:
    def __init__(self, chat, store, *, clock=time.monotonic):
        self.chat, self.store, self.clock = chat, store, clock
        self.offer = None

    def context(self):
        chat = self.chat
        return (
            chat.hooks.generation(),
            chat.hooks.connection(),
            chat.hooks.transport_revision(),
            chat.voice.revision,
        )

    def eligible(self):
        chat = self.chat
        return (
            chat.hooks.capture_stopped()
            and chat.hooks.idle()
            and chat.hooks.connection() is not None
            and chat.voice.progress is not None
            and chat.voice.progress.source is chat.report_context_source()
        )

    def offered_record(self, ident):
        offer = self.offer
        if (
            offer is None
            or offer['id'] != ident
            or self.clock() >= offer['deadline']
            or offer['context'] != self.context()
            or not self.eligible()
            or offer['progress'] is not self.chat.voice.progress
            or payload(self.chat.voice.progress) != offer['payload']
        ):
            raise ValueError('Report progress offer changed')
        return deepcopy(offer['record'])

    def run(self, action, data, current):
        if self.store is None:
            raise ValueError('Report progress storage unavailable')
        chat = self.chat
        if action in MUTATIONS and (
            not current() or not chat.hooks.capture_stopped() or not chat.hooks.idle()
        ):
            raise ValueError('Pause voice before changing report progress')
        if action == 'save_progress':
            item = self.offered_record(data['offer_id'])
            item['saved_at'] = int(time.time())

            def save_current():
                if not current():
                    return False
                self.offered_record(data['offer_id'])
                return True

            state = self.store.apply('save', data['revision'], item=item, current=save_current)
            self.offer = None
        elif action == 'restore_progress':
            with self.store.selected(data['checkpoint_id'], data['revision'], current) as item:
                source = chat.report_context_source()
                progress = restore(item, source)
                if (
                    not current()
                    or not chat.hooks.capture_stopped()
                    or not chat.hooks.idle()
                    or source is not chat.report_context_source()
                ):
                    raise ValueError('Report progress restore owner changed')
                chat.voice.forget()
                chat.voice.progress = progress
                self.offer = None
            return dict(
                progress_restored=True,
                progress_message='已恢复所选来源的补播基线和已播记录。没有读取原聊天、播报或执行任务；可明确请求“汇报上次汇报后的工作”。',
            )
        elif action in ('delete_progress', 'clear_progress'):
            state = self.store.apply(
                'delete' if action == 'delete_progress' else 'clear',
                data['revision'],
                ident=data.get('checkpoint_id'),
                current=current,
            )
        else:
            state = self.store.snapshot()
        result = dict(progress_checkpoints=self.store.listing(state))
        if action == 'progress_checkpoint':
            item = next((r for r in state['items'] if r['id'] == data['checkpoint_id']), None)
            if item is None:
                raise ValueError('Saved report progress no longer exists')
            result.update(
                checkpoint_id=item['id'],
                progress_text=description(item),
                can_restore=matches(chat.report_context_source(), item['source']),
            )
        if action == 'progress_checkpoints':
            self.offer = None
            if self.eligible():
                try:
                    item = record(chat.voice.progress)
                except (ValueError, KeyError, TypeError):
                    # An unusable live ledger must not hide previously saved
                    # records or prevent their explicit deletion.
                    return result
                self.offer = dict(
                    id=uuid.uuid4().hex,
                    deadline=self.clock() + 120,
                    context=self.context(),
                    progress=chat.voice.progress,
                    payload=payload(chat.voice.progress),
                    record=item,
                )
                result.update(
                    offer_id=self.offer['id'], current_progress_text=description(item, preview=True)
                )
        return result


def description(item, *, preview=False):
    from datetime import datetime
    from work_reports import BEIJING

    show = lambda t: datetime.fromtimestamp(t, BEIJING).strftime('%Y-%m-%d %H:%M:%S')
    delivered = sum(e['delivered'] for e in item['entries'])
    members = item['source']['members']
    origins = (
        '\n'.join(m['title'] + ' · ' + m['project'] + ' · ' + m['thread_id'] for m in members)
        if members
        else item['source']['source_id']
    )
    return (
        '汇报补播进度 · 非实时任务状态\n来源：'
        + item['source']['source_title']
        + '\n'
        + origins
        + ('\n核对时间（北京时间）：' if preview else '\n保存时间（北京时间）：')
        + show(item['saved_at'])
        + '\n基线起点：'
        + show(item['start'])
        + '\n上次读取截止：'
        + show(item['end'])
        + f'\n共{len(item["entries"])}项，设备已确认摘要播完{delivered}项，待补播{len(item["entries"]) - delivered}项。'
        + '\n保存基线、结果标识、摘要与必要回执；最多8份、每份256项、总计256KiB，保留至手动删除，不自动淘汰。'
        + '\n不保存完整聊天、录音、图片或原始工具输出。'
        + '\n只有明确确认才保存或恢复；启动不自动恢复。恢复会替换当前补播进度并清除旧语音编号，保留当前来源；不读原聊天、不自动播报、不执行或取消任务。恢复后需重新明确查询，旧摘要若未重新读取会标明未核验。'
    )
