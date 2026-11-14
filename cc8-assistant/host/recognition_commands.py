"""Parse recognized wake and stop commands without session state."""

import re

STOP_COMMANDS = frozenset(
    ('停', '停下', '停一下', '停止', '暂停', '打住', '等等', '别说了', '别念了', '听我说', '听一下')
)

WAKE_PREFIX_SEPARATORS = ' \t\r\n，。,！!：:'
WAKE_QUOTES = {'“': '”', '「': '」', '『': '』', '"': '"'}
SLEEP_COMMAND = re.compile(r'(?:请)?(?:你)?(?:先)?(?:去)?休息(?:一下|一会儿?)?吧?')


def wake_payload(text, wake_pattern):
    candidate = text.lstrip(WAKE_PREFIX_SEPARATORS)
    closing_quote = WAKE_QUOTES.get(candidate[:1])
    if closing_quote:
        candidate = candidate[1:].lstrip(WAKE_PREFIX_SEPARATORS)
    prefix = wake_pattern.match(candidate)
    if prefix is None:
        return None
    tail = candidate[prefix.end() :]
    if closing_quote:
        tail = tail.removeprefix(closing_quote)
    return tail.lstrip(WAKE_PREFIX_SEPARATORS)


def stop_command(text, wake_pattern):
    command = re.sub(r'\W', '', text)
    if command in STOP_COMMANDS:
        return command
    payload = wake_payload(text, wake_pattern)
    command = re.sub(r'\W', '', payload) if payload is not None else ''
    return command if command in STOP_COMMANDS else ''


def stop_matches_reference(command, references):
    for reference in references:
        clean = re.sub(r'\W', '', reference)
        if clean == command or (len(command) > 1 and command in clean):
            return True
    return False
