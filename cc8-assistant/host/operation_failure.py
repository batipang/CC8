"""Stable content-free failures carried across provider/transport boundaries."""

STAGES = frozenset(('asr', 'brain', 'search', 'tts', 'playback', 'transport', 'vision', 'runtime'))
KINDS = frozenset(
    (
        'rate_limit',
        'quota',
        'credential_or_permission',
        'approval_declined',
        'timeout',
        'transport_or_io',
        'provider_or_runtime',
    )
)


class ClassifiedFailure(RuntimeError):
    def __init__(self, message, *, kind='provider_or_runtime'):
        if kind not in KINDS:
            raise ValueError('Invalid failure category')
        super().__init__(message)
        self.kind = kind


class OperationFailure(ClassifiedFailure):
    def __init__(self, message, *, stage, kind):
        if stage not in STAGES or kind not in KINDS:
            raise ValueError('Invalid operation failure category')
        super().__init__(message, kind=kind)
        self.stage = stage
        self.kind = kind
