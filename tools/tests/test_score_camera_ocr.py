"""Paired scoring uses actual image dimensions and private result files."""

import json
from html.parser import HTMLParser
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from tools import score_camera_ocr as ocr


def recognized(text, dimensions):
    return {'status': 'recognized', 'text': text, 'dimensions': dimensions}


class CameraOcrTests(unittest.TestCase):
    def test_html_export_preserves_every_scoring_reference_and_order(self):
        class Sheet(HTMLParser):
            def __init__(self):
                super().__init__()
                self.current = None
                self.panels = {}

            def handle_starttag(self, tag, attrs):
                if tag == 'section':
                    self.current = dict(attrs)['id']
                    self.panels[self.current] = ''

            def handle_data(self, value):
                if self.current is not None:
                    self.panels[self.current] += value

            def handle_endtag(self, tag):
                if tag == 'section':
                    self.current = None

        sheet = Sheet()
        sheet.feed(ocr.card_sheet())
        self.assertEqual(list(sheet.panels.items()), list(ocr.cards().items()))

    def test_paired_scores_count_missing_characters_and_keep_digits(self):
        data = ocr.template()
        reference = ocr.cards()['card-09']
        data['samples']['card-09'] = {
            'standard': recognized(
                reference.replace('0O', '0[无法辨认]').replace('8', '6'), [640, 480]
            ),
            'hd': recognized(reference, [1280, 960]),
        }
        report = ocr.score(data)
        self.assertEqual(report['recognized_pairs'], 1)
        self.assertFalse(report['full_coverage'])
        sample = report['samples'][8]
        self.assertEqual(sample['standard']['unreadable_marks'], 1)
        self.assertEqual(sample['standard']['deletions'], 1)
        self.assertEqual(sample['standard']['substitutions'], 1)
        self.assertGreater(report['paired_scores']['standard']['cer'], 0)
        self.assertEqual(report['paired_scores']['hd']['cer'], 0)
        self.assertNotIn(reference, json.dumps(report, ensure_ascii=False))

    def test_low_resolution_fallback_and_failed_capture_do_not_count_as_pairs(self):
        data = ocr.template()
        data['samples']['card-01'] = {
            'standard': recognized('白色马克杯', [640, 480]),
            'hd': recognized('白色马克杯', [480, 640]),
        }
        data['samples']['card-02']['standard']['status'] = 'failed'
        data['samples']['card-03'] = {
            'standard': recognized('纸巾盒', [640, 480]),
            'hd': recognized('纸巾盒', [800, 300]),
        }
        report = ocr.score(data)
        self.assertEqual(report['recognized_pairs'], 0)
        self.assertEqual(report['hd_standard_fallbacks'], 1)
        self.assertEqual(report['hd_without_pixel_gain'], 1)
        self.assertEqual(report['mode_counts']['standard']['failed'], 1)
        self.assertIsNone(report['paired_scores']['hd']['cer'])

    def test_full_corpus_and_invalid_input(self):
        data = ocr.template()
        for ident, text in ocr.cards().items():
            data['samples'][ident] = {
                'standard': recognized(text, [480, 640]),
                'hd': recognized(text, [960, 1280]),
            }
        self.assertTrue(ocr.score(data)['full_coverage'])
        data['samples']['card-20']['hd']['text'] = ''
        with self.assertRaises(ValueError):
            ocr.score(data)
        data['samples']['card-20']['hd']['text'] = ocr.UNREADABLE
        data['samples']['card-20']['standard']['dimensions'] = [640, True]
        with self.assertRaises(ValueError):
            ocr.score(data)
        del data['samples']['card-20']
        with self.assertRaises(ValueError):
            ocr.score(data)

    def test_receipt_scoring_requires_a_distinct_terminal_camera_ocr_request(self):
        data = ocr.template(use_receipts=True)
        standard, hd, failed = '1' * 32, '2' * 32, '3' * 32
        report = dict(ok=True, schema=1, contains_content=False, requests=[])
        for mode, ident, dimensions in (
            ('standard', standard, [640, 480]),
            ('hd', hd, [960, 1280]),
        ):
            report['requests'].append(
                dict(
                    request_id=ident,
                    origin='vision',
                    camera_ocr=True,
                    outcome='completed',
                    camera_frame=dict(
                        dimensions=dimensions, bytes=1000, requested_mode=mode, edited=False
                    ),
                )
            )
        report['requests'].append(
            dict(request_id=failed, origin='vision', camera_ocr=True, outcome='failed')
        )
        for mode, ident in zip(ocr.MODES, (standard, hd)):
            data['samples']['card-01'][mode] = dict(
                status='recognized', text=ocr.cards()['card-01'], request_id=ident
            )
        data['samples']['card-02']['standard'] = dict(status='failed', text='', request_id=failed)
        scored = ocr.score(data, diagnostics=report)
        self.assertEqual(scored['recognized_pairs'], 1)
        self.assertEqual(scored['dimension_source'], 'request_receipt')
        self.assertEqual(scored['mode_counts']['standard']['failed'], 1)
        self.assertNotIn(standard, json.dumps(scored))
        self.assertNotIn(ocr.cards()['card-01'], json.dumps(scored, ensure_ascii=False))
        with self.assertRaises(ValueError):
            ocr.score(data)
        first = report['requests'][0]
        for key, value in (('camera_ocr', False), ('outcome', 'cancelled'), ('camera_frame', None)):
            previous = first[key]
            first[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                ocr.score(data, diagnostics=report)
            first[key] = previous
        frame = first['camera_frame']
        for metadata, reason in (
            ({}, 'capture_metadata_missing'),
            (dict(requested_mode='hd', edited=False), 'mode_mismatch'),
            (dict(requested_mode='standard', edited=True), 'edited_frame'),
        ):
            first['camera_frame'] = dict(dimensions=[640, 480], bytes=1000, **metadata)
            excluded = ocr.score(data, diagnostics=report)
            self.assertEqual(excluded['recognized_pairs'], 0)
            self.assertEqual(excluded['samples'][0]['standard']['comparison_exclusion'], reason)
            self.assertEqual(excluded['excluded_pairs'][reason], 1)
            self.assertFalse(excluded['full_coverage'])
        first['camera_frame'] = frame
        data['samples']['card-01']['hd']['request_id'] = standard
        with self.assertRaisesRegex(ValueError, 'reused'):
            ocr.score(data, diagnostics=report)

    def test_private_cli_does_not_overwrite_or_expose_transcripts(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            input_path, report = root / 'input.json', root / 'report.json'
            tool = ocr.ROOT / 'tools/score_camera_ocr.py'

            def run(*args):
                return subprocess.run(
                    [sys.executable, str(tool), *map(str, args)],
                    capture_output=True,
                    text=True,
                    env={'PATH': '/usr/bin:/bin'},
                    timeout=5,
                )

            self.assertEqual(run('--template', '--output', input_path).returncode, 0)
            self.assertEqual(input_path.stat().st_mode & 0o777, 0o600)
            data = json.loads(input_path.read_text())
            data['samples']['card-01']['hd'] = recognized('PRIVATE SAMPLE', [1280, 960])
            input_path.write_text(json.dumps(data))
            input_path.chmod(0o644)
            self.assertEqual(run('--input', input_path, '--output', report).returncode, 2)
            self.assertFalse(report.exists())
            input_path.chmod(0o600)
            self.assertEqual(run('--input', input_path, '--output', report).returncode, 0)
            self.assertEqual(report.stat().st_mode & 0o777, 0o600)
            before = report.read_bytes()
            self.assertNotIn(b'PRIVATE SAMPLE', before)
            self.assertEqual(run('--input', input_path, '--output', report).returncode, 2)
            self.assertEqual(report.read_bytes(), before)

            receipt_input, diagnostic, scored = (
                root / name for name in ('receipts.json', 'diagnostics.json', 'scored.json')
            )
            self.assertEqual(
                run('--template', '--receipt-template', '--output', receipt_input).returncode, 0
            )
            self.assertIn(
                'request_id', json.loads(receipt_input.read_text())['samples']['card-01']['hd']
            )
            diagnostic.write_text(
                json.dumps(dict(ok=True, schema=1, contains_content=False, requests=[]))
            )
            diagnostic.chmod(0o600)
            self.assertEqual(
                run(
                    '--input', receipt_input, '--diagnostics', diagnostic, '--output', scored
                ).returncode,
                0,
            )
            self.assertEqual(json.loads(scored.read_text())['recognized_pairs'], 0)
