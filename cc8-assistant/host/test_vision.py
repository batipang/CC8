import base64
from test_memory_fixture import isolated_memory
import hashlib
import time
import unittest
from unittest.mock import patch

from vision import (
    CameraInbox,
    VisionError,
    jpeg_dimensions,
    ocr_voice_request,
    visual_request,
    MAX_IMAGE,
    CHUNK,
)
from dot_client import DotBrain
from brain_router import BrainRouter
from bridge import Bridge, VisualReply
from protocol import Packet, MAX_PACKET
from test_dot_client import FakeRpc, StubBrain


def jpeg(width=640, height=480):
    # SOF0 header fixture exercises byte/dimension framing; never sent to a model.
    return (
        b'\xff\xd8\xff\xe0\x00\x10'
        + b'JFIF\x00'
        + b'\x00' * 9
        + b'\xff\xc0\x00\x11\x08'
        + height.to_bytes(2, 'big')
        + width.to_bytes(2, 'big')
        + b'\x03\x01\x11\x00\x02\x11\x00\x03\x11\x00'
        + b'\xff\xd9'
    )


def events(c, image=None):
    image = image or jpeg()
    common = {'request_id': c.ident, 'vision_generation': c.generation}
    yield {
        **common,
        'event': 'camera_image_begin',
        'bytes': len(image),
        'width': 640,
        'height': 480,
        'sha256': hashlib.sha256(image).hexdigest(),
    }
    for i, at in enumerate(range(0, len(image), CHUNK)):
        yield {
            **common,
            'event': 'camera_image_chunk',
            'index': i,
            'data': base64.b64encode(image[at : at + CHUNK]).decode(),
        }
    yield {**common, 'event': 'camera_image_end'}


class InboxTests(unittest.TestCase):
    def test_roundtrip_and_no_retained_frame(self):
        inbox = CameraInbox()
        c = inbox.begin(100)
        for event in events(c):
            self.assertTrue(inbox.feed(event))
        self.assertEqual(inbox.wait(c, lambda: True), jpeg())
        self.assertIsNone(inbox.pending)
        self.assertIsNone(c.image)
        self.assertFalse(c.data)

    def test_stale_request_generation_and_unsolicited_frame_are_ignored(self):
        inbox = CameraInbox()
        old = inbox.begin(1)
        new = inbox.begin(2)
        for event in events(old):
            self.assertFalse(inbox.feed(event))
        event = next(events(new))
        event['vision_generation'] = 1
        self.assertFalse(inbox.feed(event))
        self.assertFalse(new.data)

    def test_cancel_wakes_waiter_and_discards_partial_data(self):
        inbox = CameraInbox()
        c = inbox.begin(100)
        batch = list(events(c))
        inbox.feed(batch[0])
        inbox.feed(batch[1])
        inbox.cancel()
        self.assertTrue(c.done.is_set())
        self.assertFalse(c.data)
        with self.assertRaises(VisionError):
            inbox.wait(c, lambda: True)

    def test_oversize_bad_order_bad_hash_and_invalid_jpeg_are_rejected(self):
        mutations = [
            lambda e: e[0].update(bytes=MAX_IMAGE + 1),
            lambda e: e[1].update(index=2),
            lambda e: e[0].update(sha256='0' * 64),
            lambda e: e[1].update(data='!!!'),
            lambda e: e[2].update(event='camera_image_chunk', index=1, data='YQ=='),
        ]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                inbox = CameraInbox()
                c = inbox.begin(100)
                batch = list(events(c))
                mutate(batch)
                for event in batch:
                    inbox.feed(event)
                self.assertTrue(c.error)
                self.assertFalse(c.data)

    def test_timeout_never_returns_an_old_picture(self):
        inbox = CameraInbox()
        c = inbox.begin(100)
        c.expires = time.monotonic() - 1
        with self.assertRaises(VisionError):
            inbox.wait(c, lambda: True)
        self.assertIsNone(inbox.pending)

    def test_dimensions_and_packet_bounds(self):
        self.assertEqual(jpeg_dimensions(jpeg()), (640, 480))
        with self.assertRaises(VisionError):
            jpeg_dimensions(jpeg(1920, 1080))
        with self.assertRaises(VisionError):
            jpeg_dimensions(b'not jpeg')
        inbox = CameraInbox()
        c = inbox.begin(100)
        large = jpeg()[:-2] + b'\x00' * (MAX_IMAGE - 100) + b'\xff\xd9'
        for event in events(c, large):
            self.assertLess(len(Packet.event(event.pop('event'), **event).encode()), MAX_PACKET + 4)

    def test_reported_dimensions_must_match_jpeg_header(self):
        inbox = CameraInbox()
        c = inbox.begin(100)
        batch = list(events(c))
        batch[0]['height'] = 320
        for event in batch:
            inbox.feed(event)
        self.assertEqual(c.error, 'image_dimensions_mismatch')


