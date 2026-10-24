import base64
import hashlib
import hmac
import json
import tempfile
import unittest
from html import escape
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from radar.app import DEFAULTS, State, format_message, main, poll
from radar.notifications import CATEGORY_LABELS, demo_messages, notification_key
from radar.sources import Event, execution_phase, parse_com, parse_org
from radar.transport import RequestError, send_feishu, validate_webhook

NOW = datetime(2026, 10, 24, 4, tzinfo=timezone.utc)
URL = "https://x.com/example/status/1234567890"


def event(source="com", **kwargs):
    values = dict(source=source, occurred_at=NOW - timedelta(hours=1), text="Example reset notice & details",
                  kind="regular", phase="confirmed", url=URL, evidence="[ CONFIRMED RESET ]")
    values.update(kwargs)
    return Event(**values)


def html(label="[ CONFIRMED RESET ]", text="Usage limits have been reset & applied.", at="2026-10-24T03:00:00.000Z"):
    return '''<div class="log-list"><div class="log-item">
      <span class="log-rel" data-datetime="%s">1h ago</span>
      <div class="log-event-status">%s</div>
      <div class="log-text"><span>%s</span></div>
      <a class="log-link" href="%s">Original</a></div></div>''' % (at, label, escape(text), URL)


def org_event(text, evidence="x_post", status=None):
    row = {"id": "observed-1234567890" if evidence == "observed" else "1234567890",
           "reset_type": "regular", "announced_at": "2026-10-24T03:00:00Z",
           "text": text, "source": {"type": evidence, "url": URL}}
    if status:
        row["status"] = status
    return parse_org({"data": [row]})[0]


class ParserTests(unittest.TestCase):
    def test_com_extracts_machine_timestamp_and_html_entities(self):
        parsed = parse_com(html())[0]
        self.assertEqual(parsed.occurred_at, NOW - timedelta(hours=1))
        self.assertEqual(parsed.text, "Usage limits have been reset & applied.")
        self.assertEqual(parsed.phase, "confirmed")

    def test_teaser_does_not_become_confirmed(self):
        self.assertEqual(parse_com(html("[ POSSIBLE RESET // UNCONFIRMED ]"))[0].phase, "possible")
        self.assertEqual(parse_com(html("[ SCHEDULED RESET ]"))[0].phase, "scheduled")

    def test_banked_and_operator_labels_are_preserved(self):
        self.assertEqual(parse_com(html("[ BANKED RESET // CONFIRMED ]"))[0].kind, "banked")
        self.assertIn("OPERATOR", parse_com(html("[ CONFIRMED RESET // OPERATOR CONFIRMED ]"))[0].evidence)

    def test_delivered_as_scheduled_does_not_prove_execution(self):
        parsed = parse_com(html("[ BANKED RESET // DELIVERED AS SCHEDULED ]", "A banked reset lands by end of day."))[0]
        self.assertEqual(parsed.phase, "scheduled")
        self.assertEqual(parsed.kind, "banked")

    def test_future_original_remains_preview_despite_site_confirmed_label(self):
        parsed = parse_com(html("[ CONFIRMED RESET // OPERATOR CONFIRMED ]", "Landing tomorrow before 10am PST."))[0]
        self.assertEqual(parsed.phase, "scheduled")
        self.assertIn("未来计划", parsed.basis)

    def test_observed_record_is_not_original_execution_confirmation(self):
        parsed = org_event("Codex usage limits have been reset.", evidence="observed")
        self.assertEqual(parsed.phase, "reported")
        self.assertIn("观测记录", parsed.basis)

    def test_org_does_not_assume_all_reset_feed_records_were_executed(self):
        self.assertEqual(org_event("We will reset usage tomorrow.").phase, "scheduled")
        self.assertEqual(org_event("Details to follow.").phase, "reported")
        self.assertEqual(org_event("Done").phase, "confirmed")

    def test_execution_wording_is_conservative(self):
        cases = {
            "Usage limits have been reset.": "confirmed",
            "Done and dusted": "confirmed",
            "Confirmed landed across all accounts.": "confirmed",
            "Reset all propagated. Enjoy.": "confirmed",
            "The reset has been processed.": "confirmed",
            "Loading a banked reset in everyone's paid accounts.": "executing",
            "Loading a banked reset in everyone's paid accounts. See you again tomorrow!": "executing",
            "We're resetting usage for all paid users.": "executing",
            "The reset will have been reset tomorrow.": "scheduled",
            "We have not yet been reset.": "reported",
            "Not all propagated.": "reported",
            "Reset has not landed.": "reported",
            "We will announce when usage limits have been reset.": "reported",
            "Reset? We are considering it.": "reported",
        }
        for text, phase in cases.items():
            with self.subTest(text=text):
                self.assertEqual(execution_phase(text)[0], phase)

    def test_schema_drift_and_empty_page_fail_closed(self):
        for body in ("<html>challenge</html>", html("[ UNKNOWN ]"), html().replace("log-rel", "changed").replace("data-datetime", "unknown")):
            with self.assertRaises(ValueError):
                parse_com(body)

    def test_org_and_com_share_original_post_identity(self):
        payload = {"data": [{"id": "observed-1234567890", "reset_type": "regular",
                             "announced_at": "2026-10-24T02:00:00Z", "text": "Site observed reset",
                             "source": {"type": "observed", "url": URL.replace("x.com", "twitter.com")}}]}
        parsed = parse_org(payload)[0]
        self.assertEqual(parsed.identity, parse_com(html())[0].identity)
        self.assertNotEqual(parsed.key, parse_com(html())[0].key)
        self.assertIn("观测", parsed.evidence)

    def test_org_unknown_schema_is_rejected(self):
        for payload in ({}, {"data": []}, {"data": [{"reset_type": "unknown"}]}):
            with self.assertRaises(ValueError):
                parse_org(payload)


class PollTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.path = str(Path(self.temp.name) / "state.sqlite3")
        self.state = State(self.path)
        self.config = dict(DEFAULTS)
        self.messages = []

    def tearDown(self):
        self.state.close()
        self.temp.cleanup()

    def run_poll(self, feeds, sender=None, now=NOW):
        def fetch(source, timeout):
            value = feeds[source]
            if isinstance(value, Exception):
                raise value
            return value
        return poll(self.config, self.state, sender or self.messages.append, fetch, now)

    def test_first_run_sends_latest_once_across_sources_and_restarts(self):
        old = event(occurred_at=NOW - timedelta(days=20), url=URL.replace("1234567890", "1234567880"))
        feeds = {"com": [event(), old], "org": [event("org")]}
        self.assertEqual(self.run_poll(feeds), 0)
        self.assertEqual(len(self.messages), 1)
        self.assertIn("codexresets.com", self.messages[0])
        self.assertIn("codex-resets.org", self.messages[0])
        self.state.close()
        self.state = State(self.path)
        self.run_poll(feeds)
        self.assertEqual(len(self.messages), 1)

    def test_failed_delivery_is_retried_without_consuming_event(self):
        feeds = {"com": [event()], "org": [event("org")]}
        def fail(_):
            raise RequestError("rejected")
        with self.assertLogs("radar", level="ERROR"):
            self.assertEqual(self.run_poll(feeds, fail), 1)
        self.assertFalse(self.state.seen(notification_key([event()])))
        self.run_poll(feeds)
        self.assertEqual(len(self.messages), 1)

    def test_one_source_failure_does_not_block_the_other(self):
        with self.assertLogs("radar", level="ERROR"):
            self.assertEqual(self.run_poll({"com": ValueError("unavailable"), "org": [event("org")]}), 1)
        self.assertEqual(len(self.messages), 1)
        self.assertFalse(self.state.initialized("com"))
        self.assertIn("校验暂不可用", self.messages[0])

    def test_silent_bootstrap_and_later_confirmation(self):
        self.config["first_run"] = "silent"
        possible = event(phase="possible")
        self.run_poll({"com": [possible], "org": [replace(possible, source="org")]})
        self.assertFalse(self.messages)
        self.run_poll({"com": [event()], "org": [event("org")]})
        self.assertEqual(len(self.messages), 1)

    def test_old_and_future_records_do_not_send(self):
        for offset in (timedelta(days=-10), timedelta(hours=2)):
            candidate = event(occurred_at=NOW + offset)
            self.run_poll({"com": [candidate], "org": [replace(candidate, source="org")]})
        self.assertFalse(self.messages)

    def test_possible_can_be_disabled(self):
        self.config["notify_possible"] = False
        self.run_poll({"com": [event(phase="possible")], "org": [event("org", phase="possible")]})
        self.assertFalse(self.messages)

    def test_timezone_and_source_disagreement_preserved(self):
        message = format_message([event(), event("org", occurred_at=NOW - timedelta(hours=2))])
        self.assertIn("11:00:00", message)
        self.assertIn("10:00:00", message)
        self.assertIn("UTC+8", message)

    def test_second_sender_cannot_use_same_state_database(self):
        with self.assertRaises(RuntimeError):
            State(self.path)

    def test_preview_does_not_turn_into_execution_when_time_passes(self):
        plan = parse_com(html(text="Landing tomorrow before 10am PST."))[0]
        observed = org_event("Codex usage limits have been reset.", evidence="observed")
        feeds = {"com": [plan], "org": [observed]}
        self.run_poll(feeds)
        self.assertTrue(self.messages[0].startswith("🕒 重置预告 | Codex Reset Radar"))
        self.run_poll(feeds, now=NOW + timedelta(days=2))
        self.assertEqual(len(self.messages), 1)
        completed = parse_com(html(text="Done"))[0]
        self.run_poll({"com": [completed], "org": [org_event("Done")]}, now=NOW + timedelta(days=2))
        self.assertEqual(len(self.messages), 2)
        self.assertIn("状态更新：重置预告 → 已执行", self.messages[-1])
        self.state.close()
        self.state = State(self.path)
        self.run_poll({"com": [completed], "org": [org_event("Done")]}, now=NOW + timedelta(days=2))
        self.assertEqual(len(self.messages), 2)

    def test_future_plan_time_is_not_a_reason_to_delay_preview(self):
        plan = event(phase="scheduled", occurred_at=NOW + timedelta(days=1))
        self.run_poll({"com": [plan], "org": [replace(plan, source="org")]})
        self.assertEqual(len(self.messages), 1)
        self.assertIn("重置预告", self.messages[0])

    def test_second_site_arriving_later_does_not_resend(self):
        old = event("org", occurred_at=NOW - timedelta(days=10), url=URL.replace("1234567890", "1234567880"))
        self.run_poll({"com": [event()], "org": [old]})
        self.assertIn("单源收录", self.messages[0])
        self.run_poll({"com": [event()], "org": [event("org")]})
        self.assertEqual(len(self.messages), 1)
        sources = {r[0] for r in self.state.db.execute("SELECT source FROM observations WHERE identity=?", (event().identity,))}
        self.assertEqual(sources, {"com", "org"})

    def test_other_source_baseline_cannot_consume_a_new_alert(self):
        newer = event("org", occurred_at=NOW, url=URL.replace("1234567890", "1234567899"))
        self.run_poll({"com": [event()], "org": [newer, event("org")]})
        self.assertEqual(len(self.messages), 2)
        self.assertTrue(any(URL in message for message in self.messages))
        self.run_poll({"com": [event()], "org": [newer, event("org")]})
        self.assertEqual(len(self.messages), 2)

    def test_source_disagreement_does_not_send_two_contradictory_notices(self):
        plan = event(phase="scheduled")
        feeds = {"com": [plan], "org": [event("org")]}
        self.run_poll(feeds)
        self.assertEqual(len(self.messages), 1)
        self.assertTrue(self.messages[0].startswith("⚠️"))
        self.assertIn("来源结论不一致，暂不判定为执行完成", self.messages[0])
        self.run_poll(feeds)
        self.assertEqual(len(self.messages), 1)
        self.run_poll({"com": [event()], "org": [event("org")]})
        self.assertEqual(len(self.messages), 2)
        self.assertTrue(self.messages[-1].startswith("✅"))

    def test_type_disagreement_is_visible(self):
        self.run_poll({"com": [event(kind="banked")], "org": [event("org")]})
        self.assertIn("类型待核实", self.messages[0])

    def test_legacy_confirmed_dedup_survives_new_classification(self):
        self.state.mark(event().key)
        self.run_poll({"com": [event(phase="scheduled")], "org": [event("org", phase="reported")]})
        self.assertFalse(self.messages)

    def test_legacy_false_confirmation_does_not_hide_later_real_execution(self):
        self.state.mark(event().key)
        self.run_poll({"com": [event(phase="scheduled")], "org": [event("org", phase="reported")]})
        self.assertFalse(self.messages)
        self.run_poll({"com": [event()], "org": [event("org")]})
        self.assertEqual(len(self.messages), 1)
        self.assertTrue(self.messages[0].startswith("✅"))

    def test_legacy_actual_completion_is_not_replayed_on_upgrade(self):
        self.state.mark(event().key)
        self.run_poll({"com": [event()], "org": [event("org")]})
        self.assertFalse(self.messages)

    def test_executing_and_completed_are_separate_notifications(self):
        running = event(phase="executing")
        self.run_poll({"com": [running], "org": [replace(running, source="org")]})
        self.assertTrue(self.messages[0].startswith("🔄"))
        self.run_poll({"com": [event()], "org": [event("org")]})
        self.assertEqual(len(self.messages), 2)
        self.assertIn("状态更新：正在执行 → 已执行", self.messages[-1])


