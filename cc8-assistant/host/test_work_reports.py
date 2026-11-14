"""Completion-time truth, selected-source boundaries and bounded readonly reports."""

from datetime import datetime, timezone
import unittest
import threading
from unittest.mock import patch
from dot_handoff import Handoff
from dot_client import Cancelled, DotError
from work_reports import ReportWindow, WorkReportReader
from report_display import text_pages
from report_voice import report_text


def turn(ident='turn-1', *, completed=150, status='completed', started=50):
    return dict(
        id=ident, completedAt=completed, startedAt=started, status=status, updatedAt=150, items=[]
    )


def final(ident='final-1', text='原答复：已产出，尚待实机'):
    return dict(id=ident, type='agentMessage', phase='final_answer', text=text)


class Fixture:
    def __init__(self):
        self.turn_pages = [dict(data=[turn()], nextCursor=None)]
        self.item_pages = {
            'turn-1': [dict(data=[dict(turnId='turn-1', item=final())], nextCursor=None)]
        }
        self.calls, self.clients = [], []
        self.after_request = lambda: None

    def factory(self):
        fixture = self

        class Rpc:
            def __init__(self):
                self.closed = self.started = False

            def start(self):
                self.started = True

            def request(self, method, params, **kwargs):
                fixture.calls.append((method, dict(params)))
                assert params['threadId'] == 'selected-chat'
                if method == 'thread/turns/list':
                    assert params['itemsView'] == 'notLoaded'
                    index = int(params.get('cursor', '0'))
                    result = fixture.turn_pages[index]
                elif method == 'thread/items/list':
                    index = int(params.get('cursor', '0'))
                    result = fixture.item_pages[params['turnId']][index]
                else:
                    raise AssertionError('Report must not start/resume/write a thread')
                fixture.after_request()
                return result

            def close(self):
                self.closed = True

        client = Rpc()
        self.clients.append(client)
        return client


