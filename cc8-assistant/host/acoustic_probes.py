"""Coordinate owner acoustic probes with the existing capture and session owners."""


class AcousticProbes:
    def __init__(
        self,
        *,
        session,
        wake,
        capture,
        playback,
        calibration,
        recognition,
        response,
        announcements,
        cancel,
    ):
        self.session = session
        self.wake = wake
        self.capture = capture
        self.playback = playback
        self.calibration = calibration
        self.recognition = recognition
        self.response = response
        self.announcements = announcements
        self.cancel = cancel

    def playback_probe(self):
        from playback_evidence import PROBE_TEXT

        # Owner IPC may inspect an existing, bounded foreground capture, but
        # cannot open it. Prepare before dispatch so even immediate PCM is owned.
        with self.session.guard:
            if (
                not self.session.active
                or self.response.busy
                or self.recognition.busy
                or self.playback.playing
            ):
                raise RuntimeError('Playback probe requires idle active capture')
            probe = None

            def prepare(generation):
                nonlocal probe
                probe = self.capture.begin_probe(generation, self.session.epoch)

            try:
                job = self.announcements.announce(PROBE_TEXT, prepare=prepare)
            except Exception:
                if probe is not None:
                    self.capture.end_probe(probe, 'dispatch_failed')
                raise
            return job, probe

    def cancel_playback_probe(self, job, probe):
        with self.session.guard:
            self.capture.end_probe(probe)
            if self.session.generation == job.generation:
                self.cancel('playback_probe_cancelled')

    def finish_playback_probe(self, probe):
        with self.session.guard:
            self.capture.end_probe(probe)

    def arm_response_probe(self, case):
        with self.session.guard:
            if (
                not self.session.active
                or not self.session.wake
                or self.session.awake
                or self.calibration.active
                or self.response.busy
                or self.recognition.busy
                or self.playback.playing
            ):
                raise RuntimeError('Response probe requires idle active capture waiting for wake')
            return {'status': 'armed', 'token': self.capture.arm_response_probe(case)}

    def read_response_probe(self, token):
        with self.session.guard:
            return self.capture.read_response_probe(token)

    def arm_wake_input_probe(self, root, directory, metadata):
        from known_speech_evidence import load_fixture
        from wake_input_receipt import CHINESE_PCM_SHA256, CHINESE_QA_PCM_SHA256

        def ready():
            return (
                self.session.active
                and self.session.wake
                and not self.session.awake
                and self.wake.ready
                and not self.calibration.active
                and not self.playback.playing
                and not self.recognition.busy
                and not self.response.busy
                and not self.capture.endpoint.vad.talking
            )

        with self.session.guard:
            if not ready():
                raise RuntimeError('Wake input probe requires idle capture waiting for wake')
            binding = self.wake.serial
        pcm = load_fixture(root, directory, metadata)
        with self.session.guard:
            if self.wake.serial != binding or not ready():
                raise RuntimeError('Wake input probe owner changed')
            if isinstance(metadata, dict) and metadata.get('pcm_sha256') in (
                CHINESE_PCM_SHA256,
                CHINESE_QA_PCM_SHA256,
            ):
                return self.wake.arm_input_probe(pcm, chinese=True)
            return self.wake.arm_input_probe(pcm)

    def arm_known_speech(self, root, directory, metadata, generation, *, near_only=False):
        from known_speech_evidence import load_fixture

        def ready():
            if (
                type(generation) is not int
                or generation != self.session.generation
                or epoch != self.session.epoch
                or not self.session.active
                or self.calibration.active
            ):
                return False
            if not near_only:
                return self.playback.playing
            return (
                self.session.awake
                and not self.playback.playing
                and not self.recognition.busy
                and not self.response.busy
                and self.recognition.queue.empty()
                and self.response.queue.empty()
                and not self.capture.endpoint.vad.talking
            )

        with self.session.guard:
            if near_only:
                generation = self.session.generation
            epoch = self.session.epoch
            if not ready():
                raise RuntimeError('Known speech probe requires the selected active capture state')
        # Do not hold the session lock while reading a bounded public fixture.
        pcm = load_fixture(root, directory, metadata)
        with self.session.guard:
            if not ready():
                raise RuntimeError('Known speech probe owner changed')
            token = self.capture.arm_known_speech(
                pcm, metadata, generation, epoch, near_only=near_only
            )
            return {'status': 'armed', 'token': token}

    def mark_known_speech_stimulus(self, token, *, verify_far=False):
        def current(owner):
            return (
                owner is not None
                and self.capture.known_speech is owner
                and token == owner.token
                and self.session.active
                and owner.epoch == self.session.epoch
                and owner.generation == self.session.generation
                and self.playback.playing
                and not self.calibration.active
            )

        with self.session.guard:
            owner = self.capture.known_speech
            if not current(owner):
                return {'status': 'unavailable'}
            if not verify_far:
                return owner.mark_stimulus()
        result = owner.mark_stimulus(verify_far=True)
        with self.session.guard:
            return result if current(owner) else {'status': 'unavailable'}

    def read_known_speech(self, token):
        with self.session.guard:
            owner = self.capture.known_speech
            if (
                owner is None
                or token != owner.token
                or not self.session.active
                or owner.epoch != self.session.epoch
            ):
                return {'status': 'unavailable'}
            if owner.near_only and self.playback.playing:
                owner.cancel('unexpected_render')
        # FFT work runs on this IPC caller, outside the ingress/session lock.
        release = True
        try:
            result = owner.read()
            release = result.get('status') != 'analysing'
            with self.session.guard:
                if (
                    self.capture.known_speech is not owner
                    or not self.session.active
                    or owner.epoch != self.session.epoch
                ):
                    return {'status': 'unavailable'}
                return result
        finally:
            with self.session.guard:
                if release and self.capture.known_speech is owner:
                    self.capture.clear_known_speech()

    def cancel_playback_observation(self):
        self.capture.end_probe()
        self.capture.evidence.cancel()
