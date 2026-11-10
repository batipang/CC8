"""Bounded wire views and strict controls, using actual RAM meeting state."""

import json
import unittest

from meeting_asr import Transcript
from meeting_session import CaptureOwner, MeetingSession
from meeting_view import index_page, status, text_page
from meeting_protocol import validate_request
from protocol import decode_json_object, HEADER, MAX_PACKET


class MeetingViewTests(unittest.TestCase):
    def model(self, text='中英 English 😀'):
        model = MeetingSession('a' * 32)
        model.start(CaptureOwner(1, 1, 'b' * 32))
        model.receive(model.owner, 0, 0, b'\x00\x20' * 1600)
        chunk = model.end()[0]
        model.complete(chunk, Transcript(text, 100))
        return model, chunk.ident

    def wire(self, value):
        raw = json.dumps(value, ensure_ascii=False).encode()
        self.assertLess(len(raw), MAX_PACKET - HEADER.size)
        self.assertEqual(decode_json_object(raw), value)

    def test_passive_status_has_no_text_audio_capture_owner_or_client(self):
        model, _ = self.model('private transcript')
        value = status(model)
        self.assertTrue(value['processing_complete'])
        self.assertNotIn('private', repr(value))
        self.assertNotIn('owner', value)
        self.wire(value)
        model.delete()
        self.assertEqual(status(model), {'available': False})

    def test_long_unicode_and_escaped_text_reconstruct_exactly_from_small_pages(self):
        text = ('😀中"\\\n\t' * 2000)[:12000].strip()
        model, ident = self.model(text)
        actual, offset = [], 0
        while offset is not None:
            value = text_page(model, model.ident, model.revision, ident, offset)
            self.wire(value)
            actual.append(value['text'])
            self.assertNotIn('pcm', value)
            offset = value['next_offset']
        self.assertEqual(''.join(actual), text)

    def test_corrected_and_original_views_are_explicit_and_never_mixed(self):
        model, ident = self.model('原始识别')
        model.edit(ident, model.revision, '人工校对')
        current = text_page(model, model.ident, model.revision, ident, 0)
        original = text_page(model, model.ident, model.revision, ident, 0, original=True)
        self.assertEqual(current['text'], '人工校对')
        self.assertEqual(original['text'], '原始识别')
        self.assertTrue(current['row']['edited'])
        self.assertEqual(current['row']['alignment'], 'edited_text')

    def test_stale_revision_foreign_meeting_deleted_state_and_invalid_cursors_rejected(self):
        model, ident = self.model()
        for meeting, revision in (
            (model.ident, True),
            ('b' * 32, model.revision),
            (model.ident, model.revision - 1),
        ):
            with self.assertRaises(ValueError):
                index_page(model, meeting, revision, 0)
        for cursor in (True, -1, 257, 2):
            with self.assertRaises(ValueError):
                index_page(model, model.ident, model.revision, cursor)
        for cursor in (True, -1, 12001, 100):
            with self.assertRaises(ValueError):
                text_page(model, model.ident, model.revision, ident, cursor)
        model.delete()
        with self.assertRaises(ValueError):
            text_page(model, model.ident, model.revision, ident, 0)

    def test_index_pages_include_all_gaps_without_sending_transcript_bodies(self):
        model = MeetingSession('a' * 32)
        for i in range(31):
            model.start(CaptureOwner(1, i, format(i, '032x')))
            model.receive(model.owner, 0, 0, b'\x00\x20' * 1600)
            model.pause()
            model.interrupt('route_lost')
        model.end()
        actual, offset = [], 0
        while offset is not None:
            value = index_page(model, model.ident, model.revision, offset)
            self.wire(value)
            self.assertLessEqual(len(value['rows']), 12)
            actual.extend(value['rows'])
            offset = value['next_offset']
        self.assertEqual(len(actual), 31)
        self.assertTrue(all(row['reason'] == 'route_lost' for row in actual))
        self.assertFalse(value['processing_complete'])
        self.assertFalse(any('text' in row for row in actual))

    def request(self, action, **kwargs):
        return dict(event='meeting_request', request_id='f' * 32, action=action, **kwargs)

    def test_each_control_has_exact_shape_and_explicit_confirmation(self):
        valid = [
            self.request('status'),
            self.request('prepare'),
            self.request('start', offer_id='a' * 32, confirmed=True, save_audio=False),
            self.request('prepare_resume', id='a' * 32, revision=7),
            self.request('resume', id='a' * 32, offer_id='b' * 32, confirmed=True),
            self.request('pause', id='a' * 32),
            self.request('end', id='a' * 32),
            self.request('abort_start', start_request_id='b' * 32),
            self.request('capture_failed', id='a' * 32, nonce='b' * 32, reason='capture_failed'),
            self.request('index', id='a' * 32, revision=7, offset=0),
            self.request(
                'text', id='a' * 32, revision=7, row_id='c' * 32, offset=0, original=False
            ),
            self.request(
                'edit', id='a' * 32, revision=7, row_id='c' * 32, text='校对', confirmed=True
            ),
            self.request('delete', id='a' * 32, revision=7, confirmed=True),
            self.request('export', id='a' * 32, revision=7, format='txt', confirmed=True),
            self.request('summarize', id='a' * 32, revision=7, confirmed=True),
            self.request('translate', id='a' * 32, revision=7, language='en', confirmed=True),
        ]
        for request in valid:
            with self.subTest(action=request['action']):
                validate_request(request)
                for key in request:
                    with self.assertRaises(ValueError):
                        validate_request({k: v for k, v in request.items() if k != key})
                with self.assertRaises(ValueError):
                    validate_request(dict(request, auto_start=True))
                if 'confirmed' in request:
                    for confirmation in (False, 1, 'true', None):
                        with self.assertRaises(ValueError):
                            validate_request(dict(request, confirmed=confirmation))

    def test_malformed_options_revisions_identities_content_and_unsupported_actions_rejected(self):
        requests = [
            self.request('start', offer_id='a' * 32, confirmed=True, save_audio=1),
            self.request('start', offer_id='../private', confirmed=True, save_audio=False),
            self.request('delete', id='a' * 32, revision=True, confirmed=True),
            self.request('text', id='a' * 32, revision=7, row_id='c' * 32, offset=0, original=1),
            self.request('index', id='a' * 32, revision=7, offset=257),
            self.request(
                'edit', id='a' * 32, revision=7, row_id='c' * 32, text='x' * 12001, confirmed=True
            ),
            self.request(
                'translate', id='a' * 32, revision=7, language='system:run', confirmed=True
            ),
            self.request('export', id='a' * 32, revision=7, format='../file', confirmed=True),
            self.request('auto_record'),
            self.request([]),
        ]
        for request in requests:
            with self.subTest(action=request['action']), self.assertRaises(ValueError):
                validate_request(request)


if __name__ == '__main__':
    unittest.main()
