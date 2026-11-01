"""Manifest compatibility and build provenance without SDK/model/device access."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import release_info as versions


class ReleaseTests(unittest.TestCase):
    def test_independent_versions_and_protocol_type_boundaries(self):
        release = versions.RELEASE
        self.assertNotEqual(release.assistant.version, release.backend_version)
        self.assertNotEqual(release.diagnostics.version, release.assistant.version)
        self.assertTrue(release.compatible(release.protocol_version))
        for value in (True, False, '2', 2.0, None, 0, 255):
            self.assertFalse(release.compatible(value))

    def test_invalid_schema_ranges_codes_packages_and_duplicate_fields_fail(self):
        original = json.loads((versions.ROOT / 'release.json').read_text())
        cases = []
        for field, value in (('schema_version', True), ('release_id', '../escape')):
            item = json.loads(json.dumps(original))
            item[field] = value
            cases.append(json.dumps(item))
        for component, field, value in (
            ('protocol', 'device_max', 1),
            ('assistant', 'version_code', True),
            ('assistant', 'package', 'other.app'),
            ('backend', 'version', 'secret invalid'),
        ):
            item = json.loads(json.dumps(original))
            item[component][field] = value
            cases.append(json.dumps(item))
        cases += ['{"schema_version":1,"schema_version":1}', 'x' * 16385]
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for raw in cases:
                (root / 'release.json').write_text(raw)
                with self.subTest(raw=raw[:80]), self.assertRaises(ValueError):
                    versions.load_release(root)

    def test_build_receipt_binds_clean_source_manifest_version_and_bytes(self):
        stamp = versions.SourceStamp('a' * 40, False, 'b' * 64)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            apk = root / 'candidate.apk'
            apk.write_bytes(b'signed candidate')
            with (
                patch.object(versions, 'source_stamp', return_value=stamp),
                patch.object(versions, 'load_release', return_value=versions.RELEASE),
            ):
                versions.write_build_receipt(apk, 'assistant', stamp, root)
            value = json.loads(apk.with_suffix('.apk.build.json').read_text())
            self.assertEqual(value['source_commit'], 'a' * 40)
            self.assertIs(value['source_dirty'], False)
            self.assertEqual(value['version_code'], versions.RELEASE.assistant.version_code)
            self.assertEqual(value['manifest_sha256'], 'b' * 64)
            self.assertEqual(apk.with_suffix('.apk.build.json').stat().st_mode & 0o777, 0o600)

    def test_changed_source_cannot_issue_build_receipt(self):
        with tempfile.TemporaryDirectory() as folder:
            apk = Path(folder) / 'app.apk'
            stamp = versions.SourceStamp('a' * 40, False, 'b' * 64)
            with patch.object(
                versions,
                'source_stamp',
                return_value=versions.SourceStamp('c' * 40, False, 'b' * 64),
            ):
                with self.assertRaises(ValueError):
                    versions.write_build_receipt(apk, 'assistant', stamp)
            self.assertFalse(apk.with_suffix('.apk.build.json').exists())


if __name__ == '__main__':
    unittest.main()
