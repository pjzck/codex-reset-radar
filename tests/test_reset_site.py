import tempfile
import unittest
import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from radar.app import DEFAULTS, State, main, poll
from radar.notifications import format_message, notification_key
from radar.reset_site import fetch_reset_samples, parse_reset_forecast, reset_demo_messages


NOW = datetime(2026, 10, 24, 4, tzinfo=timezone.utc)
URL = "https://x.com/thsottiaux/status/2113492502355562875"


def observation():
    return {"updated_at": "2026-10-24T04:00:00Z", "latest_alert": {
        "id": "2113492502355562875", "kind": "reset", "state": "confirmed",
        "source_at": "2026-10-23T17:40:51Z", "summary": "Reset observed; awaiting Tibo's confirmation.",
        "confirmation_source": "operator_observed", "observed_at": "2026-10-23T17:40:51Z",
        "url": URL, "observation_message": "Codex 重置已确认。\n我们的检查显示，Codex 用量额度已于 10月23日 17:40 UTC 左右重置。\nTibo 尚未发布确认消息。他发布后，我们可能会更新这条消息。"}}


def announcement():
    return {"updated_at": "2026-10-24T04:00:00Z", "latest_alert": {
        "id": "2113492502355562875", "kind": "watch", "state": "active",
        "source_at": "2026-10-23T04:47:34Z", "summary": "Landing tomorrow before 10am PST together with a small ship.",
        "url": URL, "window": {"target_kind": "deadline", "target_at": "2026-10-23T17:00:00Z"}}}


class SingleSiteTests(unittest.TestCase):
    def test_default_is_only_codex_reset_com(self):
        self.assertEqual(DEFAULTS["sources"], ["reset"])

    def test_website_observation_is_completed_without_claiming_tibo_confirmation(self):
        event = parse_reset_forecast(observation())[0]
        self.assertEqual(event.phase, "confirmed")
        message = format_message([event])
        self.assertTrue(message.startswith("Codex 已重置\n类型：常规重置"))
        self.assertIn("观测时间：2026-10-24 01:40:51（北京时间）", message)
        self.assertIn("2026-10-24 01:40（北京时间） 左右重置", message)
        self.assertIn("Tibo 尚未发布确认消息", message)
        self.assertIn("消息来源：网站观测", message)
        self.assertIn("网站：https://codex-reset.com/", message)
        self.assertNotIn("交叉核对", message)
        self.assertNotIn("待确认", message)
        self.assertNotIn("17:40 UTC", message)

    def test_announcement_uses_website_deadline_not_a_second_timezone_guess(self):
        event = parse_reset_forecast(announcement())[0]
        message = format_message([event])
        self.assertTrue(message.startswith("Codex 已安排重置"))
        self.assertIn("公告时间：2026-10-23 12:47:34（北京时间）", message)
        self.assertIn("预计最晚：2026-10-24 01:00:00（北京时间）", message)
        self.assertIn("状态：等待网站确认执行", message)
        self.assertIn("公告原文：\nLanding tomorrow before 10am PST", message)
        self.assertIn("消息来源：Tibo（@thsottiaux）", message)

    def test_expired_watch_never_becomes_reset_and_does_not_repeat_on_expiry(self):
        first = announcement()
        expired = deepcopy(first)
        expired["latest_alert"]["state"] = "expired"
        planned, later = parse_reset_forecast(first)[0], parse_reset_forecast(expired)[0]
        self.assertEqual(later.phase, "scheduled")
        self.assertEqual(notification_key([planned]), notification_key([later]))

    def test_observation_and_later_tibo_confirmation_are_distinct_deliveries(self):
        first = observation()
        later = deepcopy(first)
        later["latest_alert"]["confirmation_url"] = "https://x.com/thsottiaux/status/2113492502355562888"
        later["latest_alert"]["confirmed_at"] = "2026-10-23T18:00:00Z"
        later["latest_alert"]["observation_message"] = "Codex 重置已确认。Tibo 已发布确认消息。"
        before, after = parse_reset_forecast(first)[0], parse_reset_forecast(later)[0]
        self.assertEqual(before.identity, after.identity)
        self.assertNotEqual(notification_key([before]), notification_key([after]))
        self.assertIn("消息来源：网站观测，Tibo 已确认", format_message([after]))

    def test_model_probability_or_weak_hint_does_not_announce_a_reset(self):
        self.assertEqual(parse_reset_forecast({"updated_at": "2026-10-24T04:00:00Z", "latest_alert": None,
                                              "probabilities": {"model_24h": 99}}), [])
        weak = announcement()
        weak["latest_alert"]["summary"] = "Who needs a reset?"
        weak["latest_alert"]["window"] = None
        self.assertEqual(parse_reset_forecast(weak), [])

    def test_malformed_source_does_not_create_an_observation(self):
        for body in ({}, {"latest_alert": None}, {**observation(), "updated_at": "invalid"}):
            with self.subTest(body=body), self.assertRaises(ValueError):
                parse_reset_forecast(body)
        missing = observation()
        del missing["latest_alert"]["observed_at"]
        with self.assertRaises(ValueError):
            parse_reset_forecast(missing)

    def test_only_two_compact_demo_messages_are_generated(self):
        messages = reset_demo_messages()
        self.assertEqual(len(messages), 2)
        self.assertTrue(messages[0].startswith("Codex 已安排重置"))
        self.assertTrue(messages[1].startswith("Codex 已重置"))
        self.assertTrue(all("格式样例" in message for message in messages))

    @patch("radar.reset_site.fetch_reset", return_value=parse_reset_forecast(observation()))
    def test_real_samples_replay_the_archived_plan_without_claiming_it_is_current(self, _):
        payload = {"events": [{"id": "historical-plan", "type": "reset", "confirmed": True,
                               "preview": False, "announced_at": "2026-08-13T01:01:37Z",
                               "summary": "Global reset within an hour.", "url": URL,
                               "official_window": {"start_at": "2026-08-13T01:01:37Z", "end_at": "2026-08-13T02:01:37Z"}}]}
        with patch("radar.reset_site.request", return_value=json.dumps(payload)):
            messages = fetch_reset_samples()
        self.assertEqual(len(messages), 2)
        self.assertIn("历史预告回放，非当前状态", messages[0])
        self.assertIn("历史格式样例，非新增通知", messages[1])


class SingleSiteStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.state = State(str(Path(self.temp.name) / "state.sqlite3"))
        self.config = dict(DEFAULTS)
        self.messages = []

    def tearDown(self):
        self.state.close()
        self.temp.cleanup()

    def run_events(self, events):
        return poll(self.config, self.state, self.messages.append, lambda source, timeout: events, NOW)

    def test_empty_first_poll_does_not_lose_the_first_real_announcement(self):
        self.config["first_run"] = "silent"
        self.assertEqual(self.run_events([]), 0)
        self.assertTrue(self.state.initialized("reset"))
        self.run_events(parse_reset_forecast(announcement()))
        self.assertEqual(len(self.messages), 1)

    def test_preview_then_observation_sends_only_two_and_ignores_stale_watch(self):
        self.run_events(parse_reset_forecast(announcement()))
        self.run_events(parse_reset_forecast(observation()))
        self.run_events(parse_reset_forecast(announcement()))
        self.run_events(parse_reset_forecast(observation()))
        self.assertEqual(len(self.messages), 2)
        self.assertTrue(self.messages[-1].startswith("Codex 已重置"))

    def test_new_source_silent_migration_does_not_replay_history(self):
        self.config["first_run"] = "silent"
        self.run_events(parse_reset_forecast(observation()))
        self.run_events(parse_reset_forecast(observation()))
        self.assertEqual(self.messages, [])

    @patch("radar.app.State")
    @patch("radar.app.send_feishu")
    @patch("radar.app.fetch_reset_samples", return_value=["Codex 已安排重置\n历史样例", "Codex 已重置\n历史样例"])
    @patch.dict("os.environ", {"FEISHU_WEBHOOK_URL": "https://open.feishu.cn/open-apis/bot/v2/hook/test-placeholder"})
    def test_real_sample_cli_sends_two_individually_without_opening_state(self, fetch_mock, send_mock, state_mock):
        with patch("radar.app.logging.basicConfig"), patch("radar.app.time.sleep"):
            self.assertEqual(main(["test-message", "--real-samples"]), 0)
        self.assertEqual(send_mock.call_count, 2)
        fetch_mock.assert_called_once()
        state_mock.assert_not_called()
