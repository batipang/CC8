"""Read explicitly saved work-report handoffs on the Mac."""

import argparse
import json
from pathlib import Path
import sys

from saved_tasks import HANDOFF_DRAFT_LABEL, SavedTasks, desktop_handoff_pending, ident

ROOT = Path(__file__).resolve().parents[1]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('list', 'show'))
    parser.add_argument('id', nargs='?')
    parser.add_argument('--runtime-root', type=Path, default=ROOT)
    args = parser.parse_args(argv)
    if (args.action == 'show') != (args.id is not None):
        parser.error('show requires one saved task ID')
    try:
        selected = ident(args.id) if args.id is not None else None
        store = SavedTasks(
            args.runtime_root / 'device-access/private/voice-runtime/saved-task-results.json'
        )
        records = []
        for item in reversed(store.snapshot()):
            if not desktop_handoff_pending(item):
                continue
            provenance = item['provenance']
            source = provenance['original_source']
            if (
                source is None
                or source['thread_id'] != provenance['thread_id']
                or provenance['turn_id'] is None
            ):
                continue
            record = {
                'id': item['id'],
                'source_title': source['title'],
                'saved_at': item['saved_at'],
            }
            if selected == item['id']:
                _, _, draft = item['result'].partition(HANDOFF_DRAFT_LABEL)
                record.update(
                    thread_id=source['thread_id'],
                    turn_id=provenance['turn_id'],
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
