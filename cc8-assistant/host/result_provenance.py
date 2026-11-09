"""Bounded structured tool receipts, never paths inferred from model prose."""

import json
import os
from typing import TypedDict, cast
from source_identity import identifier, title

MAX_FILES = 8
MAX_ITEMS = 32
MAX_BYTES = 8192


class FileReceipt(TypedDict):
    item_id: str
    path: str
    kind: str
    status: str
    move_to: str | None


class OriginalSource(TypedDict):
    thread_id: str
    title: str


class Provenance(TypedDict):
    version: int
    provider: str
    host: str
    thread_id: str
    turn_id: str | None
    ephemeral: bool
    original_source: OriginalSource | None
    files: list[FileReceipt]
    files_truncated: bool


def path_text(value: object) -> str:
    if not isinstance(value, str) or not value or any(ord(c) < 32 for c in value):
        raise ValueError('Invalid result path')
    if len(value.encode('utf-8')) > 512 or '://' in value:
        raise ValueError('Invalid result path')
    return value


def absolute_path(value: object, cwd: str | None) -> str:
    value = path_text(value)
    if not os.path.isabs(value):
        if not cwd or not os.path.isabs(cwd):
            raise ValueError('Unknown result working directory')
        value = os.path.join(cwd, value)
    return path_text(os.path.normpath(value))


def validate(value: object) -> Provenance:
    if not isinstance(value, dict) or set(value) != {
        'version',
        'provider',
        'host',
        'thread_id',
        'turn_id',
        'ephemeral',
        'original_source',
        'files',
        'files_truncated',
    }:
        raise ValueError('Invalid result provenance')
    if (
        type(value['version']) is not int
        or value['version'] != 1
        or value['provider'] not in ('dot', 'work_report')
        or value['host'] != 'local'
        or type(value['ephemeral']) is not bool
        or type(value['files_truncated']) is not bool
        or value['ephemeral'] != (value['provider'] == 'dot')
    ):
        raise ValueError('Invalid result provenance source')
    identifier(value['thread_id'])
    if value['provider'] == 'dot' or value['turn_id'] is not None:
        identifier(value['turn_id'])
    source = value['original_source']
    if source is not None:
        if not isinstance(source, dict) or set(source) != {'thread_id', 'title'}:
            raise ValueError('Invalid original result source')
        identifier(source['thread_id'])
        if not isinstance(source['title'], str) or title(source['title']) != source['title']:
            raise ValueError('Invalid original result title')
    files = value['files']
    if not isinstance(files, list) or len(files) > MAX_FILES:
        raise ValueError('Invalid result file count')
    if value['provider'] == 'work_report' and files:
        raise ValueError('Report file metadata is not connected')
    for item in files:
        if not isinstance(item, dict) or set(item) != {
            'item_id',
            'path',
            'kind',
            'status',
            'move_to',
        }:
            raise ValueError('Invalid result file receipt')
        identifier(item['item_id'])
        if item['kind'] not in ('add', 'update', 'delete') or item['status'] not in (
            'completed',
            'failed',
            'declined',
        ):
            raise ValueError('Invalid result file state')
        for field in ('path', 'move_to'):
            if field == 'move_to' and item[field] is None:
                continue
            path = path_text(item[field])
            if not os.path.isabs(path) or os.path.normpath(path) != path:
                raise ValueError('Invalid absolute result path')
        if item['move_to'] is not None and item['kind'] != 'update':
            raise ValueError('Invalid moved result file')
    if len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode()) > MAX_BYTES:
        raise ValueError('Result provenance exceeds limit')
    return cast(Provenance, value)
