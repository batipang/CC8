"""Read saved handoffs or open their original local Codex chat on the Mac."""

import argparse
import json
from pathlib import Path
import subprocess
import sys

from desktop_handoff import confirm_open, launch_chat
from saved_tasks import HANDOFF_DRAFT_LABEL, SavedTasks, desktop_handoff_source, ident

ROOT = Path(__file__).resolve().parents[1]


def request_open(thread_id):
    try:
        confirm_open(launch_chat(thread_id))
    except (OSError, ValueError, subprocess.TimeoutExpired):
        print('无法确认 Mac 已接收打开请求，请在桌面查找原聊天。', file=sys.stderr)
        return 2
    print('已请求桌面打开原聊天，请在窗口中核对后提交草稿。')
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('list', 'show', 'open'))
    parser.add_argument('id', nargs='?')
    parser.add_argument('--runtime-root', type=Path, default=ROOT)
    args = parser.parse_args(argv)
    if args.action == 'list':
        if args.id is not None:
            parser.error('list takes no saved task ID')
    elif args.id is None:
        parser.error(f'{args.action} requires one saved task ID')
    try:
        selected = ident(args.id) if args.id is not None else None
        store = SavedTasks(
            args.runtime_root / 'device-access/private/voice-runtime/saved-task-results.json'
        )
        records = []
        for item in reversed(store.snapshot()):
            source = desktop_handoff_source(item)
            if source is None:
                continue
            record = {
                'id': item['id'],
                'source_title': source['title'],
                'saved_at': item['saved_at'],
            }
            if selected == item['id']:
                if args.action == 'open':
                    return request_open(source['thread_id'])
                _, _, draft = item['result'].partition(HANDOFF_DRAFT_LABEL)
                record.update(
                    thread_id=source['thread_id'],
                    turn_id=item['provenance']['turn_id'],
                    next_step=draft or None,
                )
                print(json.dumps(record, ensure_ascii=False, indent=2))
                return 0
            records.append(record)
        if selected is None:
            print(json.dumps(records, ensure_ascii=False, indent=2))
            return 0
        print('未找到这条已保存的桌面接续记录。', file=sys.stderr)
    except (OSError, ValueError, TypeError, RecursionError):
        print('无法核对已保存的桌面接续记录。', file=sys.stderr)
    return 2
