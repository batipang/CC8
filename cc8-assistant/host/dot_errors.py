"""Allowlisted app-server error metadata; free-form provider text is discarded."""

GENERIC = 'provider_or_runtime'
NAMED_KINDS = {
    'rateLimitExceeded': 'rate_limit',
    'usageLimitExceeded': 'quota',
    'unauthorized': 'credential_or_permission',
}
HTTP_VARIANTS = frozenset(
    (
        'httpConnectionFailed',
        'responseStreamConnectionFailed',
        'responseStreamDisconnected',
        'responseTooManyFailedAttempts',
    )
)
HTTP_KINDS = {
    401: 'credential_or_permission',
    403: 'credential_or_permission',
    402: 'quota',
    408: 'timeout',
    504: 'timeout',
}

# HTTP 429 alone can mean a rate limit or exhausted credits. Only an
# explicit named codexErrorInfo distinguishes those causes reliably.


def safe_info(value):
    if type(value) is str:
        return value if value in NAMED_KINDS else None
    if type(value) is not dict or len(value) != 1:
        return None
    variant = next(iter(value))
    if variant not in HTTP_VARIANTS or type(value[variant]) is not dict:
        return None
    status = value[variant].get('httpStatusCode')
    if status is not None and (type(status) is not int or not 100 <= status <= 599):
        return None
    return {variant: {'httpStatusCode': status}}


def error_kind(error):
    info = safe_info(error.get('codexErrorInfo')) if type(error) is dict else None
    if type(info) is str:
        return NAMED_KINDS[info]
    if type(info) is dict:
        variant = next(iter(info))
        status = info[variant]['httpStatusCode']
        if status is None and variant != 'responseTooManyFailedAttempts':
            return 'transport_or_io'
        return HTTP_KINDS.get(status, GENERIC)
    return GENERIC


def safe_error(value):
    if value is None:
        info = None
    elif type(value) is not dict:
        info = 'other'
    else:
        original = value.get('codexErrorInfo')
        info = safe_info(original)
        if original is not None and info is None:
            # Preserve that a terminal supplied an unknown reason without
            # retaining its arbitrary value or borrowing an older cause.
            info = 'other'
    return {
        'message': 'Codex reported an operation failure',
        'codexErrorInfo': info,
    }


def error_notice(event):
    p = event.get('params')
    if type(p) is not dict or type(p.get('willRetry')) is not bool:
        return None
    if any(type(p.get(k)) is not str or not 0 < len(p[k]) <= 4096 for k in ('threadId', 'turnId')):
        return None
    return {
        'method': 'error',
        'params': {
            'threadId': p['threadId'],
            'turnId': p['turnId'],
            'willRetry': p['willRetry'],
            'error': safe_error(p.get('error')),
        },
    }


def turn_completion(event):
    p = event.get('params')
    turn = p.get('turn') if type(p) is dict else None
    if type(turn) is not dict or turn.get('error') is None:
        return event
    return {**event, 'params': {**p, 'turn': {**turn, 'error': safe_error(turn['error'])}}}