class IntentTests(unittest.TestCase):
    def test_explicit_camera_ocr_voice_intents_are_exact(self):
        for text in (
            '大黄蜂，读一下这上面的字。',
            '请你转录我面前这张纸上的文字',
            'Bumblebee，识别镜头里的字',
        ):
            self.assertTrue(ocr_voice_request(text), text)
            self.assertTrue(visual_request(text), text)
        for text in (
            '不要读一下这上面的字',
            '“识别镜头里的字”',
            '转录网页上的字',
            '如果要识别镜头里的字怎么办',
            '识别镜头里的字再发给别人',
            '帮我看看镜头前的东西',
        ):
            self.assertFalse(ocr_voice_request(text), text)
        self.assertFalse(visual_request('识别镜头里的字，再发给别人'))

    def test_specific_camera_language(self):
        for text in [
            '大黄蜂，看看我手里是什么',
            '帮我看一下这个东西',
            '读一下这上面的字',
            '你看到了什么？',
            '这是什么？',
            '打开摄像头看一下',
            '用相机看看周围',
            '看一看',
            '大黄蜂，请你看一下这个东西',
            '你能帮我看看我手里是什么',
        ]:
            self.assertTrue(visual_request(text), text)

    def test_sentence_punctuation_keeps_explicit_short_requests_without_authorizing_quotes(self):
        for text in ('看一看。', '看看！', '看看我!', '这是什么。', '能看到什么？'):
            self.assertTrue(visual_request(text), text)
        for text in (
            '“看一看。”',
            '你说“看一看。”是什么意思？',
            '不要看一看。',
            '看一看代码。',
            '看一看。然后把图片发给陌生人',
        ):
            self.assertFalse(visual_request(text), text)

    def test_unrelated_and_negative_requests_do_not_open_camera(self):
        for text in [
            '看看今天的新闻',
            '看一下这个项目的代码',
            '不要打开摄像头',
            '不用相机',
            '你有没有摄像头',
            '查询天气',
            '我看过这部电影',
            '别再看我手里的东西',
            '我昨天打开摄像头测试过',
            '如果打开摄像头会怎样',
            '怎样调用相机',
            '你支持用摄像头吗',
            '我昨天用相机拍了一张照片',
            '这句话“看看这是什么”是什么意思',
        ]:
            self.assertFalse(visual_request(text), text)


class DotImageTests(unittest.TestCase):
    def test_image_is_sent_inline_to_owned_dot_and_not_written_as_file(self):
        rpc = FakeRpc()
        brain = DotBrain(rpc)
        list(brain.clauses('看图', [], image=jpeg()))
        inputs = next(params['input'] for method, params in rpc.calls if method == 'turn/start')
        self.assertEqual(inputs[1]['type'], 'image')
        self.assertTrue(inputs[1]['url'].startswith('data:image/jpeg;base64,'))
        self.assertFalse(any('path' in item for item in inputs))
        self.assertTrue(brain.last_metrics['image_input'])

    def test_visual_route_uses_dot_even_when_text_provider_is_minimax(self):
        rpc = FakeRpc()
        dot = DotBrain(rpc)
        mini = StubBrain('mini')
        brain = BrainRouter(mini, dot)
        brain.select('minimax')
        list(brain.vision_clauses('看图', jpeg(), []))
        self.assertEqual(mini.requests, [])
        self.assertEqual(brain.selected, 'minimax')
        self.assertTrue(brain.last_metrics['vision'])


