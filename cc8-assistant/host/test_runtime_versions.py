"""Real handshake/status declarations remain separate from installation verification."""

from dataclasses import replace
import json
import unittest
from unittest.mock import Mock, patch

from device_session import complete_handshake
from release_info import RELEASE
from runtime_versions import device_release, public_release, status_versions
import test_device_control_flow as control_flow


def receipt(**changes):
    return {
        'schema_version': 1,
        'assistant_version': RELEASE.assistant.version,
        'assistant_code': RELEASE.assistant.version_code,
        'protocol_version': RELEASE.protocol_version,
        **changes,
    }


class RuntimeVersionTests(unittest.TestCase):
    def test_loaded_manifest_is_public_detached_and_not_installation_proof(self):
        with patch('pathlib.Path.read_bytes', side_effect=AssertionError('disk read')):
            first = public_release()
            first['backend_version'] = 'mutated'
            second = public_release()
        self.assertEqual(second['backend_version'], RELEASE.backend_version)
        self.assertEqual(
            set(second),
            {
                'schema_version',
                'release_id',
                'backend_version',
                'expected_assistant_version',
                'expected_assistant_code',
                'protocol_version',
                'device_protocol_min',
                'device_protocol_max',
            },
        )
        result = status_versions(
            {'ok': True, 'device_release': receipt(), 'installation_verified': True}
        )
        self.assertTrue(result['declared_protocol_compatible'])
        self.assertTrue(result['declared_version_pair_matches'])
        self.assertFalse(result['installation_verified'])
        self.assertNotIn('/', json.dumps(second))

    def test_malformed_legacy_or_mismatching_device_does_not_borrow_expected_version(self):
        invalid = [None, [], {}, receipt(extra='ignored?')]
        for key, values in {
            'schema_version': [True, 2, '1', 1.0],
            'assistant_version': [True, '1.2', '1.2.3\nsecret', '1' * 33 + '.2.3'],
            'assistant_code': [True, 0, -1, 2**31, '158', 158.0],
            'protocol_version': [True, 0, 256, '2', 2.0],
        }.items():
            invalid.extend(receipt(**{key: value}) for value in values)
        for value in invalid:
            with self.subTest(value=value):
                self.assertIsNone(device_release(value))
                result = status_versions({'ok': True, 'device_release': value})
                self.assertIsNone(result['device_release'])
                self.assertIsNone(result['declared_version_pair_matches'])
                self.assertIsNone(result['declared_protocol_compatible'])
                self.assertFalse(result['installation_verified'])
        old = receipt(assistant_code=1, assistant_version='0.0.1', protocol_version=255)
        result = status_versions({'ok': True, 'device_release': old})
        self.assertFalse(result['declared_protocol_compatible'])
        self.assertFalse(result['declared_version_pair_matches'])
        self.assertEqual(result['device_release']['assistant_version'], '0.0.1')
        old['assistant_version'] = 'changed'
        self.assertEqual(result['device_release']['assistant_version'], '0.0.1')
        other = replace(RELEASE, backend_version='1.0.0', device_protocol_max=255)
        self.assertTrue(
            status_versions({'device_release': receipt(protocol_version=255)}, other)[
                'declared_protocol_compatible'
            ]
        )

    def test_status_actual_controller_filters_spoofs_and_only_enriches_success(self):
        session = control_flow.DeviceControlFlowTests().session()

        def respond(name, **fields):
            session.on_event(
                {
                    'event': 'device_control_result',
                    'request_id': 'old',
                    'ok': True,
                    'device_release': receipt(assistant_code=1),
                }
            )
            session.on_event(
                {
                    'event': 'device_control_result',
                    'request_id': fields['request_id'],
                    'ok': True,
                    'device_release': receipt(),
                    'host_release': {'backend_version': 'spoof'},
                    'installation_verified': True,
                    'declared_protocol_compatible': False,
                }
            )

        session.event = respond
        result = session.control_device('status')
        self.assertEqual(result['host_release'], public_release())
        self.assertEqual(result['device_release'], receipt())
        self.assertFalse(result['installation_verified'])
        self.assertTrue(result['declared_protocol_compatible'])
        for payload in ({'ok': True}, {'ok': True, 'device_release': receipt(extra=True)}):
            session.event = lambda name, **fields: session.on_event(
                {'event': 'device_control_result', 'request_id': fields['request_id'], **payload}
            )
            self.assertIsNone(session.control_device('status')['device_release'])
        session.event = lambda name, **fields: session.on_event(
            {
                'event': 'device_control_result',
                'request_id': fields['request_id'],
                'ok': False,
                'error': 'unavailable',
            }
        )
        self.assertEqual(session.control_device('status'), {'ok': False, 'error': 'unavailable'})
        self.assertFalse(session.session.active)
        self.assertEqual(session.device.waiters, {})

    def test_real_handshake_advertises_loaded_public_release_without_capture(self):
        session = control_flow.DeviceControlFlowTests().session()
        session.session.negotiate_capture_frontend = Mock(return_value='mono')
        session.session.voice_preferences_state = Mock(return_value={})
        # Exercise actual handshake through its real component owners. The hook
        # adapters are the same as Bridge's; avoid scheduling background work.
        with patch('device_session.window_available', return_value=False):
            complete_handshake(session.session, {})
        hello = [call for call in session.event.call_args_list if call.args == ('hello',)]
        self.assertEqual(len(hello), 1)
        self.assertEqual(hello[0].kwargs['release_info'], public_release())
        self.assertFalse(session.session.active)


if __name__ == '__main__':
    unittest.main()
