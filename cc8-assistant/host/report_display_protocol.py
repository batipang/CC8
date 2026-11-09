"""Only bounded page references travel from the device, never report bodies."""

import re


def page_number(value: object) -> None:
    if type(value) is not int or not 0 <= value < 16:
        raise ValueError('Invalid report page')


def validate_request(value: object) -> None:
    if (
        not isinstance(value, dict)
        or set(value) != {'event', 'request_id', 'action', 'snapshot_id', 'page'}
        or value.get('event') != 'dot_chat_request'
        or value.get('action') != 'report_page'
        or not isinstance(value.get('request_id'), str)
        or not re.fullmatch('[a-f0-9]{32}', value['request_id'])
        or not isinstance(value.get('snapshot_id'), str)
        or not re.fullmatch('[a-f0-9]{32}', value['snapshot_id'])
    ):
        raise ValueError('Invalid report page request')
    page_number(value.get('page'))
