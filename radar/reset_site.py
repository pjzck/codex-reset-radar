"""The single-site default: structured alerts published by codex-reset.com."""

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone

from .sources import Event, execution_phase, safe_url, timestamp
from .transport import request

SITE = "https://codex-reset.com/"
FORECAST_URL = SITE + "api/forecast?locale=zh&tz=Asia%2FShanghai"
TIMELINE_URL = SITE + "api/timeline?locale=zh"
BEIJING = timezone(timedelta(hours=8))


def _iso(value):
    return timestamp(value).isoformat() if value else ""


def _deadline(window):
    if not isinstance(window, dict) or re.search(r"timing unspecified|时间未定|时间未明确", str(window.get("label", "")), re.I):
        return ""
    value = (window.get("target_at") or window.get("end_at")) if window.get("target_kind") == "deadline" else window.get("end_at")
    return _iso(value)


def _is_preview(summary):
    # Accept terse deadlines only within the site's structured watch alerts.
    clock = r"(?:midnight|noon|eod|end of (?:the )?day|(?:0?[1-9]|1[0-2])(?::[0-5]\d)?\s*[ap]m|(?:[01]?\d|2[0-3]):[0-5]\d)"
    short_deadline = r"\s*(?:by|before)\s+" + clock + r"(?:\s+(?:PST|PDT|PT|UTC|GMT))?(?:\s+(?:today|tomorrow))?[.!]?\s*"
    return execution_phase(summary)[0] == "scheduled" or bool(re.fullmatch(short_deadline, summary, re.I))


def parse_reset_forecast(payload):
    if not isinstance(payload, dict) or "latest_alert" not in payload or not payload.get("updated_at"):
        raise ValueError("Invalid codex-reset.com forecast response")
    _iso(payload["updated_at"])
    alert = payload["latest_alert"]
    if alert is None:
        signal = payload.get("official_signal")
        if not isinstance(signal, dict) or not signal.get("tweet_id"):
            return []
        alert = {"id": signal["tweet_id"], "kind": "watch", "state": "active",
                 "source_at": signal.get("at"), "summary": signal.get("summary"),
                 "localized_summary": signal.get("localized_summary"), "url": signal.get("url"),
                 "window": signal.get("window")}
    if not isinstance(alert, dict) or not alert.get("id") or not alert.get("source_at"):
        raise ValueError("Invalid codex-reset.com alert")
    kind, state = alert.get("kind"), alert.get("state")
    banked = kind == "banked"
    if kind == "reset" and state == "confirmed":
        phase = "confirmed"
    elif kind == "watch" and state in {"active", "expired"}:
        phase = "scheduled"
    elif banked and state in {"available", "granted", "landed"}:
        phase = "confirmed"
    elif banked and state in {"announced", "scheduled", "pending"}:
        phase = "scheduled"
    elif banked and state in {"expired", "used", "unknown"}:
        return []
    else:
        raise ValueError("Unrecognized codex-reset.com alert state")
    summary = alert.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise ValueError("Empty codex-reset.com alert")
    if kind == "watch" and not _deadline(alert.get("window")) and not _is_preview(summary):
        return []
    origin = str(alert.get("confirmation_source") or "")
    observed = origin in {"operator_observed", "probe_observed"} or (banked and alert.get("observed") is True)
    observed_at = _iso(alert.get("observed_at") or (alert.get("source_at") if banked and observed else None))
    if observed and not observed_at:
        raise ValueError("Website observation lacks its observation timestamp")
    confirmation_url = safe_url(alert.get("confirmation_url", ""))
    url = confirmation_url or safe_url(alert.get("url", ""))
    if phase == "scheduled" and not url:
        raise ValueError("Scheduled reset lacks an original announcement URL")
    published = _iso(alert.get("source_at"))
    details = {
        "origin": "website" if observed else "tibo" if phase == "scheduled" or confirmation_url or origin in {"tibo", "tibo_confirmed", "official"} else "website_confirmed",
        "announced_at": published,
        "observed_at": observed_at,
        "expected_end_at": _deadline(alert.get("window")),
        "confirmation_url": confirmation_url,
        "confirmed_at": _iso(alert.get("confirmed_at")),
        "translation": str(alert.get("observation_message") if observed else alert.get("localized_summary") or ""),
        "original": summary,
        "original_label": "公告原文",
    }
    if "measured_at" in alert:
        details["measured_at"] = _iso(alert.get("measured_at"))
    event_at = observed_at if observed else published
    return [Event("reset", timestamp(event_at), summary, "banked" if banked else "regular", phase,
                  url, "网站观测" if observed else "Tibo（@thsottiaux）" if details["origin"] == "tibo" else "网站确认",
                  str(alert["id"]), "依据 codex-reset.com 的结构化通知状态", details)]


def fetch_reset(timeout=20):
    return parse_reset_forecast(json.loads(request(FORECAST_URL, timeout=timeout)))


def reset_key(event):
    details = event.details
    # A new Tibo confirmation or a revised deadline may merit one follow-up.
    status = [details.get(name, "") for name in ("origin", "observed_at", "expected_end_at", "confirmation_url")]
    digest = hashlib.sha256(json.dumps(status, ensure_ascii=False).encode()).hexdigest()[:12]
    return event.identity + ":v3:" + event.phase + ":" + digest


def _beijing(value):
    return timestamp(value).astimezone(BEIJING).strftime("%Y-%m-%d %H:%M:%S") + "（北京时间）" if value else "待确认"


