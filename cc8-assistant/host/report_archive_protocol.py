"""Strict saved-report wire fields; deliberately independent of readers and providers."""

import re
from report_display_protocol import page_number

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
    if action == 'saved_report' and ('page' in value or 'revision' in value):
        fields |= {'page', 'revision'}
        page_number(value.get('page'))
        if type(value.get('revision')) is not int or not 0 <= value['revision'] < REVISION_LIMIT:
            raise ValueError('Current saved report revision required for paging')
    if action == 'save_report':
        fields.add('snapshot_id')
        token(value.get('snapshot_id'))
        if 'include_receipts' in value:
            fields.add('include_receipts')
            if value['include_receipts'] is not True:
                raise ValueError('Invalid saved execution receipt consent')
    if set(value) != fields or value.get('event') != 'dot_chat_request':
        raise ValueError('Invalid saved report request fields')
