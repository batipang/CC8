"""One non-secret configuration schema; no model imports or credential reads."""

from __future__ import annotations

from dataclasses import dataclass
import math
import re


class ConfigError(ValueError):
    def __init__(self, field: str, reason: str) -> None:
        # Never include values or parser exception text: a misconfigured field can contain a key.
        self.field = (
            field if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,40}', field) else 'unknown_field'
        )
        self.reason = reason
        super().__init__(
            f'Invalid CC8 configuration: {self.field} ({reason}); see docs/CONFIGURATION.md'
        )


@dataclass(frozen=True)
class Field:
    kind: str
    default: object
    choices: tuple[str, ...] = ()
    minimum: float | None = None
    maximum: float | None = None
    max_length: int = 120
    nullable: bool = False


FIELDS = {
    'asr': Field('choice', 'minimax', ('minimax', 'sensevoice', 'whisper')),
    'brain': Field('choice', 'dot', ('dot', 'minimax', 'echo')),
    'capture_frontend': Field('choice', 'mono', ('mono', 'paired')),
    'minimax_base': Field(
        'endpoint',
        'https://api.minimax.cn',
        (
            'https://api.minimax.cn',
            'https://api.minimaxi.com',
            'https://api.minimax.io',
        ),
    ),
    'minimax_model': Field('identifier', 'MiniMax-M3'),
    'asr_model': Field('identifier', 'asr-1.0'),
    'tts_model': Field('identifier', 'speech-2.8-turbo'),
    'tts_voice': Field('text', 'Chinese (Mandarin)_Straightforward_Boy'),
    'tts_speed': Field('number', 1.03, minimum=0.5, maximum=2.0),
    'tts_pitch': Field('integer', 1, minimum=-12, maximum=12),
    'tts_emotion': Field('identifier', None, nullable=True),
    'tls_port': Field('integer', 18766, minimum=1024, maximum=65535),
    'connect_timeout': Field('integer', 8, minimum=1, maximum=30),
    'read_timeout': Field('integer', 20, minimum=1, maximum=120),
    'wake_window_seconds': Field('integer', 60, minimum=15, maximum=300),
    'chinese_wake_verifier': Field('choice', 'off', ('off', 'sensevoice', 'sensevoice_complete')),
    'english_wake_verifier': Field('choice', 'off', ('off', 'whisper_complete')),
    'credential_file': Field('path', '.toolchains/cc8/voice-secrets.env', max_length=4096),
    'signing_keystore': Field('path', None, max_length=4096, nullable=True),
}
ENV_FIELDS = {
    'CC8_ASR_ENGINE': 'asr',
    'CC8_BRAIN_PROVIDER': 'brain',
    'CC8_CAPTURE_FRONTEND': 'capture_frontend',
    'CC8_MINIMAX_BASE': 'minimax_base',
    'CC8_MINIMAX_MODEL': 'minimax_model',
    'CC8_ASR_MODEL': 'asr_model',
    'CC8_TTS_MODEL': 'tts_model',
    'CC8_TTS_VOICE': 'tts_voice',
    'CC8_TTS_SPEED': 'tts_speed',
    'CC8_TTS_PITCH': 'tts_pitch',
    'CC8_TTS_EMOTION': 'tts_emotion',
    'CC8_TLS_PORT': 'tls_port',
    'CC8_CONNECT_TIMEOUT': 'connect_timeout',
    'CC8_READ_TIMEOUT': 'read_timeout',
    'CC8_WAKE_WINDOW_SECONDS': 'wake_window_seconds',
    'CC8_CHINESE_WAKE_VERIFIER': 'chinese_wake_verifier',
    'CC8_ENGLISH_WAKE_VERIFIER': 'english_wake_verifier',
    'CC8_MINIMAX_CREDENTIAL_FILE': 'credential_file',
    'CC8_SIGNING_KEYSTORE': 'signing_keystore',
}


