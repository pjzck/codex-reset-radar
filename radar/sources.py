"""Adapters for two independent, unofficial trackers; no browser required."""

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import urlsplit


def timestamp(value):
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("Source timestamp must include a timezone")
    return dt.astimezone(timezone.utc)


def safe_url(value):
    if not isinstance(value, str):
        return ""
    parsed = urlsplit(value)
    if parsed.scheme == "https" and parsed.hostname in {"x.com", "www.x.com", "twitter.com", "www.twitter.com"}:
        match = re.fullmatch(r"/([A-Za-z0-9_]+)/status/(\d+)/?", parsed.path)
        if match:
            return "https://x.com/{}/status/{}".format(*match.groups())
    return ""


@dataclass(frozen=True)
class Event:
    source: str
    occurred_at: datetime
    text: str
    kind: str
    phase: str
    url: str
    evidence: str
    source_id: str = ""

    @property
    def key(self):
        match = re.search(r"/status/(\d+)", self.url)
        identity = "x:" + match.group(1) if match else self.source + ":" + self.source_id
        if not match and not self.source_id:
            identity += ":" + hashlib.sha256((self.occurred_at.isoformat() + self.text).encode()).hexdigest()
        return identity + ":" + self.phase


class Node:
    def __init__(self, tag="", attrs=()):
        self.tag = tag
        self.attrs = dict(attrs)
        self.children = []

    def find(self, class_name):
        found = []
        if class_name in self.attrs.get("class", "").split():
            found.append(self)
        for child in self.children:
            if isinstance(child, Node):
                found.extend(child.find(class_name))
        return found

    def nodes(self):
        yield self
        for child in self.children:
            if isinstance(child, Node):
                yield from child.nodes()

    def text(self):
        return " ".join("".join(c.text() if isinstance(c, Node) else c for c in self.children).split())


class Document(HTMLParser):
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.root = Node()
        self.stack = [self.root]
        self.feed(html)
        self.close()

    def handle_starttag(self, tag, attrs):
        node = Node(tag, attrs)
        self.stack[-1].children.append(node)
        if tag not in self.VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for idx in range(len(self.stack) - 1, 0, -1):
            if self.stack[idx].tag == tag:
                del self.stack[idx:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def parse_com(html):
    root = Document(html).root
    events = []
    for item in root.find("log-item"):
        labels = item.find("log-event-status")
        texts = item.find("log-text")
        links = item.find("log-link")
        dates = [n.attrs["data-datetime"] for n in item.nodes() if "data-datetime" in n.attrs]
        if not labels or not texts or not links or not dates:
            raise ValueError("codexresets.com announcement structure changed")
        label = labels[0].text().upper()
        if any(token in label for token in ("POSSIBLE", "TEASER", "UNCONFIRMED")):
            phase = "possible"
        elif any(token in label for token in ("SCHEDULED", "PLANNED", "PENDING", "ANNOUNCED")):
            phase = "scheduled"
        elif "CONFIRMED" in label:
            phase = "confirmed"
        else:
            raise ValueError("Unrecognized codexresets.com event status")
        url = safe_url(links[0].attrs.get("href", ""))
        if not url:
            raise ValueError("Announcement lacks a valid original post URL")
        # Expanded content avoids duplicate preview/full text on long posts.
        expanded = texts[0].find("tweet-expanded-content")
        text = (expanded[0] if expanded else texts[0]).text()
        if not text:
            raise ValueError("Empty announcement")
        events.append(Event("com", timestamp(dates[0]), text,
                            "banked" if "BANKED" in label else "regular",
                            phase, url, labels[0].text()))
    if not events:
        raise ValueError("No announcements found on codexresets.com; refusing an empty baseline")
    return events


def parse_org(payload):
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or not rows:
        raise ValueError("Invalid or empty codex-resets.org API response")
    events = []
    for row in rows:
        if row.get("reset_type") not in {"regular", "banked"}:
            raise ValueError("Unrecognized codex-resets.org reset type")
        source = row.get("source", {})
        if not isinstance(source, dict) or source.get("type") not in {"x_post", "observed"}:
            raise ValueError("Unrecognized codex-resets.org evidence type")
        raw_status = row.get("status", "confirmed")
        if raw_status not in {"confirmed", "scheduled", "possible"}:
            raise ValueError("Unrecognized codex-resets.org status")
        text = row.get("text")
        if not isinstance(text, str) or not text.strip() or not row.get("id"):
            raise ValueError("Incomplete codex-resets.org announcement")
        events.append(Event("org", timestamp(row["announced_at"]), text.strip(),
                            row["reset_type"], raw_status, safe_url(source.get("url", "")),
                            "站点观测记录" if source["type"] == "observed" else "站点收录的 X 公告",
                            str(row["id"])))
    return events