class MessageTests(unittest.TestCase):
    def test_message_lists_collector_websites_separately_from_original_post(self):
        message = format_message([event(), event("org")], collected_at=NOW)
        self.assertIn("采集网站：Codex Resets（codexresets.com）", message)
        self.assertIn("网站链接：https://codexresets.com/#log-heading", message)
        self.assertIn("采集网站：Codex Resets Unofficial Tracker（codex-resets.org）", message)
        self.assertIn("网站链接：https://codex-resets.org/", message)
        self.assertIn("原始 X 公告：\n" + URL, message)
        self.assertIn("双源状态一致", message)

    def test_each_demo_category_is_a_separate_message_after_one_introduction(self):
        messages = demo_messages(collected_at=NOW)
        self.assertEqual(len(messages), 7)
        self.assertTrue(messages[0].startswith("🧪 验收说明"))
        self.assertIn("0 条历史实采，6 条合成补充", messages[0])
        for message in messages[1:]:
            self.assertEqual(message.count("【验收演示 · 合成样例，非真实通知】"), 1)
            self.assertEqual(message.count("摘要："), 1)
            self.assertNotIn("原始 X 公告：", message)
            self.assertIn("参考网站（合成占位）", message)
            self.assertNotIn("采集网站：", message)
        labels = {message.splitlines()[0].split(" | ")[0].split(" ", 1)[1] for message in messages[1:]}
        self.assertEqual(labels, set(CATEGORY_LABELS.values()))

    def test_real_demo_keeps_original_information_without_faking_missing_categories(self):
        messages = demo_messages([event(), event("org")], collected_at=NOW)
        self.assertIn("1 条历史实采，5 条合成补充", messages[0])
        confirmation = next(message for message in messages[1:] if "重置确认 |" in message.splitlines()[0])
        self.assertIn("历史实采，非新增通知", confirmation)
        self.assertIn("原始 X 公告：\n" + URL, confirmation)
        self.assertIn("采集网站：Codex Resets", confirmation)
        self.assertIn("双源状态一致", confirmation)

    @patch("radar.app.State")
    @patch("radar.app.send_feishu")
    @patch.dict("os.environ", {"FEISHU_WEBHOOK_URL": "https://open.feishu.cn/open-apis/bot/v2/hook/test-placeholder"})
    def test_test_message_never_writes_delivery_or_dedup_state(self, send_mock, state_mock):
        with patch("radar.app.logging.basicConfig"), patch("radar.app.time.sleep"):
            self.assertEqual(main(["test-message"]), 0)
        state_mock.assert_not_called()
        self.assertEqual(send_mock.call_count, 7)
        payloads = [call.args[1] for call in send_mock.call_args_list]
        self.assertIn("验收说明", payloads[0])
        self.assertTrue(all("非真实通知" in message for message in payloads[1:]))

    @patch("radar.app.State")
    @patch("radar.app.send_feishu")
    @patch("radar.app.fetch_source", side_effect=lambda source, timeout: [event(source)])
    @patch.dict("os.environ", {"FEISHU_WEBHOOK_URL": "https://open.feishu.cn/open-apis/bot/v2/hook/test-placeholder"})
    def test_real_sample_cli_fetches_sources_without_writing_production_state(self, fetch_mock, send_mock, state_mock):
        with patch("radar.app.logging.basicConfig"), patch("radar.app.time.sleep"):
            self.assertEqual(main(["test-message", "--real-samples"]), 0)
        self.assertEqual(fetch_mock.call_count, 2)
        self.assertEqual(send_mock.call_count, 7)
        state_mock.assert_not_called()
        self.assertIn("1 条历史实采", send_mock.call_args_list[0].args[1])

    @patch("radar.app.State")
    @patch("radar.app.send_feishu")
    @patch("radar.app.fetch_source", side_effect=RequestError("unavailable"))
    @patch.dict("os.environ", {"FEISHU_WEBHOOK_URL": "https://open.feishu.cn/open-apis/bot/v2/hook/test-placeholder"})
    def test_real_sample_failure_does_not_send_a_synthetic_batch_as_real(self, fetch_mock, send_mock, state_mock):
        with patch("radar.app.logging.basicConfig"), self.assertLogs("radar", level="ERROR"):
            self.assertEqual(main(["test-message", "--real-samples"]), 1)
        send_mock.assert_not_called()
        state_mock.assert_not_called()