class ReportTests(unittest.TestCase):
    source = Handoff('selected-chat', '所选来源', (), False)

    def report(self, fixture, **kwargs):
        return WorkReportReader(fixture.factory).read(self.source, ReportWindow(100, 200), **kwargs)

    def test_same_window_uses_completion_time_and_reads_only_matching_turn_items(self):
        f = Fixture()
        f.turn_pages = [
            dict(
                data=[
                    turn('end', completed=200),
                    turn('start', completed=100),
                    turn('old', completed=99),
                    turn('failed', status='failed'),
                    turn('interrupted', status='interrupted'),
                    turn('active', status='inProgress'),
                ],
                nextCursor=None,
            )
        ]
        f.item_pages = {'start': [dict(data=[dict(turnId='start', item=final())], nextCursor=None)]}
        r = self.report(f)
        self.assertEqual([x['turn_id'] for x in r['items']], ['start'])
        self.assertEqual(
            (r['failed_turns'], r['interrupted_turns'], r['active_turns_seen']), (1, 1, 1)
        )
        self.assertEqual(
            [p['turnId'] for name, p in f.calls if name == 'thread/items/list'], ['start']
        )
        self.assertTrue(r['coverage_complete'])
        self.assertEqual(r['items'][0]['verification'], 'not_assessed')
        self.assertTrue(all(c.closed for c in f.clients))

    def test_missing_or_invalid_completion_never_borrows_update_or_start_time(self):
        for completed in (None, True, '150', 40):
            f = Fixture()
            f.turn_pages[0]['data'] = [turn(completed=completed)]
            r = self.report(f)
            self.assertEqual(r['unknown_completion_time'], 1)
            self.assertFalse(r['empty_confirmed'])
            self.assertFalse(r['coverage_complete'])
            self.assertEqual(len(f.calls), 1)

    def test_cross_window_old_start_on_later_page_is_not_skipped(self):
        f = Fixture()
        f.turn_pages = [
            dict(data=[turn('older-completion', completed=99)], nextCursor='1'),
            dict(data=[turn(started=1, completed=175)], nextCursor=None),
        ]
        r = self.report(f)
        self.assertEqual(r['items'][0]['completed_at'], 175)
        self.assertEqual(len(f.calls), 3)

    def test_partial_history_does_not_report_zero_results(self):
        f = Fixture()
        f.turn_pages = [dict(data=[], nextCursor=str(i + 1)) for i in range(4)]
        r = self.report(f)
        self.assertEqual(len(f.calls), 4)
        self.assertFalse(r['coverage_complete'])
        self.assertFalse(r['empty_confirmed'])

    def test_later_read_failure_preserves_validated_results_with_partial_coverage(self):
        for phase in ('turn_page', 'item_page', 'next_result'):
            with self.subTest(phase=phase):
                f = Fixture()
                if phase == 'turn_page':
                    f.turn_pages[0]['nextCursor'] = '1'
                elif phase == 'item_page':
                    f.item_pages['turn-1'][0]['nextCursor'] = '1'
                else:
                    f.turn_pages[0]['data'].append(turn('older', completed=149))
                rpc = f.factory()
                request = rpc.request

                def interrupted(method, params, **options):
                    if (
                        (
                            phase == 'turn_page'
                            and method == 'thread/turns/list'
                            and 'cursor' in params
                        )
                        or (
                            phase == 'item_page'
                            and method == 'thread/items/list'
                            and 'cursor' in params
                        )
                        or (phase == 'next_result' and params.get('turnId') == 'older')
                    ):
                        raise DotError('PRIVATE_PROVIDER_MESSAGE', kind='timeout')
                    return request(method, params, **options)

                rpc.request = interrupted
                result = WorkReportReader(lambda: rpc).read(self.source, ReportWindow(100, 200))
                self.assertEqual([row['turn_id'] for row in result['items']], ['turn-1'])
                self.assertEqual(result['items'][0]['evidence_complete'], phase != 'item_page')
                self.assertFalse(result['coverage_complete'])
                self.assertEqual(result['unconfirmed_final_answer'], int(phase == 'next_result'))
                self.assertFalse(result['empty_confirmed'])
                self.assertNotIn('PRIVATE_PROVIDER_MESSAGE', repr(result))
                self.assertTrue(rpc.closed)

    def test_answer_disconnect_retains_observed_turn_states_and_stops_reading(self):
        f = Fixture()
        f.turn_pages[0]['data'].extend(
            [turn('failed', status='failed'), turn('older', completed=149)]
        )
        rpc = f.factory()
        request = rpc.request

        def interrupted(method, params, **options):
            if method == 'thread/items/list':
                self.assertEqual(params['turnId'], 'turn-1')
                raise DotError('PRIVATE_PROVIDER_MESSAGE', kind='transport_or_io')
            return request(method, params, **options)

        rpc.request = interrupted
        result = WorkReportReader(lambda: rpc).read(self.source, ReportWindow(100, 200))
        self.assertEqual(result['failed_turns'], 1)
        self.assertEqual(result['unconfirmed_final_answer'], 2)
        self.assertEqual(result['missing_final_answer'], 0)
        self.assertFalse(result['empty_confirmed'])
        self.assertFalse(result['coverage_complete'])
        self.assertNotIn('PRIVATE_PROVIDER_MESSAGE', repr(result))
        self.assertTrue(rpc.closed)

    def test_one_answer_timeout_still_reads_the_next_completed_turn(self):
        f = Fixture()
        f.turn_pages[0]['data'].append(turn('older', completed=149))
        f.item_pages['older'] = [
            dict(
                data=[dict(turnId='older', item=final(text='可核对的另一条成果'))], nextCursor=None
            )
        ]
        rpc = f.factory()
        request = rpc.request

        def interrupted(method, params, **options):
            if params.get('turnId') == 'turn-1':
                raise DotError('PRIVATE_PROVIDER_MESSAGE', kind='timeout')
            return request(method, params, **options)

        rpc.request = interrupted
        result = WorkReportReader(lambda: rpc).read(self.source, ReportWindow(100, 200))
        self.assertEqual([row['turn_id'] for row in result['items']], ['older'])
        self.assertEqual(result['unconfirmed_final_answer'], 1)
        self.assertEqual(result['missing_final_answer'], 0)
        self.assertFalse(result['coverage_complete'])
        self.assertNotIn('PRIVATE_PROVIDER_MESSAGE', repr(result))
        self.assertTrue(rpc.closed)

    def test_answer_timeout_at_the_shared_deadline_does_not_request_more_turns(self):
        f = Fixture()
        f.turn_pages[0]['data'].extend(
            [turn('older', completed=149), turn('oldest', completed=148)]
        )
        rpc = f.factory()
        request = rpc.request
        now = [0.0]

        def interrupted(method, params, **options):
            if method == 'thread/items/list':
                self.assertEqual(params['turnId'], 'turn-1')
                self.assertEqual(options['timeout'], 3.0)
                now[0] = 3.0
                raise DotError('Deadline reached', kind='timeout')
            return request(method, params, **options)

        rpc.request = interrupted
        result = WorkReportReader(lambda: rpc, clock=lambda: now[0]).read(
            self.source, ReportWindow(100, 200), deadline=3.0
        )
        self.assertEqual(result['items'], [])
        self.assertEqual(result['unconfirmed_final_answer'], 3)
        self.assertFalse(result['empty_confirmed'])
        self.assertTrue(rpc.closed)

    def test_revocation_during_answer_read_still_aborts(self):
        f = Fixture()
        f.item_pages['turn-1'][0]['nextCursor'] = '1'
        current = [True]
        rpc = f.factory()
        request = rpc.request

        def interrupted(method, params, **options):
            if method == 'thread/items/list' and 'cursor' in params:
                current[0] = False
                raise DotError('Disconnected', kind='transport_or_io')
            return request(method, params, **options)

        rpc.request = interrupted
        with self.assertRaises(Cancelled):
            WorkReportReader(lambda: rpc).read(
                self.source, ReportWindow(100, 200), is_current=lambda: current[0]
            )
        self.assertTrue(rpc.closed)

    def test_tools_reasoning_commentary_and_partial_answers_are_not_narration(self):
        f = Fixture()
        values = [
            dict(id='reason', type='reasoning', text='PRIVATE_REASON'),
            dict(id='comment', type='agentMessage', phase='commentary', text='PRIVATE_COMMENT'),
            dict(id='partial', type='agentMessage', phase='partial_answer', text='PRIVATE_PARTIAL'),
            dict(
                id='cmd',
                type='commandExecution',
                status='completed',
                exitCode=0,
                aggregatedOutput='PRIVATE_OUTPUT',
            ),
            dict(id='cmd-bool', type='commandExecution', status='completed', exitCode=False),
            dict(id='cmd-fail', type='commandExecution', status='failed'),
            dict(id='cmd-exit', type='commandExecution', status='completed', exitCode=2),
            dict(id='patch', type='fileChange', status='completed', changes=['PRIVATE_DIFF']),
            final(),
        ]
        f.item_pages['turn-1'][0]['data'] = [dict(turnId='turn-1', item=v) for v in values]
        r = self.report(f)
        self.assertNotIn('PRIVATE', str(r))
        self.assertEqual(
            r['items'][0]['evidence'],
            dict(
                command_successes=1,
                command_failures=2,
                file_change_successes=1,
                file_change_failures=0,
                unconfirmed_tool_items=1,
            ),
        )
        self.assertFalse(r['coverage_complete'])
        self.assertFalse(r['original_chat_modified'])
        self.assertFalse(r['history_persisted'])

    def test_direct_test_processes_derive_from_owned_command_and_exit_receipts(self):
        f = Fixture()
        rows = (
            ('python3 -m unittest discover -s tests -q', 'completed', 0),
            ('.venv/bin/pytest tests/test_suite.py::test_case -q', 'completed', 1),
            ('pytest -q', 'failed', 2),
            ('pytest -q', 'declined', None),
            ('pytest -q', 'completed', False),
            ('pytest -q', 'failed', 0),
            ('pytest --collect-only', 'completed', 0),
            ('pytest -h', 'completed', 0),
            ('pytest -q || true', 'completed', 0),
            ('sh -c pytest', 'completed', 0),
            ('echo pytest', 'completed', 0),
            ('python -m unittest --help', 'completed', 0),
            ('python3 tools/test_all.py', 'completed', 0),
            ('/tmp/dev-venv/bin/python3.13 ./tools/test_all.py --suite host', 'failed', 1),
            ('python -m tools.test_all --suite installer --suite java', 'completed', 0),
            ('python -m tools.test_all --suite tooling', 'inProgress', None),
            ('python tools/test_all.py --help', 'completed', 0),
            ('python tools/test_all.py --suite', 'completed', 0),
            ('python tools/test_all.py --suite fake', 'completed', 0),
            ('tools.test_all', 'completed', 0),
            ('python tools/test_all.py && echo success', 'completed', 0),
        )
        f.item_pages['turn-1'][0]['data'].extend(
            dict(
                turnId='turn-1',
                item=dict(
                    id=f'command-{i}',
                    type='commandExecution',
                    command=cmd,
                    status=state,
                    exitCode=code,
                    aggregatedOutput='PRIVATE_OUTPUT',
                ),
            )
            for i, (cmd, state, code) in enumerate(rows)
        )
        item = self.report(f)['items'][0]
        self.assertEqual(item['test_processes'], dict(exit_zero=3, exit_nonzero=3, unconfirmed=4))
        self.assertEqual(
            [row['item_id'] for row in item['test_receipts']],
            [f'command-{i}' for i in (1, 2, 13, 3, 4, 5, 15, 0)],
        )
        self.assertNotIn('PRIVATE_OUTPUT', str(item))
        self.assertNotIn('test_suite.py', str(item))
        self.assertNotIn('tools/test_all.py', str(item))
        self.assertEqual(item['verification'], 'not_assessed')

    def test_unreadable_final_does_not_erase_other_results_or_select_an_older_answer(self):
        for invalid in ('\ud800', True, '长' * 64001, '', '\x01 \n'):
            with self.subTest(
                text_type=type(invalid), length=len(invalid) if isinstance(invalid, str) else None
            ):
                f = Fixture()
                f.turn_pages[0]['data'].extend(
                    [turn('older', completed=149), turn('failed', status='failed')]
                )
                f.item_pages['turn-1'][0]['data'] = [
                    dict(turnId='turn-1', item=final('latest', invalid)),
                    dict(turnId='turn-1', item=final('previous', '过时答复')),
                ]
                f.item_pages['older'] = [
                    dict(
                        data=[dict(turnId='older', item=final(text='有效😀答复'))], nextCursor=None
                    )
                ]
                report = self.report(f)
                self.assertEqual([row['turn_id'] for row in report['items']], ['older'])
                self.assertEqual(report['items'][0]['excerpt'], '有效😀答复')
                self.assertEqual(report['failed_turns'], 1)
                self.assertEqual(report['unconfirmed_final_answer'], 1)
                self.assertEqual(report['missing_final_answer'], 0)
                self.assertFalse(report['coverage_complete'])
                self.assertFalse(report['empty_confirmed'])
                rendered = ''.join(text_pages(report_text(report)))
                self.assertIn('有效😀答复', rendered)
                self.assertNotIn('过时答复', rendered)

    def test_no_final_answer_is_missing_evidence_instead_of_empty_success(self):
        f = Fixture()
        f.item_pages['turn-1'][0]['data'] = [
            dict(
                turnId='turn-1',
                item=dict(id='partial', type='agentMessage', phase='partial_answer'),
            )
        ]
        r = self.report(f)
        self.assertEqual(r['missing_final_answer'], 1)
        self.assertEqual(r['unconfirmed_final_answer'], 0)
        self.assertFalse(r['empty_confirmed'])

    def test_bounded_answer_pages_distinguish_absent_from_unconfirmed(self):
        f = Fixture()
        f.turn_pages[0]['data'].append(turn('complete-read', completed=149))
        f.item_pages = {
            'turn-1': [dict(data=[], nextCursor='1'), dict(data=[], nextCursor='2')],
            'complete-read': [dict(data=[], nextCursor=None)],
        }
        r = self.report(f)
        self.assertEqual(r['missing_final_answer'], 1)
        self.assertEqual(r['unconfirmed_final_answer'], 1)
        self.assertEqual(r['items'], [])
        self.assertFalse(r['coverage_complete'])
        self.assertFalse(r['empty_confirmed'])

    def test_answer_read_limit_counts_only_known_in_window_omissions(self):
        f = Fixture()
        read_count = 20
        f.turn_pages[0]['data'] = [
            turn(f'turn-{i}', completed=190 - i) for i in range(read_count + 1)
        ] + [turn('after-window', completed=200)]
        f.item_pages = {
            f'turn-{i}': [dict(data=[dict(turnId=f'turn-{i}', item=final())], nextCursor=None)]
            for i in range(read_count)
        }
        r = self.report(f)
        self.assertEqual(len(r['items']), read_count)
        self.assertEqual(r['unconfirmed_final_answer'], 1)
        self.assertEqual(r['missing_final_answer'], 0)
        self.assertFalse(r['coverage_complete'])

    def test_oversized_excerpt_is_marked_and_tool_evidence_pages_are_bounded(self):
        f = Fixture()
        f.item_pages['turn-1'] = [
            dict(data=[dict(turnId='turn-1', item=final(text='文' * 700))], nextCursor='1'),
            dict(data=[], nextCursor='2'),
        ]
        r = self.report(f)
        self.assertEqual(len(r['items'][0]['excerpt']), 500)
        self.assertTrue(r['items'][0]['excerpt_truncated'])
        self.assertFalse(r['items'][0]['evidence_complete'])
        self.assertFalse(r['coverage_complete'])

    def test_rogue_turn_item_or_repeated_cursor_fails_closed(self):
        for mode in ('wrong_turn', 'repeat_cursor', 'contradictory_turn'):
            f = Fixture()
            if mode == 'wrong_turn':
                f.item_pages['turn-1'][0]['data'][0]['turnId'] = 'other-chat-turn'
            elif mode == 'repeat_cursor':
                f.turn_pages = [dict(data=[], nextCursor='1'), dict(data=[], nextCursor='1')]
            else:
                f.turn_pages[0]['data'].append(turn(completed=151))
            with self.assertRaises(DotError):
                self.report(f)
            self.assertTrue(all(c.closed for c in f.clients))

    def test_cancellation_before_start_and_after_reply_never_reads_next_page(self):
        f = Fixture()
        with self.assertRaises(Cancelled):
            self.report(f, is_current=lambda: False)
        self.assertFalse(f.clients[0].started)
        self.assertTrue(f.clients[0].closed)
        f = Fixture()
        current = [True]
        f.after_request = lambda: current.__setitem__(0, False)
        with self.assertRaises(Cancelled):
            self.report(f, is_current=lambda: current[0])
        self.assertEqual(len(f.calls), 1)
        self.assertTrue(f.clients[0].closed)

    def test_source_and_windows_reject_unselected_or_invalid_inputs(self):
        f = Fixture()
        with self.assertRaises(ValueError):
            WorkReportReader(f.factory).read(None, ReportWindow(100, 200))
        self.assertEqual(f.clients, [])
        for start, end in ((True, 2), (2, 1), (0, 32 * 86400), (1, 2.0)):
            with self.assertRaises(ValueError):
                ReportWindow(start, end)

    def test_today_is_beijing_and_half_open_bounds_are_fixed(self):
        now = int(datetime(2026, 11, 9, 0, 30, tzinfo=timezone.utc).timestamp())
        today = ReportWindow.period('today', now)
        self.assertEqual(
            today.start, int(datetime(2026, 11, 8, 16, tzinfo=timezone.utc).timestamp())
        )
        self.assertEqual(today.end, now)
        self.assertEqual(ReportWindow.period('half_hour', now).start, now - 1800)
        self.assertEqual(ReportWindow.period('two_hours', now).start, now - 7200)

    def test_previous_calendar_windows_use_beijing_midnight_and_monday(self):
        def utc(year, month, day, hour):
            return int(datetime(year, month, day, hour, tzinfo=timezone.utc).timestamp())

        # 16:00 UTC on Sunday is already Monday in Beijing.
        now = utc(2026, 11, 8, 16)
        self.assertEqual(
            ReportWindow.period('yesterday', now), ReportWindow(utc(2026, 11, 7, 16), now)
        )
        self.assertEqual(
            ReportWindow.period('last_week', now), ReportWindow(utc(2026, 11, 1, 16), now)
        )
        self.assertEqual(
            ReportWindow.period('last_week', utc(2027, 1, 4, 2)),
            ReportWindow(utc(2026, 12, 27, 16), utc(2027, 1, 3, 16)),
        )

    def test_card_limit_keeps_coverage_partial_instead_of_hiding_older_results(self):
        f = Fixture()
        f.turn_pages[0]['data'] = [turn('turn-' + str(i)) for i in range(25)]
        f.item_pages = {
            r['id']: [dict(data=[dict(turnId=r['id'], item=final())], nextCursor=None)]
            for r in f.turn_pages[0]['data']
        }
        r = self.report(f)
        self.assertEqual(len(r['items']), 20)
        self.assertFalse(r['coverage_complete'])
        self.assertEqual(len(f.calls), 21)

    def test_changed_duplicate_item_is_not_a_second_or_trusted_receipt(self):
        f = Fixture()
        f.item_pages['turn-1'][0]['data'].append(dict(turnId='turn-1', item=final(text='changed')))
        with self.assertRaises(DotError):
            self.report(f)
        self.assertTrue(f.clients[0].closed)

    def test_total_deadline_is_checked_before_another_page(self):
        f = Fixture()
        now = [0.0]
        f.after_request = lambda: now.__setitem__(0, 31.0)
        f.turn_pages[0]['nextCursor'] = '1'
        result = WorkReportReader(f.factory, clock=lambda: now[0]).read(
            self.source, ReportWindow(100, 200)
        )
        self.assertEqual(result['unconfirmed_final_answer'], 1)
        self.assertFalse(result['coverage_complete'])
        self.assertEqual(len(f.calls), 1)
        self.assertTrue(f.clients[0].closed)


