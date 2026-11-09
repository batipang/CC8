"""Strict saved-report wire fields; deliberately independent of readers and providers."""

import re

REVISION_LIMIT = 2**31 - 1
ACTIONS = frozenset(
    ('saved_reports', 'saved_report', 'save_report', 'delete_report', 'clear_reports')
)
MUTATIONS = frozenset(('save_report', 'delete_report', 'clear_reports'))


def token(value: object, size: int = 32) -> str:
    if not isinstance(value, str) or not re.fullmatch('[a-f0-9]{' + str(size) + '}', value):
        raise ValueError('Invalid saved report ID')
    return value


def validate_request(value: object) -> None:
    if not isinstance(value, dict) or not isinstance(value.get('action'), str):
        raise ValueError('Invalid saved report request')
    action = value['action']
    fields = {'event', 'request_id', 'action'}
    if action not in ACTIONS:
        raise ValueError('Invalid saved report action')
    token(value.get('request_id'))
    if action in MUTATIONS:
        fields |= {'revision', 'confirmed'}
        if (
            type(value.get('revision')) is not int
            or not 0 <= value['revision'] < REVISION_LIMIT
            or value.get('confirmed') is not True
        ):
            raise ValueError('Explicit current saved report consent required')
    if action in ('saved_report', 'delete_report'):
        fields.add('saved_id')
        token(value.get('saved_id'))
    if action == 'save_report':
        fields.add('snapshot_id')
        token(value.get('snapshot_id'))
    if set(value) != fields or value.get('event') != 'dot_chat_request':
        raise ValueError('Invalid saved report request fields')