class FeishuTests(unittest.TestCase):
    HOOK = "https://open.feishu.cn/open-apis/bot/v2/hook/test-placeholder"

    @patch("radar.transport.time.time", return_value=1700000000)
    @patch("radar.transport.request", return_value='{"code":0}')
    def test_feishu_signature_matches_documented_protocol(self, request_mock, _):
        send_feishu(self.HOOK, "Codex Reset Radar 测试", "example-secret")
        payload = request_mock.call_args.args[1]
        expected = base64.b64encode(hmac.new(b"1700000000\nexample-secret", b"", hashlib.sha256).digest()).decode()
        self.assertEqual(payload["sign"], expected)
        self.assertEqual(payload["timestamp"], "1700000000")

    def test_rejected_or_malformed_response_is_not_a_success(self):
        for response in ('{"code":19024}', '{}', '[]', 'not json'):
            with patch("radar.transport.request", return_value=response), self.assertRaises(RequestError):
                send_feishu(self.HOOK, "test")

    def test_webhook_cannot_point_to_arbitrary_hosts(self):
        for url in ("http://open.feishu.cn/open-apis/bot/v2/hook/x", "https://evil.invalid/open-apis/bot/v2/hook/x", self.HOOK + "?secret=x"):
            with self.assertRaises(ValueError):
                validate_webhook(url)


if __name__ == "__main__":
    unittest.main()
