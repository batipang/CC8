"""Strict references and explicit consent for optional scheduled work notices."""

from report_archive_protocol import token, REVISION_LIMIT
from report_display_protocol import page_number

ACTIONS = frozenset(
    (
        'watch_state',
        'watch_result',
        'watch_result_page',
        'configure_watch',
        'pause_watch',
        'clear_watch',
    )
)
MUTATIONS = frozenset(('configure_watch', 'pause_watch', 'clear_watch'))


def validate_request(value: object) -> None:
    if not isinstance(value, dict) or not isinstance(value.get('action'), str):
        raise ValueError('Invalid report subscription request')
    action = value['action']
    if action not in ACTIONS:
        raise ValueError('Invalid report subscription action')
    fields = {'event', 'request_id', 'action'}
    token(value.get('request_id'))
    if action == 'watch_result_page':
        fields |= {'snapshot_id', 'page'}
        token(value.get('snapshot_id'))
        page_number(value.get('page'))
    if action in MUTATIONS:
        fields |= {'revision', 'confirmed'}
        if (
            type(value.get('revision')) is not int
            or not 0 <= value['revision'] < REVISION_LIMIT
            or value.get('confirmed') is not True
        ):
            raise ValueError('Explicit current subscription consent required')
    if action == 'configure_watch':
        fields |= {'offer_id', 'period_seconds', 'channel'}
        if 'mode' in value:
            fields.add('mode')
        token(value.get('offer_id'))
        if (
            type(value.get('period_seconds')) is not int
            or value['period_seconds'] not in (300, 900, 1800)
            or value.get('channel') not in ('cc8', 'desktop')
            or value.get('mode', 'count') not in ('count', 'summary')
            or value.get('mode') == 'summary'
            and value.get('channel') != 'cc8'
        ):
            raise ValueError('Invalid report notice period or channel')
    if value.get('event') != 'dot_chat_request' or set(value) != fields:
        raise ValueError('Invalid report subscription fields')
