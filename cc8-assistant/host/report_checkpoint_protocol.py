"""Wire consent for explicitly saved report delivery progress, never task execution."""

from report_archive_protocol import token, REVISION_LIMIT

ACTIONS = frozenset(
    (
        'progress_checkpoints',
        'progress_checkpoint',
        'save_progress',
        'restore_progress',
        'delete_progress',
        'clear_progress',
    )
)
MUTATIONS = frozenset(('save_progress', 'restore_progress', 'delete_progress', 'clear_progress'))


def validate_request(value: object) -> None:
    if not isinstance(value, dict) or not isinstance(value.get('action'), str):
        raise ValueError('Invalid report progress request')
    action = value['action']
    if action not in ACTIONS:
        raise ValueError('Invalid report progress action')
    fields = {'event', 'request_id', 'action'}
    token(value.get('request_id'))
    if action in MUTATIONS:
        fields |= {'revision', 'confirmed'}
        if (
            type(value.get('revision')) is not int
            or not 0 <= value['revision'] < REVISION_LIMIT
            or value.get('confirmed') is not True
        ):
            raise ValueError('Explicit current report progress consent required')
    if action in ('progress_checkpoint', 'restore_progress', 'delete_progress'):
        fields.add('checkpoint_id')
        token(value.get('checkpoint_id'))
    if action == 'save_progress':
        fields.add('offer_id')
        token(value.get('offer_id'))
    if set(value) != fields or value.get('event') != 'dot_chat_request':
        raise ValueError('Invalid report progress fields')
