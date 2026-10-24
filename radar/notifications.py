"""Separate execution status, source agreement, and links in Feishu messages."""

import hashlib
import json
from datetime import datetime, timedelta, timezone

SOURCE_SITES = {"com": "https://codexresets.com/#log-heading", "org": "https://codex-resets.org/"}
SOURCE_NAMES = {"com": "Codex Resets（codexresets.com）", "org": "Codex Resets Unofficial Tracker（codex-resets.org）"}
PHASE_LABELS = {"possible": "可能重置信号", "scheduled": "重置预告", "reported": "执行待确认",
                "executing": "正在执行", "confirmed": "已执行（公告确认）", "disputed": "来源状态存在分歧"}
PHASE_RANK = {"possible": 0, "reported": 1, "scheduled": 2, "executing": 3, "confirmed": 4}


def notification_phase(events):
    phases = {e.phase for e in events if e.phase != "reported"}
    kinds = {e.kind for e in events}
    pending = phases.intersection({"possible", "scheduled"})
    executed = phases.intersection({"executing", "confirmed"})
    if len(kinds) > 1 or (pending and executed):
        return "disputed"
    return max(events, key=lambda e: PHASE_RANK[e.phase]).phase


def notification_key(events):
    phase = notification_phase(events)
    identity = events[0].identity
    if phase == "disputed":
        status = sorted((e.source, e.phase, e.kind) for e in events)
        digest = hashlib.sha256(json.dumps(status).encode()).hexdigest()[:12]
        return identity + ":v2:disputed:" + digest
    return identity + ":v2:" + phase


def format_message(events, failed_sources=(), previous_phases=(), collected_at=None, demo=False):
    events = sorted(events, key=lambda e: e.source)
    phase = notification_phase(events)
    icons = {"possible": "💭", "scheduled": "🕒", "reported": "🔎", "executing": "🔄", "confirmed": "✅", "disputed": "⚠️"}
    title = icons[phase] + " Codex " + PHASE_LABELS[phase]
    if demo:
        title = "【格式演示 · 非真实重置通知】\n" + title
    kinds = {e.kind for e in events}
    kind = "备用重置（Banked）" if kinds == {"banked"} else "普通额度重置" if kinds == {"regular"} else "类型待核实"
    agreement = "单源收录" if len(events) == 1 else "双源状态一致"
    if len(events) > 1 and len({e.phase for e in events}) > 1:
        agreement = "双源收录，确认程度不同"
    if phase == "disputed":
        agreement = "来源结论不一致，暂不判定为执行完成"
    lines = [title, "Codex Reset Radar", "类型：" + kind, "交叉核对：" + agreement]
    if previous_phases and phase not in previous_phases:
        previous = max(previous_phases, key=lambda p: PHASE_RANK.get(p, -1))
        lines.append("状态更新：{} → {}".format(PHASE_LABELS[previous], PHASE_LABELS[phase]))
    local_time = (collected_at or datetime.now(timezone.utc)).astimezone(timezone(timedelta(hours=8)))
    lines.append("采集时间：{}（北京时间 UTC+8）".format(local_time.strftime("%Y-%m-%d %H:%M:%S")))
    for event in events:
        local = event.occurred_at.astimezone(timezone(timedelta(hours=8)))
        lines.extend(["", "采集网站：" + SOURCE_NAMES[event.source],
                      "网站链接：" + SOURCE_SITES[event.source],
                      "本站信息判定：" + PHASE_LABELS[event.phase],
                      "站点标注时间：{}（北京时间 UTC+8）".format(local.strftime("%Y-%m-%d %H:%M:%S")),
                      "来源标注：" + event.evidence])
        if event.basis:
            lines.append("判断依据：" + event.basis)
        lines.append("公告原文：" + event.text[:700] + ("…" if len(event.text) > 700 else ""))
    urls = sorted({e.url for e in events if e.url})
    if urls:
        lines.extend(["", "原始 X 公告："] + urls)
    if failed_sources:
        names = "、".join(SOURCE_NAMES[source] for source in sorted(set(failed_sources)))
        lines.extend(["", "校验暂不可用：" + names])
    notes = {
        "possible": "这是可能重置信号，尚未确认计划或执行。",
        "scheduled": "这是预告；预计时间到达后仍需执行确认，不会自动视为已执行。",
        "reported": "目前只有站点记录或含糊表述，尚未确认实际执行。",
        "executing": "公告描述正在执行，尚未确认所有账户生效。",
        "confirmed": "执行结论依据采集到的公告；个人额度及适用范围请查看自己的账户。",
        "disputed": "两站表述不一致，保留双方信息并继续检查。",
    }
    lines.extend(["", notes[phase]])
    return "\n".join(lines)


def demo_message():
    """A single clearly labelled sample message; no poll or delivery state writes."""
    from .sources import Event
    now = datetime.now(timezone.utc)
    samples = []
    for phase, body in (("scheduled", "演示内容：计划稍后重置，尚未确认执行。"),
                        ("confirmed", "演示内容：公告明确确认重置已执行。")):
        events = [Event(source, now, body, "regular", phase, "", "合成演示，非真实网站公告", "format-demo")
                  for source in ("com", "org")]
        samples.append(format_message(events, previous_phases=("scheduled",) if phase == "confirmed" else (),
                                      collected_at=now, demo=True))
    return "\n\n──────────\n\n".join(samples)
