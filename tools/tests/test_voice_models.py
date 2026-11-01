"""Both wake archives are bounded/verified; fixture extraction never downloads models."""

import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    'cc8_voice_setup_fixture', ROOT / 'scripts/setup-voice.py'
)
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


class VoiceModelTests(unittest.TestCase):
    def archive(self, tools, resources, name, resource, unsafe=None):
        archive = tools / (name + '.tar.bz2')
        with tarfile.open(archive, 'w:bz2') as output:
            entry = tarfile.TarInfo(name + '/tokens.txt')
            if unsafe:
                entry.name = unsafe[0]
                entry.type = unsafe[1]
                entry.linkname = '../../outside'
            if entry.isfile():
                data = b'fixed synthetic model bytes'
                entry.size = len(data)
                output.addfile(entry, io.BytesIO(data))
            else:
                output.addfile(entry)
        manifest = {
            'name': archive.name,
            'size': archive.stat().st_size,
            'digest': 'sha256:' + hashlib.sha256(archive.read_bytes()).hexdigest(),
            'browser_download_url': 'https://example.invalid/fixture',
        }
        (resources / resource).write_text(json.dumps(manifest))
        return archive

    def test_both_required_models_are_extracted_from_valid_cached_archives(self):
        with tempfile.TemporaryDirectory() as folder:
            tools, resources = Path(folder) / 'tools', Path(folder) / 'resources'
            tools.mkdir()
            resources.mkdir()
            for name, resource in [
                ('zh', 'fast-stop-model.json'),
                ('en', 'english-wake-model.json'),
            ]:
                self.archive(tools, resources, name, resource)
            with (
                patch.object(setup, 'TOOLS', tools),
                patch.object(setup, 'RESOURCES', resources),
                patch.object(
                    setup.subprocess, 'run', side_effect=AssertionError('unexpected network')
                ),
            ):
                setup.prepare_models()
            self.assertEqual((tools / 'zh/tokens.txt').read_bytes(), b'fixed synthetic model bytes')
            self.assertEqual((tools / 'en/tokens.txt').read_bytes(), b'fixed synthetic model bytes')

    def test_links_and_wrong_roots_are_rejected_before_any_extraction(self):
        for name, kind in [
            ('en/link', tarfile.SYMTYPE),
            ('en/hard', tarfile.LNKTYPE),
            ('../outside', tarfile.REGTYPE),
            ('other/tokens.txt', tarfile.REGTYPE),
        ]:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as folder:
                tools, resources = Path(folder) / 'tools', Path(folder) / 'resources'
                tools.mkdir()
                resources.mkdir()
                self.archive(tools, resources, 'en', 'english-wake-model.json', (name, kind))
                with (
                    patch.object(setup, 'TOOLS', tools),
                    patch.object(setup, 'RESOURCES', resources),
                    self.assertRaises(RuntimeError),
                ):
                    setup.prepare_kws_model('english-wake-model.json')
                self.assertFalse((tools / 'en').exists())
                self.assertFalse((Path(folder) / 'outside').exists())

    def test_download_bound_is_enforced_in_command_and_after_completion(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / 'fixture.tar.bz2'
            payload = b'synthetic download'
            digest = hashlib.sha256(payload).hexdigest()

            def curl(arguments, **kwargs):
                self.assertIn('--max-filesize', arguments)
                Path(arguments[arguments.index('-o') + 1]).write_bytes(payload)

            with patch.object(setup.subprocess, 'run', side_effect=curl):
                setup.download(
                    'https://example.invalid/fixture', target, digest, maximum_bytes=len(payload)
                )
                self.assertEqual(target.read_bytes(), payload)
                target.unlink()
                with self.assertRaises(RuntimeError):
                    setup.download(
                        'https://example.invalid/fixture',
                        target,
                        digest,
                        maximum_bytes=len(payload) - 1,
                    )
            self.assertFalse(target.exists())
            self.assertFalse(target.with_suffix(target.suffix + '.partial').exists())

    def test_invalid_download_bound_fails_before_network(self):
        for bound in (True, 0, -1, 2**40, 'SECRET'):
            with (
                self.subTest(kind=type(bound).__name__),
                patch.object(setup.subprocess, 'run') as run,
            ):
                with self.assertRaises(ValueError):
                    setup.download(
                        'https://example.invalid/fixture',
                        Path('unused'),
                        'not-used',
                        maximum_bytes=bound,
                    )
                run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
