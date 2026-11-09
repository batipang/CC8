"""Explicit local sensitivity presets, not noise-source or speaker recognition.

Normal preserves the deployed thresholds. Changing a preset requires the shared
idle-setting gate; no setting can change transcript text or start capture.
"""

from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True)
class EnvironmentProfile:
    name: str
    minimum_dbfs: float
    maximum_dbfs: float
    noise_margin_db: float
    confirmation_frames: int
    confirmation_votes: int


PROFILES = MappingProxyType(
    {
        'normal': EnvironmentProfile('normal', -43.0, -34.0, 12.0, 20, 16),
        'quiet': EnvironmentProfile('quiet', -46.0, -37.0, 12.0, 20, 16),
        'television': EnvironmentProfile('television', -40.0, -31.0, 15.0, 24, 20),
    }
)


def profile(name: object) -> EnvironmentProfile:
    if type(name) is not str or name not in PROFILES:
        raise ValueError('Unknown listening environment')
    return PROFILES[name]
