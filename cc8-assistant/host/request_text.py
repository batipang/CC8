"""Validate owner text before cancellation, admission or UTF-8 transport work."""


def validate_request_text(text: object, maximum: int) -> str:
    if (
        not isinstance(text, str)
        or not text.strip()
        or len(text) > maximum
        or any(ord(character) < 32 and character not in '\n\t' for character in text)
    ):
        raise ValueError(f'Text must contain 1–{maximum} characters')
    try:
        # JSON can contain escaped lone surrogates, which fail only when a
        # later event/TTS request encodes. Reject them before touching ownership.
        text.encode('utf-8')
    except UnicodeEncodeError:
        raise ValueError('Text must contain valid Unicode') from None
    return text.strip()
