"""Polling, durable delivery state, and a small command-line interface."""

import argparse
import json
import logging
import os
import signal
import sqlite3
import threading
import time
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .sources import parse_com, parse_org
from .reset_site import FORECAST_URL, fetch_reset, fetch_reset_samples, reset_demo_messages
from .notifications import PHASE_RANK, format_message, notification_key, notification_phase
from .transport import RequestError, request, send_feishu, validate_webhook

LOG = logging.getLogger("radar")
SOURCE_URLS = {"reset": FORECAST_URL, "com": "https://codexresets.com/", "org": "https://codex-resets.org/api/resets"}
DEFAULTS = {"sources": ["reset"], "poll_interval_seconds": 60, "request_timeout_seconds": 20,
            "state_path": "data/state.sqlite3", "first_run": "latest", "max_event_age_hours": 72,
            "notify_possible": True}


def settings(path):
    config = dict(DEFAULTS)
    if path:
        config.update(json.loads(Path(path).read_text(encoding="utf-8-sig")))
    if not isinstance(config["sources"], list) or not config["sources"] or not all(s in SOURCE_URLS for s in config["sources"]):
        raise ValueError("sources must contain reset (default), or legacy com/org")
    config["sources"] = list(dict.fromkeys(config["sources"]))
    for field, minimum in (("poll_interval_seconds", 30), ("request_timeout_seconds", 1), ("max_event_age_hours", 1)):
        if type(config[field]) not in (int, float) or not minimum <= config[field] <= 604800:
            raise ValueError("Invalid configuration: " + field)
    if type(config["notify_possible"]) is not bool or config["first_run"] not in {"latest", "silent"}:
        raise ValueError("Invalid first_run/notify_possible configuration")
    if os.environ.get("RADAR_STATE_PATH"):
        config["state_path"] = os.environ["RADAR_STATE_PATH"]
    return config


def load_env(path):
    if not path:
        return
    for line in Path(path).read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or key.strip() not in {"FEISHU_WEBHOOK_URL", "FEISHU_SECRET", "RADAR_STATE_PATH"}:
            raise ValueError("Invalid environment file entry")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


