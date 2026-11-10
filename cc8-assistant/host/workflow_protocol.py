"""Strict authenticated device requests; a saved camera step is never consent."""

from workflow_definition import definition, integer, token, REVISION_LIMIT


def validate_request(value: object) -> None:
    if not isinstance(value, dict) or value.get('event') != 'workflow_request':
        raise ValueError('Invalid workflow request')
    token(value.get('request_id'))
    action = value.get('action')
    if not isinstance(action, str):
        raise ValueError('Invalid workflow action')
    base = {'event', 'request_id', 'action'}
    fields = {
        'list': set(),
        'get': {'id'},
        'put': {'revision', 'value', 'confirmed'},
        'delete': {'revision', 'id', 'confirmed'},
        'reset': {'revision', 'confirmed'},
        'clear_history': {'revision', 'confirmed'},
        'prepare': {'revision', 'id'},
        'run': {'offer_id', 'confirmed', 'camera_nonce'},
        'cancel': {'run_id'},
        'result': {'run_id', 'step_id'},
    }
    if action not in fields or set(value) != base | fields[action]:
        raise ValueError('Unexpected workflow request fields')
    for key in ('id', 'offer_id', 'run_id', 'step_id'):
        if key in value:
            token(value[key])
    if 'camera_nonce' in value and value['camera_nonce'] != '':
        token(value['camera_nonce'])
    if 'revision' in value:
        integer(value['revision'], 0, REVISION_LIMIT - 1)
    if 'confirmed' in value and value['confirmed'] is not True:
        raise ValueError('Explicit workflow confirmation required')
    if action == 'put':
        definition(value['value'])
