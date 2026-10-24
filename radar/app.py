"""Polling, durable delivery state, and a small command-line interface."""

import argparse
import json
import logging
import os
import signal
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .sources import parse_com, parse_org
from .transport import RequestError, request, send_feishu, validate_webhook

LOG = logging.getLogger("radar")
SOURCE_URLS = {"com": "https://codexresets.com/", "org": "https://codex-resets.org/api/resets"}
SOURCE_SITES = {"com": "https://codexresets.com/", "org": "https://codex-resets.org/"}
DEFAULTS = {"sources": ["com", "org"], "poll_interval_seconds": 60, "request_timeout_seconds": 20,
            "state_path": "data/state.sqlite3", "first_run": "latest", "max_event_age_hours": 72,
            "notify_possible": True}


def settings(path):
    config = dict(DEFAULTS)
    if path:
        config.update(json.loads(Path(path).read_text(encoding="utf-8-sig")))
    if not isinstance(config["sources"], list) or not config["sources"] or not all(s in SOURCE_URLS for s in config["sources"]):
        raise ValueError("sources must be a nonempty list containing com and/or org")
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
        self.db.commit()

    def seen(self, key):
        return self.db.execute("SELECT 1 FROM seen WHERE key=?", (key,)).fetchone() is not None

    def mark(self, key):
        self.db.execute("INSERT OR IGNORE INTO seen VALUES (?,?)", (key, datetime.now(timezone.utc).isoformat()))
        self.db.commit()

    def initialized(self, source):
        return self.db.execute("SELECT 1 FROM initialized WHERE source=?", (source,)).fetchone() is not None

    def initialize(self, source):
        self.db.execute("INSERT OR IGNORE INTO initialized VALUES (?)", (source,))
        self.db.commit()

    def health(self, source, ok):
        self.db.execute("INSERT OR REPLACE INTO health VALUES (?,?,?)", (source, datetime.now(timezone.utc).isoformat(), int(ok)))
        self.db.commit()

    def close(self):
        self.db.close()
        if self.lock_file:
            self.lock_file.close()
            self.lock_file = None


def format_message(events):
    primary = sorted(events, key=lambda e: (e.evidence != "[ CONFIRMED RESET ]", e.source))[0]
    labels = {"confirmed": "站点记录重置", "scheduled": "重置预告 · 尚未确认执行", "possible": "可能重置 · 尚未确认"}
    kind = "储存重置（Banked）" if primary.kind == "banked" else "普通额度重置"
    lines = ["📡 Codex Reset Radar", labels[primary.phase] + " | " + kind, ""]
    for event in sorted(events, key=lambda e: e.source):
        # Fixed UTC+8 avoids OS tzdata dependencies and PST/PDT ambiguity.
        local = event.occurred_at.astimezone(timezone(timedelta(hours=8)))
        lines.append("来源：{}".format(SOURCE_SITES[event.source]))
        lines.append("站点记录时间：{}（北京时间 UTC+8）".format(local.strftime("%Y-%m-%d %H:%M:%S")))
        lines.append("来源标注：{}".format(event.evidence))
        lines.append("公告：{}".format(event.text[:1800] + ("…" if len(event.text) > 1800 else "")))
        lines.append("")
    if primary.url:
        lines.append("原始公告：" + primary.url)
    lines.append("这是非官方追踪站点的信息；实际到账与适用范围请以自己的账户为准。")
    return "\n".join(lines)


def fetch_source(source, timeout):
    body = request(SOURCE_URLS[source], timeout=timeout)
    return parse_com(body) if source == "com" else parse_org(json.loads(body))


def poll(config, state=None, sender=None, fetcher=fetch_source, now=None, preview=False):
    now = now or datetime.now(timezone.utc)
    groups, successful, failed = {}, [], []
    for source in config["sources"]:
        try:
            events = fetcher(source, config["request_timeout_seconds"])
            if not events:
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
        first = bool(state and not state.initialized(source))
        newest = max(events, key=lambda e: e.occurred_at).key
        for event in events:
            age = now - event.occurred_at
            eligible = (age <= timedelta(hours=config["max_event_age_hours"])
                        and (config["notify_possible"] or event.phase == "confirmed"))
            if age < -timedelta(minutes=5):
                # Keep future events pending, never consume them before their time.
                continue
            if first and (config["first_run"] == "silent" or event.key != newest):
                state.mark(event.key)
                continue
            if not eligible:
                if state:
                    state.mark(event.key)
                continue
            if state and state.seen(event.key):
                continue
            bucket = groups.setdefault(event.key, {})
            bucket[source] = event
        if first:
            state.initialize(source)
        LOG.info("source=%s parsed=%d", source, len(events))
    sent = 0
    for key, by_source in sorted(groups.items(), key=lambda pair: min(e.occurred_at for e in pair[1].values())):
        if state and state.seen(key):
            continue
        message = format_message(list(by_source.values()))
        if preview:
            print(message + "\n---")
        else:
            try:
                sender(message)
            except Exception as exc:
                LOG.error("delivery failed (%s); will retry next poll", str(exc) if isinstance(exc, RequestError) else type(exc).__name__)
                failed.append("feishu")
                break
            state.mark(key)
            LOG.info("delivered event=%s sources=%s", key, ",".join(by_source))
        sent += 1
    LOG.info("poll complete sources_ok=%d sources_failed=%d messages=%d", len(successful), len(set(failed)), sent)
    return 1 if failed else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Codex reset announcements → Feishu")
    parser.add_argument("command", choices=["run", "check", "preview", "test-message", "status"], nargs="?", default="run")
    parser.add_argument("--config", default=None, help="JSON configuration file")
    parser.add_argument("--env-file", default=None, help="Private environment file (never commit it)")
    args = parser.parse_args(argv)
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
            sender("📡 Codex Reset Radar\n飞书通知连通测试成功。此消息是部署测试，不代表发生额度重置。")
            LOG.info("Feishu test message accepted")
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
