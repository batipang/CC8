"""Artifact inventories cannot disguise dirty, stale or path-injected records."""

import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from tools import release


class InventoryTests(unittest.TestCase):
    def fixture(self, root):
        shutil.copyfile(release.ROOT / 'release.json', root / 'release.json')
        manifest_hash = hashlib.sha256((root / 'release.json').read_bytes()).hexdigest()
        (root / 'artifacts').mkdir()
        config = release.load_release(root)
        for name in ('assistant', 'diagnostics'):
            info = getattr(config, name)
            apk = root / 'artifacts' / f'cc8-{name}-{info.version}.apk'
            apk.write_bytes(b'candidate')
            value = {
                'schema_version': 1,
                'component': name,
                'version': info.version,
                'version_code': info.version_code,
                'source_commit': 'a' * 40,
                'source_dirty': False,
                'manifest_sha256': manifest_hash,
                'apk_sha256': release.digest(apk),
            }
            apk.with_suffix('.apk.build.json').write_text(json.dumps(value))

    def fake_apk(self, component, apk, root):
        info = getattr(release.load_release(root), component)
        return {
            'component': component,
            'filename': apk.name,
            'package': info.package,
            'version': info.version,
            'version_code': info.version_code,
            'sha256': release.digest(apk),
            'signer_sha256': ['f' * 64],
        }

    def test_clean_record_roundtrip_and_tampered_types_or_paths_fail(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.fixture(root)
            with (
                patch.object(release, 'git_state', return_value=('a' * 40, False)),
                patch.object(release, 'check_apk', side_effect=self.fake_apk),
            ):
                value = release.create_record(root)
                path = root / 'record.json'
                release.write_record(path, value)
                self.assertEqual(release.verify_record(path, root), value)
                for field, replacement in (
                    ('schema_version', True),
                    ('source_commit', 'b' * 40),
                    ('artifacts', [{'filename': '/private/secret'}]),
                ):
                    altered = dict(value, **{field: replacement})
                    path.write_text(json.dumps(altered))
                    with self.subTest(field=field), self.assertRaises(ValueError):
                        release.verify_record(path, root)

    def test_stale_receipt_or_modified_apk_cannot_claim_current_source(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.fixture(root)
            config = release.load_release(root)
            apk = root / 'artifacts' / f'cc8-assistant-{config.assistant.version}.apk'
            apk.write_bytes(b'changed signed APK')
            with (
                patch.object(release, 'git_state', return_value=('a' * 40, False)),
                patch.object(release, 'check_apk', side_effect=self.fake_apk),
            ):
                with self.assertRaises(ValueError):
                    release.create_record(root)

    def test_dirty_source_and_candidate_receipts_cannot_be_verified_release(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.fixture(root)
            with patch.object(release, 'git_state', return_value=('a' * 40, True)):
                with self.assertRaises(ValueError):
                    release.create_record(root)
            path = root / 'candidate.json'
            path.write_text('{"source_dirty":true}')
            with self.assertRaises(ValueError):
                release.verify_record(path, root)

    def test_nonannotated_or_wrong_target_tag_is_rejected_before_artifact_read(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.fixture(root)
            with patch.object(release, 'git_state', return_value=('a' * 40, False)):
                for responses in (['commit', 'a' * 40], ['tag', 'b' * 40]):
                    with (
                        patch.object(release.subprocess, 'check_output', side_effect=responses),
                        self.assertRaises(ValueError),
                    ):
                        release.create_record(root, tag='foundation-test')


if __name__ == '__main__':
    unittest.main()