class State:
    def __init__(self, path, lock=True):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.lock_file = None
        if lock:
            self.lock_file = open(str(path) + ".lock", "a+b")
            try:
                if os.name == "nt":
                    import msvcrt
                    self.lock_file.seek(0, 2)
                    if self.lock_file.tell() == 0:
                        self.lock_file.write(b"0")
                        self.lock_file.flush()
                    self.lock_file.seek(0)
                    msvcrt.locking(self.lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                self.lock_file.close()
                self.lock_file = None
                raise RuntimeError("Another sender is using this state database") from None
        try:
            self.db = sqlite3.connect(path, timeout=30)
        except Exception:
            if self.lock_file:
                self.lock_file.close()
            raise
        self.db.execute("CREATE TABLE IF NOT EXISTS seen (key TEXT PRIMARY KEY, recorded_at TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS initialized (source TEXT PRIMARY KEY)")
        self.db.execute("CREATE TABLE IF NOT EXISTS health (source TEXT PRIMARY KEY, checked_at TEXT, ok INTEGER)")
        self.db.execute("CREATE TABLE IF NOT EXISTS observations (identity TEXT, source TEXT, payload TEXT, checked_at TEXT, PRIMARY KEY(identity,source))")
        self.db.execute("CREATE TABLE IF NOT EXISTS deliveries (key TEXT PRIMARY KEY, identity TEXT, phase TEXT, delivered_at TEXT)")
        self.db.execute("CREATE TABLE IF NOT EXISTS classification_baselines (identity TEXT PRIMARY KEY)")
        self.db.commit()

    def seen(self, key):
        return self.db.execute("SELECT 1 FROM seen WHERE key=?", (key,)).fetchone() is not None

    def mark(self, key):
        self.db.execute("INSERT OR IGNORE INTO seen VALUES (?,?)", (key, datetime.now(timezone.utc).isoformat()))
        self.db.commit()

    def completed(self, identity):
        return self.seen(identity + ":v2:confirmed") or self.db.execute(
            "SELECT 1 FROM seen WHERE key LIKE ? LIMIT 1", (identity + ":v3:confirmed:%",)).fetchone() is not None

    def initialized(self, source):
        return self.db.execute("SELECT 1 FROM initialized WHERE source=?", (source,)).fetchone() is not None

    def initialize(self, source):
        self.db.execute("INSERT OR IGNORE INTO initialized VALUES (?)", (source,))
        self.db.commit()

    def health(self, source, ok):
        self.db.execute("INSERT OR REPLACE INTO health VALUES (?,?,?)", (source, datetime.now(timezone.utc).isoformat(), int(ok)))
        self.db.commit()

    def remember(self, event, now):
        payload = asdict(event)
        payload["occurred_at"] = event.occurred_at.isoformat()
        self.db.execute("INSERT OR REPLACE INTO observations VALUES (?,?,?,?)",
                        (event.identity, event.source, json.dumps(payload, ensure_ascii=False), now.isoformat()))
        self.db.commit()

    def delivery_phases(self, identity):
        return {row[0] for row in self.db.execute("SELECT DISTINCT phase FROM deliveries WHERE identity=?", (identity,))}

    def adopt_legacy(self, identity, key):
        """Reclassify old records silently, while allowing later execution proof.

        Version 1 treated observation rows as completed. Do not replay them or let
        that old flag hide a future real completion under the stricter policy.
        """
        if self.db.execute("SELECT 1 FROM classification_baselines WHERE identity=?", (identity,)).fetchone():
            return False
        legacy = any(self.seen(identity + ":" + phase) for phase in PHASE_RANK)
        with self.db:
            self.db.execute("INSERT INTO classification_baselines VALUES (?)", (identity,))
            if legacy:
                self.db.execute("INSERT OR IGNORE INTO seen VALUES (?,?)", (key, datetime.now(timezone.utc).isoformat()))
        return legacy

    def delivered(self, key, identity, phase):
        recorded_at = datetime.now(timezone.utc).isoformat()
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO seen VALUES (?,?)", (key, recorded_at))
            self.db.execute("INSERT OR IGNORE INTO deliveries VALUES (?,?,?,?)", (key, identity, phase, recorded_at))

    def close(self):
        self.db.close()
        if self.lock_file:
            self.lock_file.close()
            self.lock_file = None


def fetch_source(source, timeout):
    if source == "reset":
        return fetch_reset(timeout)
    body = request(SOURCE_URLS[source], timeout=timeout)
    return parse_com(body) if source == "com" else parse_org(json.loads(body))


def poll(config, state=None, sender=None, fetcher=fetch_source, now=None, preview=False):
    now = now or datetime.now(timezone.utc)
    groups, successful, failed, candidates = {}, [], [], set()
    baselines = []
    for source in config["sources"]:
        try:
            events = fetcher(source, config["request_timeout_seconds"])
            if not events and source != "reset":
                raise ValueError("Empty source result")
        except Exception as exc:
            LOG.error("source=%s fetch/parse failed (%s)", source, type(exc).__name__)
            if state:
                state.health(source, False)
            failed.append(source)
            continue
        successful.append(source)
        if state:
            state.health(source, True)
        if not events:
            if state and not state.initialized(source):
                state.initialize(source)
            LOG.info("source=%s parsed=0 (no actionable alert)", source)
            continue
        first = bool(state and not state.initialized(source))
        newest = max(events, key=lambda e: e.occurred_at).key
        for event in events:
            if state:
                state.remember(event, now)
            age = now - event.occurred_at
            eligible = age <= timedelta(hours=config["max_event_age_hours"])
            if age < -timedelta(minutes=5) and event.phase not in {"possible", "scheduled"}:
                # A future plan can be announced now; future execution cannot be confirmed now.
                continue
            baseline_only = first and (config["first_run"] == "silent" or event.key != newest)
            if baseline_only:
                baselines.append((event.identity, notification_key([event])))
            if not eligible:
                if state:
                    baselines.append((event.identity, notification_key([event])))
                continue
            if not baseline_only and (not state or not state.seen(notification_key([event]))):
                candidates.add(event.identity)
            # Include already-seen current records when checking source agreement.
            bucket = groups.setdefault(event.identity, {})
            previous = bucket.get(source)
            if previous is None or (event.occurred_at, PHASE_RANK[event.phase]) > (previous.occurred_at, PHASE_RANK[previous.phase]):
                bucket[source] = event
        if first:
            state.initialize(source)
        LOG.info("source=%s parsed=%d", source, len(events))
    # One source's historical baseline must not consume another source's new alert.
    if state:
        for identity, key in baselines:
            if identity not in candidates:
                state.mark(key)
    sent = 0
    for identity, by_source in sorted(groups.items(), key=lambda pair: min(e.occurred_at for e in pair[1].values())):
        events = list(by_source.values())
        phase = notification_phase(events)
        key = notification_key(events)
        if state and state.adopt_legacy(identity, key):
            continue
        if identity not in candidates:
            continue
        if state and state.seen(key):
            continue
        if state and phase not in {"confirmed", "disputed"} and state.completed(identity):
            continue
        if not config["notify_possible"] and phase != "confirmed":
            continue
        previous_phases = state.delivery_phases(identity) if state else ()
        message = format_message(events, failed, previous_phases, now)
        if preview:
            print(message + "\n---")
        else:
            try:
                sender(message)
            except Exception as exc:
                LOG.error("delivery failed (%s); will retry next poll", str(exc) if isinstance(exc, RequestError) else type(exc).__name__)
                failed.append("feishu")
                break
            state.delivered(key, identity, phase)
            LOG.info("delivered event=%s sources=%s", key, ",".join(by_source))
        sent += 1
    LOG.info("poll complete sources_ok=%d sources_failed=%d messages=%d", len(successful), len(set(failed)), sent)
    return 1 if failed else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Codex reset announcements → Feishu")
    parser.add_argument("command", choices=["run", "check", "preview", "test-message", "status"], nargs="?", default="run")
    parser.add_argument("--config", default=None, help="JSON configuration file")
    parser.add_argument("--env-file", default=None, help="Private environment file (never commit it)")
    parser.add_argument("--real-samples", action="store_true", help="For test-message: fetch real historical examples; fill missing categories with labelled synthetic samples")
    args = parser.parse_args(argv)
    if args.real_samples and args.command != "test-message":
        parser.error("--real-samples is only supported with test-message")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    state = None
    try:
        load_env(args.env_file)
        config = settings(args.config)
        if args.command == "preview":
            return poll(config, preview=True)
        if args.command == "status":
            if not Path(config["state_path"]).exists():
                print("State database not initialized")
                return 1
            state = State(config["state_path"], lock=False)
            print(json.dumps({"health": state.db.execute("SELECT source,checked_at,ok FROM health ORDER BY source").fetchall(),
                              "processed_events": state.db.execute("SELECT count(*) FROM seen").fetchone()[0]}, ensure_ascii=False, indent=2))
            return 0
        webhook = os.environ.get("FEISHU_WEBHOOK_URL", "")
        validate_webhook(webhook)
        secret = os.environ.get("FEISHU_SECRET", "")
        sender = lambda text: send_feishu(webhook, text, secret, config["request_timeout_seconds"])
        if args.command == "test-message":
            if config["sources"] == ["reset"]:
                messages = fetch_reset_samples(config["request_timeout_seconds"]) if args.real_samples else reset_demo_messages()
                if not messages:
                    raise RequestError("No sample available; no demo messages sent")
            else:
                from .notifications import demo_messages
                real_events, failed_sources = [], []
                if args.real_samples:
                    for source in config["sources"]:
                        try:
                            real_events.extend(fetch_source(source, config["request_timeout_seconds"]))
                        except Exception as exc:
                            LOG.error("demo source=%s failed (%s)", source, type(exc).__name__)
                            failed_sources.append(source)
                    if not real_events:
                        raise RequestError("No real sample source available; no demo messages sent")
                messages = demo_messages(real_events, failed_sources=failed_sources)
            for number, message in enumerate(messages, 1):
                if number > 1:
                    time.sleep(1)
                sender(message)
                LOG.info("Feishu demo message accepted (%d/%d)", number, len(messages))
            return 0
        state = State(config["state_path"])
        if args.command == "check":
            return poll(config, state, sender)
        stop = threading.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: stop.set())
        while not stop.is_set():
            poll(config, state, sender)
            stop.wait(config["poll_interval_seconds"])
        return 0
    except Exception as exc:
        # Untrusted endpoint errors must never print the webhook or signature.
        LOG.error("command failed (%s); check configuration and service health", str(exc) if isinstance(exc, RequestError) else type(exc).__name__)
        return 1
    finally:
        if state:
            state.close()
