"""Actual unbound/routed frame encoding and lossless text reconstruction."""

from copy import deepcopy
import unittest

from report_display import display_fields, paged_text, text_pages, MAX_PAGES
from protocol import Packet
from test_report_voice import report, SOURCE
from report_voice import ReportVoice, report_text

ENVELOPE = dict(
    request_id='a' * 32,
    ok=True,
    action='report',
    source_title='😀' * 80,
    report_source_title='😀' * 80,
)


class ReportDisplayTests(unittest.TestCase):
    def encode(self, fields):
        packet = Packet.event('dot_chat_state', **ENVELOPE, **fields)
        packet.encode()
        packet.bind_route('b' * 32).encode()

    def test_valid_maximum_report_cannot_encode_without_bounded_display(self):
        value = report(*(['😀' * 500] * 20))
        self.assertTrue(ReportVoice.valid_report(SOURCE, value))
        original = deepcopy(value)
        text = report_text(value)
        with self.assertRaises(ValueError):
            Packet.event('dot_chat_state', report=value, report_text=text).encode()
        fields = display_fields(value, text, ENVELOPE)
        self.encode(fields)
        collected = []
        for page in range(fields['report_pages']):
            current = display_fields(value, text, ENVELOPE, page=page)
            self.encode(current)
            marker = current['report_page_marker']
            self.assertTrue(current['report_text'].startswith(marker))
            collected.append(current['report_text'][len(marker) :])
            self.assertEqual(len(current['report']['items']), 20)
        self.assertEqual(''.join(collected), text)
        self.assertEqual(value, original)

    def test_single_small_report_is_not_shortened_or_split(self):
        value = report('简短成果')
        text = report_text(value)
        fields = display_fields(value, text, ENVELOPE)
        self.assertEqual(fields['report'], value)
        self.assertEqual(fields['report_text'], text)
        self.assertEqual(fields['report_pages'], 1)
        self.encode(fields)

    def test_eight_maximum_labels_and_twenty_namespaced_results_fit_routed_frames(self):
        from group_work_reports import GroupWorkReports
        from report_sources import ReportScope
        from test_report_sources import entry, GroupFixture
        from work_reports import ReportWindow

        rows = [
            dict(
                entry('x' + str(i) + 'a' * 90),
                id=f'{i:016x}',
                registered_at=100,
                title='😀' * 80,
                project='😀' * 80,
            )
            for i in range(8)
        ]
        source = ReportScope.from_state(dict(schema=1, revision=1, items=rows))
        value = GroupWorkReports(GroupFixture().factory).read(source, ReportWindow(100, 200))
        value['items'] = report(*(['😀' * 500] * 20))['items']
        for i, item in enumerate(value['items']):
            member = source.members[i % 8]
            item.update(
                source_provider='codex',
                source_host='local',
                source_thread_id=member.thread_id,
                source_title=member.title,
                source_project=member.project,
            )
        self.assertTrue(ReportVoice.valid_report(source, value))
        original = report_text(value)
        first = display_fields(value, original, ENVELOPE)
        parts = []
        for page in range(first['report_pages']):
            fields = display_fields(value, original, ENVELOPE, page=page)
            self.encode(fields)
            parts.append(fields['report_text'][len(fields['report_page_marker']) :])
        self.assertEqual(''.join(parts), original)

    def test_long_history_json_escapes_and_unicode_reconstruct_exactly(self):
        text = '😀汉字"\\\n\t' * 6000
        first = paged_text(text, ENVELOPE, field='saved_report_text')
        self.assertGreater(first['report_pages'], 1)
        pieces = []
        for page in range(first['report_pages']):
            fields = paged_text(text, ENVELOPE, field='saved_report_text', page=page)
            self.encode(fields)
            pieces.append(fields['saved_report_text'][len(fields['report_page_marker']) :])
        self.assertEqual(''.join(pieces), text)

    def test_bad_page_unrepresentable_unicode_and_excess_capacity_are_rejected(self):
        for page in (True, -1, 1):
            with self.assertRaises(ValueError):
                paged_text('small', ENVELOPE, page=page)
        with self.assertRaises(UnicodeError):
            text_pages('\ud800')
        with self.assertRaises(ValueError):
            text_pages('😀' * (MAX_PAGES * 8000 // 4 + 1))