def validate_value(name: str, value: object) -> object:
    spec = FIELDS[name]
    if value is None and spec.nullable:
        return None
    if spec.kind in ('integer', 'number'):
        if (spec.kind == 'integer' and type(value) is not int) or (
            not isinstance(value, (int, float)) or isinstance(value, bool)
        ):
            raise ConfigError(name, 'wrong_number_type')
        if (
            (spec.minimum is not None and value < spec.minimum)
            or (spec.maximum is not None and value > spec.maximum)
            or (isinstance(value, float) and not math.isfinite(value))
        ):
            raise ConfigError(name, 'out_of_range')
        return int(value) if spec.kind == 'integer' else float(value)
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > spec.max_length
    ):
        raise ConfigError(name, 'invalid_text')
    try:
        value.encode('utf-8')
    except UnicodeError:
        raise ConfigError(name, 'invalid_unicode') from None
    if any(ord(c) < 32 for c in value):
        raise ConfigError(name, 'control_character')
    if re.search(r'sk-(?:cp-|proj-|ant-)', value) or value.startswith('Bearer '):
        raise ConfigError(name, 'credential_value_forbidden')
    if spec.kind == 'choice' and value not in spec.choices:
        raise ConfigError(name, 'unsupported_choice')
    if spec.kind == 'endpoint':
        value = value.removesuffix('/')
        if value not in spec.choices:
            raise ConfigError(name, 'official_https_endpoint_required')
    if spec.kind == 'identifier' and not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:/-]{0,95}', value):
        raise ConfigError(name, 'invalid_identifier')
    if spec.kind == 'path' and '://' in value:
        raise ConfigError(name, 'filesystem_reference_required')
    return value


def validate_document(data: object) -> tuple[dict[str, object], bool]:
    if not isinstance(data, dict):
        raise ConfigError('config_file', 'object_required')
    for key in data:
        if not isinstance(key, str) or key not in FIELDS and key != 'schema_version':
            raise ConfigError(key if isinstance(key, str) else 'unknown_field', 'unknown_field')
    legacy = 'schema_version' not in data
    if legacy:
        if not data or set(data) - {'asr'}:
            raise ConfigError('schema_version', 'missing_version')
    elif type(data['schema_version']) is not int or data['schema_version'] != 1:
        raise ConfigError('schema_version', 'unsupported_version')
    values = {
        key: validate_value(key, value) for key, value in data.items() if key != 'schema_version'
    }
    return values, legacy


def from_environment(field: str, raw: str) -> object:
    spec = FIELDS[field]
    try:
        if spec.kind == 'integer':
            if not re.fullmatch(r'-?[0-9]+', raw):
                raise ValueError()
            value: object = int(raw)
        elif spec.kind == 'number':
            value = float(raw)
        elif field == 'tts_emotion' and raw.lower() in ('', 'none', 'null'):
            value = None
        else:
            value = raw
    except ValueError:
        raise ConfigError(field, 'invalid_environment_value') from None
    return validate_value(field, value)


def schema() -> dict[str, object]:
    properties: dict[str, object] = {'schema_version': {'const': 1}}
    for name, field in FIELDS.items():
        entry: dict[str, object] = {'default': field.default}
        kind = field.kind if field.kind in ('integer', 'number') else 'string'
        entry['type'] = [kind, 'null'] if field.nullable else kind
        if field.kind in ('integer', 'number'):
            entry.update(minimum=field.minimum, maximum=field.maximum)
        else:
            entry.update(minLength=1, maxLength=field.max_length)
            blocked = r'sk-(?:cp-|proj-|ant-)|^Bearer |[\u0000-\u001f]|^\s|\s$'
            if field.kind == 'path':
                blocked += r'|://'
            entry['not'] = {'type': 'string', 'pattern': blocked}
        if field.kind == 'choice':
            entry['enum'] = list(field.choices)
        if field.kind == 'endpoint':
            entry['enum'] = [v for host in field.choices for v in (host, host + '/')]
        if field.kind == 'identifier':
            entry['pattern'] = r'^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,95}$'
        properties[name] = entry
    return {
        '$schema': 'https://json-schema.org/draft/2020-12/schema',
        'title': 'CC8 non-secret assistant configuration v1',
        'type': 'object',
        'additionalProperties': False,
        'required': ['schema_version'],
        'properties': properties,
    }
