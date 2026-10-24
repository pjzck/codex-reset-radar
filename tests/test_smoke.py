"""Only the two core flows; no field-by-field or legacy-source test suites."""

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from radar.app import DEFAULTS, State, poll
from radar.notifications import format_message
from radar.reset_site import parse_reset_forecast
from radar.transport import RequestError

NOW = datetime(2026, 10, 24, 4, tzinfo=timezone.utc)


def notice(completed=False, expired=False):
    alert = {"id": "2113492502355562875", "kind": "watch", "state": "expired" if expired else "active",
             "source_at": "2026-10-23T04:47:34Z", "summary": "Landing tomorrow before 10am PST.",
             "url": "https://x.com/thsottiaux/status/2113492502355562875",
             "window": {"target_kind": "deadline", "target_at": "2026-10-23T17:00:00Z"}}
    if completed:
        alert.update(kind="reset", state="confirmed", confirmation_source="operator_observed",
                     observed_at="2026-10-23T17:40:51Z", observation_message="Codex 重置已确认。Tibo 尚未发布确认消息。")
    return parse_reset_forecast({"updated_at": "2026-10-24T04:00:00Z", "latest_alert": alert})


class SmokeTests(unittest.TestCase):
    def test_announcement_and_website_observation(self):
        planned, completed = notice(expired=True), notice(completed=True)
        self.assertEqual(planned[0].phase, "scheduled")
        self.assertIn("预计最晚：2026-10-24 01:00:00（北京时间）", format_message(planned))
        self.assertEqual(completed[0].phase, "confirmed")
        self.assertIn("消息来源：网站观测", format_message(completed))

    def test_retry_and_persistent_deduplication(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as folder:
            path = str(Path(folder) / "state.sqlite3")
            messages = []
            def check(state, events, sender=messages.append):
                return poll(dict(DEFAULTS), state, sender, lambda *_: events, NOW)
            def fail(_):
                raise RequestError("temporary failure")
            state = State(path)
            try:
                with self.assertLogs("radar", level="ERROR"):
                    self.assertEqual(check(state, notice(), fail), 1)
                check(state, notice())
                check(state, notice())
                check(state, notice(completed=True))
            finally:
                state.close()
            state = State(path)
            try:
                check(state, notice(completed=True))
                check(state, notice(expired=True))
                self.assertEqual(len(messages), 2)
            finally:
                state.close()


if __name__ == "__main__":
    unittest.main()
