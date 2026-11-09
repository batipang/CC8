"""Strict references and explicit consent for optional scheduled work notices."""

from report_archive_protocol import token, REVISION_LIMIT

ACTIONS = frozenset(('watch_state', 'configure_watch', 'pause_watch', 'clear_watch'))
MUTATIONS = ACTIONS - {'watch_state'}


def validate_request(value: object) -> None:
    if not isinstance(value, dict) or not isinstance(value.get('action'), str):
        raise ValueError('Invalid report subscription request')
    action = value['action']
    if action not in ACTIONS:
        raise ValueError('Invalid report subscription action')
    fields = {'event', 'request_id', 'action'}
    token(value.get('request_id'))
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
        token(value.get('offer_id'))
        if (
            type(value.get('period_seconds')) is not int
            or value['period_seconds'] not in (300, 900, 1800)
            or value.get('channel') not in ('cc8', 'desktop')
        ):
            raise ValueError('Invalid report notice period or channel')
    if value.get('event') != 'dot_chat_request' or set(value) != fields:
        raise ValueError('Invalid report subscription fields')