def _observation_translation(event):
    details = event.details
    body = details.get("translation", "")
    if not isinstance(body, str) or not re.search(r"[\u4e00-\u9fff]", body):
        measured = details.get("measured_at", details.get("observed_at", ""))
        lines = ["Codex 重置已由网站观测确认。"]
        if measured:
            lines.append("网站检查显示，Codex 用量额度约于 " + _beijing(measured) + "重置。")
        if details.get("confirmation_url"):
            lines.append("Tibo 已发布确认消息。")
        else:
            lines.append("Tibo 尚未发布确认消息，后续确认后可能更新。")
        return "\n".join(lines)
    # Keep the site's Chinese copy, converting its embedded UTC clock to Beijing.
    anchor = timestamp(details["observed_at"])
    def convert(match):
        month, day, hour, minute = map(int, match.groups())
        candidates = []
        for year in (anchor.year - 1, anchor.year, anchor.year + 1):
            try:
                candidates.append(datetime(year, month, day, hour, minute, tzinfo=timezone.utc))
            except ValueError:
                pass
        if not candidates:
            return "时间待确认"
        value = min(candidates, key=lambda item: abs(item - anchor))
        return value.astimezone(BEIJING).strftime("%Y-%m-%d %H:%M") + "（北京时间）"
    return re.sub(r"(\d{1,2})月(\d{1,2})日\s*(\d{1,2}):(\d{2})\s*UTC", convert, body)


def format_reset_message(event, demo=False):
    details = event.details
    observed = details.get("origin") == "website"
    banked = event.kind == "banked"
    scheduled = event.phase == "scheduled"
    title = "Codex 已安排重置" if scheduled else "Codex 备用重置已到账" if banked else "Codex 已重置"
    lines = [title, "类型：" + ("备用重置" if banked else "常规重置")]
    if scheduled:
        lines.extend(["公告时间：" + _beijing(details.get("announced_at")),
                      "预计最晚：" + _beijing(details.get("expected_end_at")),
                      "状态：等待网站确认执行", details.get("original_label", "公告原文") + "：",
                      details.get("original") or event.text])
        source = "Tibo（@thsottiaux）"
    elif observed:
        lines.extend(["观测时间：" + _beijing(details.get("observed_at")),
                      "公告译文：", _observation_translation(event)])
        source = "网站观测" + ("，Tibo 已确认" if details.get("confirmation_url") else "")
    else:
        translated = details.get("translation", "")
        if not re.search(r"[\u4e00-\u9fff]", translated):
            translated = "备用重置机会已到账。" if banked else "Codex 用量额度已重置。"
        lines.extend(["公告时间：" + _beijing(details.get("confirmed_at") or details.get("announced_at")),
                      "公告译文：", translated])
        source = "Tibo（@thsottiaux）" if details.get("origin") == "tibo" else "网站确认"
    lines.extend(["消息来源：" + source, "网站：" + SITE])
    if event.url:
        lines.append(("相关公告：" if observed else "原文链接：") + event.url)
    if demo:
        lines.append("【历史预告回放，非当前状态】" if details.get("historical_plan") and demo == "real" else
                     "【历史格式样例，非新增通知】" if demo == "real" else "【格式样例，非真实通知】")
    return "\n".join(lines)


def fetch_reset_samples(timeout=20):
    current = fetch_reset(timeout)
    payload = json.loads(request(TIMELINE_URL, timeout=timeout))
    if not isinstance(payload, dict) or not isinstance(payload.get("events"), list):
        raise ValueError("Invalid codex-reset.com timeline")
    previews = []
    for row in payload["events"]:
        if row.get("type") != "reset" or not _deadline(row.get("official_window")):
            continue
        at = _iso(row["official_window"].get("start_at") or row.get("announced_at"))
        original = str(row.get("summary") or "")
        if not at or not original or not row.get("id"):
            continue
        details = {"origin": "tibo", "announced_at": at, "expected_end_at": _deadline(row["official_window"]),
                   "original": original, "original_label": "公告内容（网站历史预告）", "historical_plan": True}
        previews.append(Event("reset", timestamp(at), original, "regular", "scheduled", safe_url(row.get("url", "")),
                              "Tibo（@thsottiaux）", str(row["id"]), details=details))
    examples = []
    if previews:
        examples.append(max(previews, key=lambda event: event.occurred_at))
    examples.extend(current)
    return [format_reset_message(event, demo="real") for event in examples]


def reset_demo_messages():
    """The user's two compact layouts, sent separately and labelled as samples."""
    observed = parse_reset_forecast({"updated_at": "2026-10-24T04:00:00Z", "latest_alert": {
        "id": "2113492502355562875", "kind": "reset", "state": "confirmed",
        "source_at": "2026-10-23T17:40:51Z", "summary": "Reset observed; awaiting Tibo.",
        "confirmation_source": "operator_observed", "observed_at": "2026-10-23T17:40:51Z",
        "observation_message": "Codex 重置已确认。\n我们的检查显示，Codex 用量额度已于 10月23日 17:40 UTC 左右重置。\nTibo 尚未发布确认消息。他发布后，我们可能会更新这条消息。"}})[0]
    scheduled = parse_reset_forecast({"updated_at": "2026-10-24T04:00:00Z", "latest_alert": {
        "id": "2113492502355562875", "kind": "watch", "state": "active",
        "source_at": "2026-10-23T04:47:34Z", "summary": "Landing tomorrow before 10am PST together with a small ship.",
        "url": "https://x.com/thsottiaux/status/2113492502355562875",
        "window": {"target_kind": "deadline", "target_at": "2026-10-23T17:00:00Z"}}})[0]
    return [format_reset_message(event, demo=True) for event in (scheduled, observed)]
