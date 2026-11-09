"""Calendar bounds, late collection and actual receipt-owned report delivery."""

from datetime import datetime
import unittest
from report_time import ReportRange, ReportTimeError, parse_range, range_compound
from report_progress import (
    ReportProgress,
    ReportCapacityError,
    DeliveryPlan,
    DeliveryToken,
    key,
    version,
)
from test_report_voice import SOURCE, report
from work_reports import BEIJING, ReportWindow


def stamp(day, hour=0, minute=0):
    return int(datetime(2026, 11, day, hour, minute, tzinfo=BEIJING).timestamp())


class RangeTests(unittest.TestCase):
    now = stamp(9, 20)

    def window(self, text):
        value = parse_range(text)
        self.assertIsInstance(value, ReportRange)
        return value.resolve(self.now)

    def test_explicit_24h_and_chinese_morning_afternoon(self):
        for text, expected in (
            ('汇报今天08:30到10:00的工作', ReportWindow(stamp(9, 8, 30), stamp(9, 10))),
            (
                '请帮我汇报从今天上午八点半到下午两点的工作成果',
                ReportWindow(stamp(9, 8, 30), stamp(9, 14)),
            ),
            (
                '查看今天从上午8:15至上午10点三刻的工作',
                ReportWindow(stamp(9, 8, 15), stamp(9, 10, 45)),
            ),
        ):
            self.assertEqual(self.window(text), expected)

    def test_cross_day_requires_explicit_day_and_does_not_assume_rollover(self):
        self.assertEqual(
            self.window('汇报昨天23:00到今天01:00的工作'), ReportWindow(stamp(8, 23), stamp(9, 1))
        )
        with self.assertRaisesRegex(ReportTimeError, '跨日'):
            self.window('汇报昨天23:00到01:00的工作')

    def test_absolute_dates_and_seconds_keep_exact_unix_seconds(self):
        result = self.window('汇报2026-11-08T23:00:01至2026年11月9日01:30:02的工作')
        self.assertEqual((result.start, result.end), (stamp(8, 23) + 1, stamp(9, 1, 30) + 2))
        self.assertEqual(
            self.window('汇报2026/11/09 08:00至2026/11/09 10:00的工作'.replace(' ', '')),
            ReportWindow(stamp(9, 8), stamp(9, 10)),
        )

    def test_midnight_24_is_explicit_next_day(self):
        self.assertEqual(
            self.window('汇报昨天23:00到昨天24:00的工作'), ReportWindow(stamp(8, 23), stamp(9))
        )
        with self.assertRaises(ReportTimeError):
            self.window('汇报昨天23:00到昨天晚上十二点的工作')

    def test_ambiguous_chinese_clock_and_missing_date_or_year_are_not_inferred(self):
        for text in (
            '汇报今天八点到九点的工作',
            '汇报08:00到10:00的工作',
            '汇报11月9日08:00到10:00的工作',
            '汇报今天上午12点到下午2点的工作',
        ):
            with self.subTest(text=text), self.assertRaises(ReportTimeError):
                self.window(text)

    def test_future_reversed_equal_invalid_calendar_and_oversized_window(self):
        for text in (
            '汇报今天20:00到21:00的工作',
            '汇报今天10:00到08:00的工作',
            '汇报今天08:00到08:00的工作',
            '汇报2026年2月29日08:00到10:00的工作',
            '汇报2026-09-01T00:00到2026-11-08T00:00的工作',
            '汇报今天08:61到10:00的工作',
        ):
            with self.subTest(text=text), self.assertRaises(ReportTimeError):
                self.window(text)

    def test_relative_day_is_beijing_even_when_utc_date_differs(self):
        now = int(datetime.fromisoformat('2026-11-08T17:30:00+00:00').timestamp())
        self.assertEqual(
            ReportRange('今天00:00', '01:00').resolve(now), ReportWindow(stamp(9), stamp(9, 1))
        )

    def test_compound_actions_do_not_look_like_a_plain_read_query(self):
        self.assertTrue(range_compound('汇报今天08:00到10:00的工作，然后继续第二项'))
        self.assertIsNone(parse_range('汇报今天08:00到10:00的工作，然后继续第二项'))
        self.assertIsNone(parse_range('如何设计08:00到10:00的工作汇报'))