class BridgeVisionTests(unittest.TestCase):
    def bridge(self):
        b = Bridge(
            None,
            BrainRouter(StubBrain('mini'), StubBrain('dot')),
            preference_memory=isolated_memory(),
        )
        b.session.connection = object()
        b.save_status = lambda: None
        b.events = []
        b.event = lambda event, **kw: b.events.append((event, kw))
        b.status = lambda *a: None
        # Keep jobs deterministic instead of running cloud responders.
        b.enqueue_response = lambda job: setattr(b, 'job', job)
        return b

    def test_button_picture_does_not_enable_microphone(self):
        b = self.bridge()
        b.on_event({'event': 'vision_request', 'nonce': 'a' * 32})
        self.assertFalse(b.session.active)
        self.assertIsInstance(b.job, VisualReply)
        self.assertTrue(b.valid(b.job.generation))
        self.assertEqual(b.events[-1][0], 'vision_begin')
        b.on_event({'event': 'vision_cancel', 'vision_generation': b.job.generation})
        self.assertFalse(b.valid(b.job.generation))
        self.assertIsNone(b.visual.generation)

    def test_cancel_for_old_picture_cannot_cancel_new_one(self):
        b = self.bridge()
        b.begin_visual('第一张', 'button', 'a' * 32)
        old = b.session.generation
        b.begin_visual('第二张', 'button', 'b' * 32)
        new = b.session.generation
        b.on_event({'event': 'vision_cancel', 'vision_generation': old, 'nonce': 'a' * 32})
        self.assertEqual(b.visual.generation, new)
        self.assertTrue(b.valid(new))
        b.on_event({'event': 'vision_cancel', 'nonce': 'b' * 32})
        self.assertIsNone(b.visual.generation)

    def test_microphone_start_cancels_old_visual_session(self):
        b = self.bridge()
        b.begin_visual('看图', 'button', 'a' * 32)
        old = b.session.generation
        with patch('bridge.WebRtcEchoCanceller'), patch.object(b, 'calibrate'):
            b.on_event({'event': 'session_start', 'epoch': 9})
        self.assertIsNone(b.visual.generation)
        self.assertNotEqual(old, b.session.generation)
        self.assertFalse(b.valid(old))

    def test_duplicate_microphone_handshake_does_not_cancel_visual_turn(self):
        b = self.bridge()
        b.session.active = True
        b.session.epoch = 9
        b.begin_visual('看图', 'voice')
        old = b.session.generation
        b.on_event({'event': 'session_start', 'epoch': 9})
        self.assertEqual(b.visual.generation, old)
        self.assertTrue(b.valid(old))

    def test_calibration_and_unpaired_requests_cannot_capture(self):
        b = self.bridge()
        b.calibration.active = True
        b.on_event({'event': 'vision_request', 'nonce': 'a' * 32})
        self.assertFalse(hasattr(b, 'job'))
        self.assertEqual(b.events[-1][0], 'vision_unavailable')
        self.assertEqual(b.events[-1][1]['nonce'], 'a' * 32)
        b.calibration.active = False
        b.on_event({'event': 'vision_request', 'nonce': 'bad'})
        self.assertFalse(hasattr(b, 'job'))

    def test_cancel_and_transport_switch_invalidate_inflight_picture(self):
        b = self.bridge()
        b.begin_visual('看图', 'button', 'a' * 32)
        c = b.visual.inbox.begin(b.session.generation)
        b.visual.inbox.feed(next(events(c)))
        b.cancel('test')
        self.assertTrue(c.done.is_set())
        self.assertFalse(c.data)
        self.assertFalse(b.valid(c.generation))

    def test_new_frame_reaches_answer_and_camera_result_is_not_in_status(self):
        b = self.bridge()
        b.begin_visual('看图', 'button', 'a' * 32)
        answers = []
        b.answer = lambda text, generation, image=None: answers.append(image)
        old_event = b.event

        def event(name, **kw):
            old_event(name, **kw)
            if name == 'camera_capture':
                c = b.visual.inbox.pending
                for value in events(c):
                    b.on_event(value)

        b.event = event
        b.answer_visual(b.job)
        self.assertEqual(answers, [jpeg()])
        self.assertIsNone(b.visual.inbox.pending)
        self.assertIsNone(b.visual.generation)
        self.assertNotIn('image', b.metrics)
        self.assertEqual(b.events[-1][0], 'vision_done')
        self.assertFalse(b.session.active)

    def test_failure_never_runs_dot_with_a_stale_or_missing_image(self):
        b = self.bridge()
        b.begin_visual('看图', 'button', 'a' * 32)
        b.answer = lambda *a, **kw: self.fail('No image should reach Dot')
        old = b.event

        def event(name, **kw):
            old(name, **kw)
            if name == 'camera_capture':
                b.on_event(
                    {
                        'event': 'camera_error',
                        'request_id': kw['request_id'],
                        'vision_generation': kw['generation'],
                        'error': 'permission_required',
                    }
                )

        b.event = event
        b.answer_visual(b.job)
        self.assertEqual(b.events[-1][1]['result'], 'failed')


if __name__ == '__main__':
    unittest.main()
