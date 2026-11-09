"""Explicit, session-only voice controls; no capture or storage side effects."""

RATES = {'slow': 0.85, 'normal': 1.0, 'fast': 1.15}


class VoicePreferences:
    def __init__(self):
        self.revision = 0
        self.rate = 'normal'
        self.natural_barge = True
        self.recognition_language = 'auto'

    def snapshot(self, supported, recognition_supported=False):
        return {
            'version': 1,
            'revision': self.revision,
            'speech_rate': self.rate,
            'natural_barge': self.natural_barge,
            'supported': supported,
            'recognition_language': self.recognition_language,
            'recognition_language_supported': recognition_supported,
        }

    def apply(self, request, speech):
        # Caller holds the session guard and has established an idle pipeline.
        # Set the provider first: a failure must not publish an applied setting.
        speech.set_voice_rate(request['speech_rate'])
        self.rate = request['speech_rate']
        self.natural_barge = request['natural_barge']
        self.revision += 1
