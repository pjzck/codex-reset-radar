import base64
import hashlib
import hmac
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from radar.app import DEFAULTS, State, format_message, poll
from radar.sources import Event, parse_com, parse_org
from radar.transport import RequestError, send_feishu, validate_webhook

NOW = datetime(2026, 10, 24, 4, tzinfo=timezone.utc)
URL = "https://x.com/example/status/1234567890"


def event(source="com", **kwargs):
    values = dict(source=source, occurred_at=NOW - timedelta(hours=1), text="Example reset notice & details",
                  kind="regular", phase="confirmed", url=URL, evidence="[ CONFIRMED RESET ]")
    values.update(kwargs)
    return Event(**values)


def html(label="[ CONFIRMED RESET ]"):
    return '''<div class="log-list"><div class="log-item">
      <span class="log-rel" data-datetime="2026-10-24T03:00:00.000Z">1h ago</span>
      <div class="log-event-status">%s</div>
      <div class="log-text"><span>Example reset notice &amp; details</span></div>
      <a class="log-link" href="%s">Original</a></div></div>''' % (label, URL)


class ParserTests(unittest.TestCase):
    def test_com_extracts_machine_timestamp_and_html_entities(self):
        parsed = parse_com(html())[0]
        self.assertEqual(parsed.occurred_at, NOW - timedelta(hours=1))
        self.assertEqual(parsed.text, "Example reset notice & details")

    def test_teaser_does_not_become_confirmed(self):
        self.assertEqual(parse_com(html("[ POSSIBLE RESET // UNCONFIRMED ]"))[0].phase, "possible")
        self.assertEqual(parse_com(html("[ SCHEDULED RESET ]"))[0].phase, "scheduled")

    def test_banked_and_operator_labels_are_preserved(self):
        self.assertEqual(parse_com(html("[ BANKED RESET // CONFIRMED ]"))[0].kind, "banked")
        self.assertIn("OPERATOR", parse_com(html("[ CONFIRMED RESET // OPERATOR CONFIRMED ]"))[0].evidence)

    def test_delivered_as_scheduled_means_delivered(self):
        parsed = parse_com(html("[ BANKED RESET // DELIVERED AS SCHEDULED ]"))[0]
        self.assertEqual(parsed.phase, "confirmed")
        self.assertEqual(parsed.kind, "banked")

    def test_schema_drift_and_empty_page_fail_closed(self):
        for body in ("<html>challenge</html>", html("[ UNKNOWN ]"), html().replace("log-rel", "changed").replace("data-datetime", "unknown")):
            with self.assertRaises(ValueError):
                parse_com(body)

    def test_org_and_com_share_original_post_identity(self):
        payload = {"data": [{"id": "observed-1234567890", "reset_type": "regular",
                             "announced_at": "2026-10-24T02:00:00Z", "text": "Site observed reset",
                             "source": {"type": "observed", "url": URL.replace("x.com", "twitter.com")}}]}
        parsed = parse_org(payload)[0]
        self.assertEqual(parsed.key, parse_com(html())[0].key)
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

    def run_poll(self, feeds, sender=None):
        def fetch(source, timeout):
            value = feeds[source]
            if isinstance(value, Exception):
                raise value
            return value
        return poll(self.config, self.state, sender or self.messages.append, fetch, NOW)

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
        self.assertEqual(self.run_poll(feeds, fail), 1)
        self.assertFalse(self.state.seen(event().key))
        self.run_poll(feeds)
        self.assertEqual(len(self.messages), 1)

    def test_one_source_failure_does_not_block_the_other(self):
        self.assertEqual(self.run_poll({"com": ValueError("unavailable"), "org": [event("org")]}), 1)
        self.assertEqual(len(self.messages), 1)
        self.assertFalse(self.state.initialized("com"))

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