class ProgressTests(unittest.TestCase):
    def source_report(self, *texts, end=200):
        result = report(*texts)
        result['window_end'] = end
        return result

    def confirm(self, progress, item):
        progress.confirm(key(item), version(item))

    def test_only_confirmed_version_is_excluded_and_unheard_items_remain(self):
        initial = self.source_report('甲', '乙', '丙')
        progress = ReportProgress(SOURCE, initial)
        self.confirm(progress, initial['items'][0])
        newer = self.source_report('甲', '乙', '丙', end=300)
        result, view = progress.merge(newer)
        self.assertEqual([x['excerpt'] for x in view['items']], ['乙', '丙'])
        self.assertEqual(view['delivered_items_skipped'], 1)
        self.assertEqual(result.status()['pending'], 2)
        self.assertEqual(progress.end, 200)

    def test_late_collected_old_completion_is_not_lost_by_end_cursor(self):
        initial = self.source_report('已播')
        progress = ReportProgress(SOURCE, initial)
        self.confirm(progress, initial['items'][0])
        newer = self.source_report('已播', '迟到结果', end=300)
        result, view = progress.merge(newer)
        self.assertEqual(view['items'][0]['since_status'], 'late_collected')
        self.assertEqual(view['since_last_end'], 200)
        self.assertEqual(result.start, initial['window_start'])

    def test_cached_unheard_is_retained_and_labeled_even_if_latest_page_omits_it(self):
        initial = self.source_report('甲', '尚未播完')
        progress = ReportProgress(SOURCE, initial)
        self.confirm(progress, initial['items'][0])
        _, view = progress.merge(self.source_report('甲', end=300))
        self.assertEqual(view['items'][0]['excerpt'], '尚未播完')
        self.assertEqual(view['items'][0]['since_status'], 'cached_unheard')
        self.assertFalse(view['coverage_complete'])

    def test_changed_final_text_becomes_pending_again_without_mutating_prior_snapshot(self):
        initial = self.source_report('旧摘录')
        progress = ReportProgress(SOURCE, initial)
        self.confirm(progress, initial['items'][0])
        result, view = progress.merge(self.source_report('新摘录', end=300))
        self.assertEqual(view['items'][0]['since_status'], 'updated')
        self.assertEqual(progress.status()['summary_delivered'], 1)
        self.assertEqual(result.status()['pending'], 1)
        self.assertEqual(progress.entries[key(initial['items'][0])].item['excerpt'], '旧摘录')

    def test_empty_remaining_is_different_from_zero_source_results(self):
        initial = self.source_report('已播')
        progress = ReportProgress(SOURCE, initial)
        self.confirm(progress, initial['items'][0])
        _, view = progress.merge(self.source_report('已播', end=300))
        self.assertEqual(view['source_completed_items_read'], 1)
        self.assertTrue(view['empty_confirmed'])
        self.assertEqual(view['items'], [])
        newer = self.source_report('已播', end=300)
        newer.update(coverage_complete=False, empty_confirmed=False)
        self.assertFalse(progress.merge(newer)[1]['empty_confirmed'])

    def test_capacity_failure_preserves_original_progress_and_pending(self):
        from unittest.mock import patch

        initial = self.source_report('未播')
        progress = ReportProgress(SOURCE, initial)
        with patch('report_progress.MAX_RECORDS', 1), self.assertRaises(ReportCapacityError):
            progress.merge(self.source_report('未播', '新结果', end=300))
        self.assertEqual(progress.end, 200)
        self.assertEqual(progress.status()['pending'], 1)

    def test_detail_requires_all_owned_clauses_and_duplicate_or_forged_tokens_do_not_advance(self):
        initial = self.source_report('未播')
        progress = ReportProgress(SOURCE, initial)
        item = initial['items'][0]
        plan = DeliveryPlan(progress, {(key(item), version(item)): {0, 1, 2}})
        plan.confirm(plan.token(0), progress)
        plan.confirm(plan.token(0), progress)
        plan.confirm(DeliveryToken('wrong', 1), progress)
        self.assertEqual(progress.status()['pending'], 1)
        plan.confirm(plan.token(1), progress)
        plan.confirm(plan.token(2), progress)
        self.assertEqual(progress.status()['summary_delivered'], 1)

    def test_old_plan_cannot_mark_new_baseline_or_updated_version_delivered(self):
        initial = self.source_report('旧')
        progress = ReportProgress(SOURCE, initial)
        item = initial['items'][0]
        plan = DeliveryPlan(progress, {(key(item), version(item)): {0}})
        new = ReportProgress(SOURCE, self.source_report('新'))
        plan.confirm(plan.token(0), new)
        self.assertEqual(new.status()['pending'], 1)
        updated, _ = progress.merge(self.source_report('新', end=300))
        plan.confirm(plan.token(0), updated)
        self.assertEqual(updated.status()['pending'], 1)


if __name__ == '__main__':
    unittest.main()