class ReportControllerTests(unittest.TestCase):
    def harness(self):
        import test_dot_handoff as existing

        helper = existing.ControllerTests()
        return helper, helper.session()

    def test_report_needs_selected_source_and_valid_period_before_reading(self):
        from protocol import validate_device_event

        helper, b = self.harness()
        helper.request(b, 'report', period='today')
        self.assertFalse(b.events[-1][1]['ok'])
        self.assertIsNone(b.chats.worker)
        for period in ('', None, True, 'all_history'):
            with self.assertRaises(ValueError):
                validate_device_event(
                    dict(
                        event='dot_chat_request',
                        request_id='a' * 32,
                        action='report',
                        period=period,
                    )
                )

    def test_report_dispatch_uses_current_selection_and_never_changes_brain_or_capture(self):
        helper, b = self.harness()
        selected = Handoff('selected-chat', '明确来源', (), False)
        b.chats.selection = selected
        report = WorkReportReader(Fixture().factory).read(selected, ReportWindow(100, 200))
        with patch.object(b.chats.catalog, 'report', return_value=report) as read:
            helper.request(b, 'report', period='yesterday', source_id='unrelated-source')
            helper.join(b)
        self.assertIs(read.call_args.args[0], selected)
        self.assertEqual(read.call_args.args[1], 'yesterday')
        self.assertTrue(b.events[-1][1]['ok'])
        self.assertIs(b.events[-1][1]['work_report_calendar_supported'], True)
        self.assertEqual(b.events[-1][1]['action'], 'report')
        self.assertFalse(b.session.active)
        self.assertIsNone(b.brain.dot.handoff)

    def test_disconnect_or_cancel_drops_delayed_report_without_a_late_ui_event(self):
        for mode in ('cancel', 'disconnect'):
            helper, b = self.harness()
            b.chats.selection = Handoff('selected-chat', '明确来源', (), False)
            entered, release = threading.Event(), threading.Event()

            def read(*args, **kwargs):
                entered.set()
                self.assertTrue(release.wait(2))
                return {'items': [{'excerpt': 'STALE_PRIVATE_REPORT'}]}

            with patch.object(b.chats.catalog, 'report', side_effect=read):
                helper.request(b, 'report', period='today')
                self.assertTrue(entered.wait(2))
                if mode == 'cancel':
                    b.chats.cancel('a' * 32)
                else:
                    b.session.connection = object()
                release.set()
                helper.join(b)
            self.assertNotIn('STALE_PRIVATE_REPORT', str(b.events))


if __name__ == '__main__':
    unittest.main()
