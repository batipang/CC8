"""Disposable public wordlists and real correction admission; no capture/provider upload."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path
import stat
import tempfile
import threading
import unittest
from unittest.mock import patch

from recognition_vocabulary import VocabularyStore, validate_request, word
from protocol import validate_device_event
import test_speech_correction as correction_fixture


def request(action='list', revision=None, **fields):
    if action == 'prepare':
        fields.setdefault('source_id', 'c' * 32)
    value = dict(
        event='vocabulary_request',
        request_id=fields.pop('request_id', 'a' * 32),
        action=action,
        **fields,
    )
    if revision is not None:
        value['revision'] = revision
    return value


class VocabularyStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'private/words.json'
        self.store = VocabularyStore(self.path)

    def put(self, revision=0, term='常州', aliases=None):
        return self.store.apply(
            request('put', revision, term=term, aliases=['沧州'] if aliases is None else aliases)
        )

    def test_default_read_is_lazy_and_explicit_private_changes_survive_restart(self):
        self.assertEqual(self.store.snapshot(), dict(schema=1, revision=0, items=[]))
        self.assertFalse(self.path.parent.exists())
        state = self.put()
        self.assertEqual(stat.S_IMODE(self.path.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(VocabularyStore(self.path).snapshot(), state)
        state['items'][0]['aliases'].append('external mutation')
        self.assertEqual(self.store.snapshot()['items'][0]['aliases'], ['沧州'])
        self.put(1, aliases=['长洲'])
        state = self.store.snapshot()
        self.assertEqual(state['revision'], 2)
        self.assertEqual(len(state['items']), 1)
        self.assertEqual(state['items'][0]['aliases'], ['长洲'])
        self.store.apply(request('delete', 2, term='常州'))
        self.assertEqual(self.store.snapshot()['items'], [])
        self.put(3, term='Bumblebee', aliases=['Bumble Bee'])
        self.store.apply(request('clear', 4))
        self.assertEqual(
            VocabularyStore(self.path).snapshot(), dict(schema=1, revision=5, items=[])
        )

    def test_casefold_edit_and_stale_or_unknown_mutations_do_not_overwrite(self):
        self.put(term='Bumblebee', aliases=['Bumble Bee'])
        self.put(1, term='BUMBLEBEE', aliases=['Bumble Bi'])
        before = self.path.read_bytes()
        for data in (
            request('put', 1, term='other', aliases=[]),
            request('delete', 2, term='missing'),
        ):
            with self.assertRaises(ValueError):
                self.store.apply(data)
            self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(len(self.store.snapshot()['items']), 1)

    def test_case_only_mishearing_is_a_literal_choice_not_an_automatic_rewrite(self):
        state = self.put(term='OpenClaw', aliases=['openclaw'])
        self.assertEqual(state['items'][0]['aliases'], ['openclaw'])
        pair = self.store.pair(request('prepare', 1, term='OpenClaw', alias='openclaw'))
        self.assertEqual(pair, ('OpenClaw', 'openclaw'))

    def test_schema_corruption_duplicate_json_fields_and_links_never_get_overwritten(self):
        self.put()
        for data in (
            b'{broken',
            b'{"schema":1,"schema":1,"revision":0,"items":[]}',
            json.dumps(dict(schema=1, revision=True, items=[])).encode(),
        ):
            self.path.write_bytes(data)
            with self.assertRaises(ValueError):
                self.put()
            self.assertEqual(self.path.read_bytes(), data)
        self.path.unlink()
        outside = Path(self.temp.name) / 'outside'
        outside.write_text('public inert marker')
        self.path.symlink_to(outside)
        with self.assertRaises(OSError):
            self.put()
        self.assertEqual(outside.read_text(), 'public inert marker')

    def test_word_and_alias_limits_reject_ambiguous_correction_delimiters(self):
        for value in ('', ' ', 'x' * 41, 'a\nb', 'a，b', 'a,b', '不是这个', '\ud800', True, None):
            with self.subTest(value=value), self.assertRaises((ValueError, UnicodeError)):
                word(value)
        for values in (['same'], ['other', 'OTHER'], ['a'] * 5, ['不是这个']):
            with self.assertRaises(ValueError):
                self.store.apply(request('put', 0, term='same', aliases=values))
        for n in range(64):
            self.put(n, term='term' + str(n), aliases=[])
        before = self.path.read_bytes()
        with self.assertRaises(ValueError):
            self.put(64, term='overflow', aliases=[])
        self.assertEqual(self.path.read_bytes(), before)

    def test_two_independent_writers_at_one_revision_cannot_lose_each_others_change(self):
        self.put()
        barrier = threading.Barrier(2)

        def change(term):
            store = VocabularyStore(self.path)
            barrier.wait(timeout=2)
            try:
                store.apply(request('put', 1, term=term, aliases=[]))
                return 'applied'
            except ValueError:
                return 'stale'

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(change, ['alpha', 'beta']))
        self.assertEqual(sorted(results), ['applied', 'stale'])
        self.assertEqual(self.store.snapshot()['revision'], 2)
        self.assertEqual(len(self.store.snapshot()['items']), 2)

    def test_durability_failure_keeps_a_committed_revision_for_explicit_reconciliation(self):
        import os

        real = os.fsync
        calls = []

        def fsync(fd):
            calls.append(fd)
            if len(calls) == 2:
                raise OSError('inert directory fsync failure')
            return real(fd)

        with patch('reminder_store.os.fsync', side_effect=fsync), self.assertRaises(OSError):
            self.put()
        self.assertEqual(self.store.snapshot()['revision'], 1)
        self.assertEqual(self.store.snapshot()['items'][0]['term'], '常州')
        with self.assertRaises(ValueError):
            self.put()

    def test_typed_protocol_rejects_overrides_and_wrong_revision_before_any_write(self):
        good = request('put', 0, term='常州', aliases=['沧州'])
        validate_device_event(good)
        for data in (
            {**good, 'revision': True},
            {**good, 'revision': -1},
            {**good, 'revision': '0'},
            {**good, 'path': '/never-write'},
            {**good, 'request_id': '../bad'},
            {**good, 'aliases': '沧州'},
            {**good, 'action': 'upload'},
        ):
            with self.assertRaises(ValueError):
                validate_request(data)
        self.assertFalse(self.path.parent.exists())


class VocabularyCorrectionTests(unittest.TestCase):
    def session(self):
        bridge = correction_fixture.CorrectionFlowTests().session()
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        bridge.vocabulary.store = VocabularyStore(Path(temp.name) / 'words.json')
        bridge.vocabulary.store.apply(request('put', 0, term='常州', aliases=['沧州']))
        return bridge

    def voice(self, bridge, text):
        correction_fixture.CorrectionFlowTests().voice(bridge, text)

    def test_explicit_word_selection_only_prepares_and_confirmation_submits_once(self):
        bridge = self.session()
        self.voice(bridge, '只读查询沧州今天的天气')
        task = deepcopy(bridge.workspace.tasks[-1])
        history = deepcopy(bridge.response.history)
        token = bridge.response.corrections.source_token(bridge.response.hooks.correction_owner())
        bridge.on_event(request('prepare', 1, term='常州', alias='沧州', source_id=token))
        draft = bridge.response.corrections.draft
        self.assertEqual(draft.original, '只读查询沧州今天的天气')
        self.assertEqual(draft.proposed, '只读查询常州今天的天气')
        self.assertEqual(bridge.brain.inputs, ['只读查询沧州今天的天气'])
        self.assertEqual(bridge.response.history, history)
        self.assertEqual(bridge.workspace.tasks, [task])
        self.assertTrue(bridge.events[-1][1]['prepared'])
        self.assertEqual(bridge.events[-1][0], 'vocabulary_result')
        self.voice(bridge, '确认更正')
        self.assertEqual(bridge.brain.inputs, ['只读查询沧州今天的天气', '只读查询常州今天的天气'])
        self.assertEqual(bridge.workspace.tasks[0], task)
        self.voice(bridge, '确认更正')
        self.assertEqual(len(bridge.brain.inputs), 2)
        self.assertEqual(bridge.vocabulary.store.snapshot()['revision'], 1)

    def test_old_pairs_repeated_aliases_partial_numbers_and_changed_owner_never_submit(self):
        for mode in ('stale', 'wrong_alias', 'duplicate', 'stop', 'partial_number'):
            bridge = self.session()
            text = '查询沧州天气'
            if mode == 'duplicate':
                text = '对比沧州和沧州的天气'
            if mode == 'partial_number':
                bridge.vocabulary.store.apply(request('put', 1, term='20', aliases=['10']))
                text = '查询100元记录'
            self.voice(bridge, text)
            before = list(bridge.brain.inputs)
            token = bridge.response.corrections.source_token(
                bridge.response.hooks.correction_owner()
            )
            if mode == 'stop':
                bridge.stop()
            data = request(
                'prepare',
                0 if mode == 'stale' else 1,
                term='常州',
                alias='没有这个词' if mode == 'wrong_alias' else '沧州',
            )
            if mode == 'partial_number':
                data = request('prepare', 2, term='20', alias='10')
            data['source_id'] = token
            bridge.on_event(data)
            self.assertFalse(bridge.events[-1][1]['ok'])
            self.assertIsNone(bridge.response.corrections.draft)
            self.assertEqual(bridge.brain.inputs, before)

    def test_old_wordlist_cannot_retarget_a_newer_recognition_with_the_same_alias(self):
        bridge = self.session()
        self.voice(bridge, '只读查询沧州甲的天气')
        old = bridge.response.corrections.source_token(bridge.response.hooks.correction_owner())
        self.voice(bridge, '只读查询沧州乙的天气')
        current = bridge.response.corrections.source_token(bridge.response.hooks.correction_owner())
        self.assertNotEqual(old, current)
        before = list(bridge.brain.inputs)
        bridge.on_event(request('prepare', 1, term='常州', alias='沧州', source_id=old))
        self.assertFalse(bridge.events[-1][1]['ok'])
        self.assertIsNone(bridge.response.corrections.draft)
        self.assertEqual(bridge.brain.inputs, before)
        self.assertEqual(
            bridge.response.corrections.source_token(bridge.response.hooks.correction_owner()),
            current,
        )

    def test_busy_mutations_and_duplicate_request_ids_do_not_write(self):
        bridge = self.session()
        path = bridge.vocabulary.store.path
        before = path.read_bytes()
        bridge.on_event(request('put', 1, term='another', aliases=[]))
        self.assertFalse(bridge.events[-1][1]['ok'])
        bridge.stop()
        bridge.on_event(request('put', 1, term='another', aliases=[]))
        self.assertFalse(bridge.events[-1][1]['ok'])
        self.assertEqual(path.read_bytes(), before)
        bridge.on_event(request('put', 1, term='another', aliases=[], request_id='b' * 32))
        self.assertTrue(bridge.events[-1][1]['ok'])
        self.assertEqual(bridge.vocabulary.store.snapshot()['revision'], 2)
