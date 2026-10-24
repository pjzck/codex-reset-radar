import unittest
from dataclasses import replace
from datetime import datetime, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfoNotFoundError

from radar.content import chinese_summary, post_created_at, time_labels
from radar.notifications import format_message
from radar.sources import Event


POST_URL = "https://x.com/thsottiaux/status/2113492502355562875"
RECORDED = datetime(2026, 10, 23, 18, tzinfo=timezone.utc)


def record(text, phase="scheduled", **values):
    fields = dict(source="com", occurred_at=RECORDED, text=text, kind="regular", phase=phase,
                  url=POST_URL, evidence="synthetic test")
    fields.update(values)
    return Event(**fields)


class ResetTimeTests(unittest.TestCase):
    def test_tomorrow_is_anchored_to_original_post_in_stated_zone(self):
        event = record("Landing tomorrow before 10am PST together with a small ship.")
        self.assertEqual(post_created_at(event).strftime("%Y-%m-%d %H:%M"), "2026-10-23 04:47")
        expected, actual = time_labels([event])
        self.assertEqual(expected, "2026-10-24 02:00 前（北京时间）")
        self.assertEqual(actual, "待确认")

    def test_pst_pdt_and_pt_do_not_share_a_fixed_offset(self):
        cases = {
            "Reset scheduled for 2026-07-01 10:00 PST.": "2026-07-02 02:00（北京时间）",
            "Reset scheduled for 2026-07-01 10:00 PDT.": "2026-07-02 01:00（北京时间）",
            "Reset scheduled for 2026-07-01 10:00 PT.": "2026-07-02 01:00（北京时间）",
            "Reset scheduled for 2026-12-01 10:00 PT.": "2026-12-02 02:00（北京时间）",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(time_labels([record(text)])[0], expected)

    def test_ambiguous_or_nonexistent_dst_clock_stays_pending(self):
        for text in ("Reset scheduled for 2026-11-01 01:30 PT.", "Reset scheduled for 2026-03-08 02:30 PT."):
            with self.subTest(text=text):
                self.assertEqual(time_labels([record(text)])[0], "待确认")

    def test_missing_timezone_data_does_not_guess_pt(self):
        with patch("radar.content.ZoneInfo", side_effect=ZoneInfoNotFoundError):
            self.assertEqual(time_labels([record("Reset scheduled for 2026-07-01 10:00 PT.")])[0], "待确认")

    def test_actual_completion_time_is_taken_from_text_and_rolls_date(self):
        expected, actual = time_labels([record("The reset completed at 2026-10-23 17:00 UTC.", "confirmed")])
        self.assertEqual(expected, "待确认")
        self.assertEqual(actual, "2026-10-24 01:00（北京时间）")

    def test_completion_without_time_never_uses_post_or_tracker_time(self):
        self.assertEqual(time_labels([record("Done", "confirmed")]), ("待确认", "待确认"))
        message = format_message([record("Done", "confirmed")], collected_at=RECORDED)
        self.assertIn("🕘 实际重置时间：待确认", message)
        self.assertLess(message.index("实际重置时间："), message.index("参考来源与原文"))
        self.assertGreater(message.index("站点记录时间（非重置时间）"), message.index("参考来源与原文"))

    def test_relative_time_requires_original_post_anchor(self):
        plan = record("The reset will land in 3 hours.")
        self.assertEqual(time_labels([plan])[0], "2026-10-23 15:47（北京时间）")
        self.assertEqual(time_labels([replace(plan, url="")])[0], "待确认")
        self.assertEqual(time_labels([replace(plan, url="https://x.com/example/status/1234567890")])[0], "待确认")

    def test_actual_ago_uses_post_anchor_but_duration_is_not_execution_time(self):
        self.assertEqual(time_labels([record("The reset completed 3 hours ago.", "confirmed")])[1], "2026-10-23 09:47（北京时间）")
        self.assertEqual(time_labels([record("The reset completed in 3 hours.", "confirmed")])[1], "待确认")

    def test_unclear_clock_date_timezone_and_vague_time_stay_pending(self):
        for text in ("Reset tomorrow.", "Reset in the next few hours.", "Reset before 10am tomorrow.",
                     "Reset at 10am PST.", "Reset at 2026-10-23 10:00 CST.", "Reset by end of day.",
                     "Reset at 2026-02-30 10:00 UTC."):
            with self.subTest(text=text):
                self.assertEqual(time_labels([record(text)])[0], "待确认")

    def test_month_name_and_chinese_beijing_clock(self):
        self.assertEqual(time_labels([record("Reset before October 23, 2026 at 10am PST.")])[0], "2026-10-24 02:00 前（北京时间）")
        self.assertEqual(time_labels([record("计划于北京时间2026年10月24日凌晨2点重置额度。")])[0], "2026-10-24 02:00（北京时间）")

    def test_explicit_utc_offset_and_iso_format(self):
        for text, expected in (("Reset at 2026-10-23T17:00:00Z.", "2026-10-24 01:00（北京时间）"),
                               ("Reset at 2026-10-24 02:00 UTC+08:00.", "2026-10-24 02:00（北京时间）")):
            with self.subTest(text=text):
                self.assertEqual(time_labels([record(text)])[0], expected)

    def test_planned_and_actual_clauses_are_not_interchanged(self):
        event = record("Reset planned for 2026-10-23 17:00 UTC; the reset completed at 2026-10-23 16:30 UTC.", "confirmed")
        self.assertEqual(time_labels([event]), ("2026-10-24 01:00（北京时间）", "2026-10-24 00:30（北京时间）"))

    def test_release_time_is_not_reset_time(self):
        event = record("We shipped an update at 2026-10-23 17:00 UTC; the reset is done.", "confirmed")
        self.assertEqual(time_labels([event]), ("待确认", "待确认"))

    def test_conflicting_reset_times_are_not_silently_picked(self):
        first = record("Reset scheduled for 2026-10-23 17:00 UTC.")
        other = record("Reset scheduled for 2026-10-23 18:00 UTC.", source="org")
        self.assertIn("待确认", time_labels([first, other])[0])
        self.assertIn("不一致", time_labels([first, other])[0])

    def test_observation_record_does_not_establish_actual_reset_time(self):
        event = record("The reset completed at 2026-10-23 17:00 UTC.", "reported", source="org")
        self.assertEqual(time_labels([event])[1], "待确认")

    def test_future_actual_time_is_not_presented_as_completed(self):
        event = record("The reset completed at 2026-10-25 17:00 UTC.", "confirmed")
        message = format_message([event], collected_at=RECORDED)
        self.assertIn("实际重置时间：待确认（公告实际时间晚于采集时间）", message)


class ChineseSummaryTests(unittest.TestCase):
    def test_summary_captures_reset_audience_and_release_in_chinese(self):
        event = record("We will reset usage for all paid users together with a small update.")
        summary = chinese_summary([event], "scheduled")
        self.assertIn("预告", summary)
        self.assertIn("所有付费用户", summary)
        self.assertIn("小版本更新", summary)
        self.assertNotIn("We will", summary)

    def test_banked_quantity_and_named_plans_are_preserved(self):
        event = record("We will give two banked resets to Plus, Pro and Business users.", kind="banked")
        summary = chinese_summary([event], "scheduled")
        self.assertIn("2 次备用重置机会", summary)
        self.assertIn("Plus、Pro、Business", summary)

    def test_unrelated_plan_names_do_not_become_eligibility_claims(self):
        event = record("A Pro pricing change is coming. We will reset usage for all paid users.")
        self.assertNotIn("涉及 Pro", chinese_summary([event], "scheduled"))

    def test_source_observation_does_not_become_completion_in_summary(self):
        event = record("Codex usage limits have been reset.", "reported")
        self.assertIn("待核实", chinese_summary([event], "reported"))
        self.assertNotIn("公告确认", chinese_summary([event], "reported"))


if __name__ == "__main__":
    unittest.main()
