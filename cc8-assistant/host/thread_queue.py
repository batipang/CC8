"""Bounded original-thread queue reads; no enqueue, resume, start or task-state inference."""

from copy import deepcopy
from datetime import datetime
import time

from dot_client import Cancelled, DotError
from report_sources import metadata
from source_identity import identifier
from work_reports import BEIJING

PAGE_SIZE = 8
MAX_PAGES = 4
MAX_QUEUE_ITEMS = PAGE_SIZE * MAX_PAGES
MAX_PREVIEW = 160


def queue_item(value):
    if not isinstance(value, dict):
        raise ValueError('Invalid source queue entry')
    ident = identifier(value.get('id'))
    client = identifier(value.get('clientUserMessageId'))
    inputs = value.get('input')
    if not isinstance(inputs, list) or len(inputs) > 64:
        raise ValueError('Invalid source queue input')
    pieces, other, truncated, remaining = [], False, False, MAX_PREVIEW
    for part in inputs:
        if not isinstance(part, dict):
            raise ValueError('Invalid source queue input entry')
        if part.get('type') != 'text':
            other = True
            continue
        text = part.get('text')
        if not isinstance(text, str):
            raise ValueError('Invalid source queue text')
        if len(text) > remaining:
            truncated = True
        # Bound before sanitizing; neither attachment paths nor extra fields
        # survive projection, and malformed Unicode cannot poison transport.
        excerpt = text[:remaining].encode('utf-8', 'replace').decode('utf-8')
        pieces.append(''.join(c if ord(c) >= 32 else ' ' for c in excerpt))
        remaining = max(0, remaining - len(excerpt))
    preview = ' '.join(' '.join(pieces).split())
    if len(preview) > MAX_PREVIEW:
        truncated = True
    preview = preview[: MAX_PREVIEW - 1] + '…' if truncated else preview[:MAX_PREVIEW]
    return dict(
        id=ident,
        client_message_id=client,
        preview=preview,
        preview_truncated=truncated,
        other_input_not_read=other,
    )


class ThreadQueueReader:
    def __init__(self, rpc_factory, *, clock=time.monotonic, wall_clock=time.time):
        self.rpc_factory, self.clock, self.wall_clock = rpc_factory, clock, wall_clock

    def read(self, source, *, is_current=lambda: True):
        source = deepcopy(metadata(source))
        result = dict(
            source=source,
            read_at=int(self.wall_clock()),
            items=[],
            coverage_complete=False,
            state='unavailable',
            reason='unavailable',
        )
        deadline = self.clock() + 25
        rpc = None

        def current():
            if not is_current():
                raise Cancelled('Source queue read cancelled')

        try:
            current()
            rpc = self.rpc_factory()
            remaining = deadline - self.clock()
            if remaining <= 0:
                raise DotError('Source queue read deadline exceeded', kind='timeout')
            rpc.start(timeout=min(20, remaining))
            cursor, cursors, seen = None, set(), set()
            for _ in range(MAX_PAGES):
                current()
                remaining = deadline - self.clock()
                if remaining <= 0:
                    result['reason'] = 'deadline'
                    break
                params = dict(threadId=source['thread_id'], limit=PAGE_SIZE)
                if cursor is not None:
                    params['cursor'] = cursor
                page = rpc.request('thread/queue/list', params, timeout=min(8, remaining))
                current()
                if self.clock() >= deadline:
                    result['reason'] = 'deadline'
                    break
                if (
                    not isinstance(page, dict)
                    or not isinstance(page.get('data'), list)
                    or len(page['data']) > PAGE_SIZE
                ):
                    raise ValueError('Invalid source queue page')
                rows = [queue_item(row) for row in page['data']]
                ids = {row['id'] for row in rows}
                if len(ids) != len(rows) or ids & seen:
                    raise ValueError('Source queue changed across pages')
                next_cursor = page.get('nextCursor')
                if next_cursor is not None and (
                    not isinstance(next_cursor, str) or not 0 < len(next_cursor) <= 2048
                ):
                    raise ValueError('Invalid source queue cursor')
                result['items'].extend(rows)
                seen |= ids
                if next_cursor is None:
                    result.update(coverage_complete=True, state='read', reason='page_end')
                    break
                if next_cursor in cursors:
                    result['reason'] = 'cursor_cycle'
                    break
                cursors.add(next_cursor)
                cursor = next_cursor
                result['reason'] = 'page_limit'
            current()
        except Cancelled:
            raise
        except (DotError, OSError, ValueError, KeyError, TypeError):
            result['reason'] = 'deadline' if self.clock() >= deadline else 'unavailable'
        finally:
            if rpc is not None:
                rpc.close()  # This read owns only its short-lived client.
        current()
        if not result['coverage_complete'] and result['items']:
            result['state'] = 'partial'
        return result


def queue_text(result):
    source = result['source']
    lines = [
        '原聊天待办队列 · 只读',
        f'来源：{source["title"]} · {source["project"]} · 本机 Codex',
        '原聊天：' + source['thread_id'],
        '读取时间（北京时间）：'
        + datetime.fromtimestamp(result['read_at'], BEIJING).strftime('%Y-%m-%d %H:%M:%S'),
        '队列分页期间可能变化；这不是原任务空闲、已开始或已完成的证明。',
    ]
    if result['state'] == 'unavailable':
        lines.append('本次未能确认队列，不能当作零条待办；请稍后明确重新查询。')
    elif result['coverage_complete']:
        lines.append(
            f'本次已读队列条目：{len(result["items"])}。'
            if result['items']
            else '本次读到空队列；不代表原任务已经结束或可以安全重启。'
        )
    else:
        lines.append(f'至少读到{len(result["items"])}条，覆盖不完整，不能判断其他待办。')
    for number, item in enumerate(result['items'], 1):
        lines.extend(
            [f'{number}. ' + (item['preview'] or '无可展示文字'), '队列编号：' + item['id']]
        )
        if item['preview_truncated']:
            lines.append('文字预览已截短。')
        if item['other_input_not_read']:
            lines.append('含非文字输入；本次没有读取附件。')
    lines.append('本次只读取队列，不发送、启动、恢复、删除或重排请求；不保存队列正文。')
    return '\n\n'.join(lines)
