"""Bounded literal scheduled excerpts; never ask a model to rewrite result claims."""

from datetime import datetime
from work_reports import BEIJING


def segments(source_title, items, *, partial, more):
    header = f'定时工作摘要，来源：{source_title}。以下为原答复摘录，尚未独立核验。'
    if partial:
        header += '本次读取覆盖不完整，不能断言没有其他成果。'
    if more:
        header += '还有已读取的新答复，将在之后的检查中继续处理。'
    values = [header]
    for number, item in enumerate(items, 1):
        time = datetime.fromtimestamp(item['completed_at'], BEIJING).strftime('%m月%d日%H点%M分')
        source = (
            f'来源：{item["source_title"]}，{item["source_project"]}。'
            if 'source_thread_id' in item
            else ''
        )
        evidence = item['evidence']
        values.append(
            f'本次定时摘要第{number}项。{source}已记录完成时间，北京时间{time}。'
            + item['excerpt']
            + ('。摘录已截短，没有读取完整原答复。' if item['excerpt_truncated'] else '')
            + f'。终端成功、失败回执：{evidence["command_successes"]}、{evidence["command_failures"]}；'
            + f'文件变更成功、失败回执：{evidence["file_change_successes"]}、{evidence["file_change_failures"]}；'
            + f'待确认：{evidence["unconfirmed_tool_items"]}。'
            + ('' if item['evidence_complete'] else '执行回执未读取完整。')
            + '原答复尚未独立核验。'
        )
    return tuple(values)
