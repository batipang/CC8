"""Typed non-secret settings: environment > owner-private profile > repository defaults.

This module never reads credential contents, model weights, Codex configuration or devices.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import stat
import tempfile
from typing import Mapping, Optional, cast
import uuid

from config_schema import (
    ConfigError,
    ENV_FIELDS,
    FIELDS,
    from_environment,
    schema,
    validate_document,
    validate_value,
)

ROOT = Path(__file__).resolve().parents[2]
PROFILE = '.toolchains/cc8/settings/assistant.json'


@dataclass(frozen=True)
class RuntimeConfig:
    asr: str
    brain: str
    minimax_base: str
    minimax_model: str
    asr_model: str
    tts_model: str
    tts_voice: str
    tts_speed: float
    tts_pitch: int
    tts_emotion: str | None
    tls_port: int
    connect_timeout: int
    read_timeout: int
    credential_file: Path
    signing_keystore: Path | None
    profile: Path
    sources: tuple[tuple[str, str], ...]
    legacy_profile: bool

    def public_summary(self) -> dict[str, object]:
        return {
            'schema_version': 1,
            'asr': self.asr,
            'brain': self.brain,
            'minimax_base': self.minimax_base,
            'minimax_model': self.minimax_model,
            'asr_model': self.asr_model,
            'tts_model': self.tts_model,
            'tts_voice': self.tts_voice,
            'tts_speed': self.tts_speed,
            'tts_pitch': self.tts_pitch,
            'tts_emotion': self.tts_emotion,
            'tls_port': self.tls_port,
            'connect_timeout': self.connect_timeout,
            'read_timeout': self.read_timeout,
            'credential_reference_configured': bool(self.credential_file),
            'signing_reference_configured': self.signing_keystore is not None,
            'sources': dict(self.sources),
            'migration_needed': self.legacy_profile,
        }

    def service_environment(self, environ: Mapping[str, str]) -> dict[str, str]:
        # Persist only explicitly supplied, validated non-secret overrides. API keys,
        # tokens and the owner's full environment never enter the LaunchAgent plist.
        result = {}
        for key, field in ENV_FIELDS.items():
            if key in environ:
                value = getattr(self, field)
                result[key] = '' if value is None else str(value)
        if 'CC8_CONFIG_FILE' in environ:
            result['CC8_CONFIG_FILE'] = str(self.profile)
        return result


def _path(raw: str, root: Path, field: str) -> Path:
    try:
        path = Path(raw).expanduser()
    except (RuntimeError, ValueError):
        raise ConfigError(field, 'invalid_filesystem_reference') from None
    return path if path.is_absolute() else root / path


def profile_path(root: Path, environ: Mapping[str, str]) -> Path:
    raw = environ.get('CC8_CONFIG_FILE', PROFILE)
    try:
        validated = validate_value('credential_file', raw)
    except ConfigError as error:
        raise ConfigError('config_file', error.reason) from None
    return _path(cast(str, validated), root, 'config_file')


def _read(path: Path, *, private: bool) -> tuple[dict[str, object], bool]:
    if private:
        info = path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077
        ):
            raise ConfigError('config_file', 'owner_only_file_required')
    try:
        flags = os.O_RDONLY | (getattr(os, 'O_NOFOLLOW', 0) if private else 0)
        # NONBLOCK prevents a replacement FIFO from blocking before fstat().
        flags |= getattr(os, 'O_NONBLOCK', 0)
        with os.fdopen(os.open(path, flags), 'rb') as source:
            opened = os.fstat(source.fileno())
            if not stat.S_ISREG(opened.st_mode):
                raise ConfigError('config_file', 'regular_file_required')
            if private and (opened.st_uid != os.getuid() or stat.S_IMODE(opened.st_mode) & 0o077):
                raise ConfigError('config_file', 'owner_only_file_required')
            raw = source.read(16385)
        if len(raw) > 16384:
            raise ConfigError('config_file', 'size_limit')

        def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
            values = {}
            for name, value in items:
                if name in values:
                    raise ConfigError('config_file', 'duplicate_field')
                values[name] = value
            return values

        data: object = json.loads(raw.decode('utf-8'), object_pairs_hook=pairs)
    except ConfigError:
        raise
    except (ValueError, UnicodeError, RecursionError):
        raise ConfigError('config_file', 'invalid_json') from None
    return validate_document(data)


def load_config(root: Path = ROOT, environ: Mapping[str, str] | None = None) -> RuntimeConfig:
    env = os.environ if environ is None else environ
    values = {name: spec.default for name, spec in FIELDS.items()}
    sources = {name: 'default' for name in values}
    base = root / 'cc8-assistant/host/voice-config.json'
    if not base.is_file():
        raise ConfigError('defaults', 'repository_defaults_missing')
    defaults, _ = _read(base, private=False)
    values.update(defaults)
    sources.update({name: 'repository' for name in defaults})
    selected = profile_path(root, env)
    legacy = False
    if selected.exists() or selected.is_symlink():
        overrides, legacy = _read(selected, private=True)
        values.update(overrides)
        sources.update({name: 'private' for name in overrides})
    elif 'CC8_CONFIG_FILE' in env:
        raise ConfigError('config_file', 'explicit_file_missing')
    for key, name in ENV_FIELDS.items():
        if key in env:
            values[name] = from_environment(name, env[key])
            sources[name] = 'environment'
    values = {name: validate_value(name, value) for name, value in values.items()}
    # Every value passed the schema above, including defaults and normalized numeric
    # environment overrides; these casts cross from the heterogeneous schema map.
    return RuntimeConfig(
        asr=cast(str, values['asr']),
        brain=cast(str, values['brain']),
        minimax_base=cast(str, values['minimax_base']),
        minimax_model=cast(str, values['minimax_model']),
        asr_model=cast(str, values['asr_model']),
        tts_model=cast(str, values['tts_model']),
        tts_voice=cast(str, values['tts_voice']),
        tts_speed=cast(float, values['tts_speed']),
        tts_pitch=cast(int, values['tts_pitch']),
        tts_emotion=cast(Optional[str], values['tts_emotion']),
        tls_port=cast(int, values['tls_port']),
        connect_timeout=cast(int, values['connect_timeout']),
        read_timeout=cast(int, values['read_timeout']),
        credential_file=_path(cast(str, values['credential_file']), root, 'credential_file'),
        signing_keystore=_path(cast(str, values['signing_keystore']), root, 'signing_keystore')
        if values['signing_keystore'] is not None
        else None,
        profile=selected,
        sources=tuple(sources.items()),
        legacy_profile=legacy,
    )


def write_profile(path: Path, values: dict[str, object]) -> None:
    validate_document(values)
    if path.is_symlink():
        raise ConfigError('config_file', 'owner_only_file_required')
    if path.exists():
        _read(path, private=True)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix='.assistant-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as out:
            os.fchmod(out.fileno(), 0o600)
            json.dump(values, out, ensure_ascii=False, indent=2, allow_nan=False)
            out.write('\n')
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def migrate(root: Path = ROOT, environ: Mapping[str, str] | None = None) -> RuntimeConfig:
    config = load_config(root, environ)
    if config.legacy_profile:
        values, _ = _read(config.profile, private=True)
        write_profile(config.profile, {'schema_version': 1, **values})
    return load_config(root, environ)


def reset(root: Path = ROOT, environ: Mapping[str, str] | None = None) -> Path | None:
    env = os.environ if environ is None else environ
    path = profile_path(root, env)
    backup = None
    if path.exists() or path.is_symlink():
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise ConfigError('config_file', 'owner_only_file_required')
        backup = path.with_name(path.name + '.' + uuid.uuid4().hex + '.backup')
        path.rename(backup)
        os.chmod(backup, 0o600)
    try:
        write_profile(path, {'schema_version': 1})
    except Exception:
        if backup is not None:
            backup.replace(path)
        raise
    return backup


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('check', 'show', 'migrate', 'reset', 'schema'))
    args = parser.parse_args()
    try:
        if args.action == 'schema':
            print(json.dumps(schema(), ensure_ascii=False, indent=2))
            return
        if args.action == 'reset':
            reset()
        config = migrate() if args.action == 'migrate' else load_config()
        print(json.dumps(config.public_summary(), ensure_ascii=False, indent=2))
    except ConfigError as error:
        parser.exit(2, str(error) + '\n')
    except OSError:
        parser.exit(2, 'CC8 configuration file unavailable; check owner access and path\n')


if __name__ == '__main__':
    main()
