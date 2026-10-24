"""Separate execution status, source agreement, and links in Feishu messages."""

import hashlib
import json
from datetime import datetime, timedelta, timezone

from .content import chinese_summary, time_labels

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
    agreement = "单源收录" if len(events) == 1 else "双源状态一致"
    if len(events) > 1 and len({e.phase for e in events}) > 1:
        agreement = "双源收录，确认程度不同"
    if phase == "disputed":
        agreement = "来源结论不一致，暂不判定为执行完成"
    summary = chinese_summary(events, phase)
    if len(kinds) > 1:
        summary = "类型待核实；" + summary
    notes = {
        "possible": "尚未确认重置计划或执行。",
        "scheduled": "预计时间到达后仍需执行确认，不会自动视为已执行。",
        "reported": "站点记录或含糊表述不等于执行确认。",
        "executing": "正在执行不等于所有账户已经生效。",
        "confirmed": "个人额度及适用范围以自己的账户为准。",
        "disputed": "保留双方信息并继续检查。",
    }
    collected_at = collected_at or datetime.now(timezone.utc)
    expected_time, actual_time = time_labels(events, as_of=collected_at)
    if phase == "disputed":
        actual_time = "待确认（来源状态存在分歧）"
    lines = [title, "摘要：" + summary,
             "⏰ 预计重置时间：" + expected_time, "🕘 实际重置时间：" + actual_time]
    if previous_phases and phase not in previous_phases:
        previous = max(previous_phases, key=lambda p: PHASE_RANK.get(p, -1))
        lines.append("状态更新：{} → {}".format(PHASE_LABELS[previous], PHASE_LABELS[phase]))
    beijing = timezone(timedelta(hours=8))
    local_time = collected_at.astimezone(beijing)
    lines.extend(["采集时间：{}（北京时间 UTC+8）".format(local_time.strftime("%Y-%m-%d %H:%M:%S")),
                  "交叉核对：" + agreement, "提示：" + notes[phase], "", "—— 参考来源与原文 ——"])
    for event in events:
        source_kind = "备用重置（Banked）" if event.kind == "banked" else "普通额度重置"
        lines.extend(["", "采集网站：" + SOURCE_NAMES[event.source],
                      "网站链接：" + SOURCE_SITES[event.source],
                      "本站信息判定：{} / {}".format(PHASE_LABELS[event.phase], source_kind),
                      "站点记录时间（非重置时间）：{}（北京时间）".format(event.occurred_at.astimezone(beijing).strftime("%Y-%m-%d %H:%M:%S")),
                      "来源标注：" + event.evidence])
        source_expected, source_actual = time_labels([event], as_of=collected_at)
        lines.extend(["本站公告预计时间：" + source_expected, "本站公告实际时间：" + source_actual])
        if event.basis:
            lines.append("判断依据：" + event.basis)
        lines.append("公告原文：" + event.text[:700] + ("…" if len(event.text) > 700 else ""))
    urls = sorted({e.url for e in events if e.url})
    if urls:
        lines.extend(["", "原始 X 公告："] + urls)
    if failed_sources:
        names = "、".join(SOURCE_NAMES[source] for source in sorted(set(failed_sources)))
        lines.extend(["", "校验暂不可用：" + names])
    lines.extend(["", "Codex Reset Radar"])
    return "\n".join(lines)


def demo_message():
    """A single clearly labelled sample message; no poll or delivery state writes."""
    from .sources import Event
    now = datetime.now(timezone.utc)
    planned = (now + timedelta(hours=3)).strftime("%Y-%m-%d %H:%M")
    executed = (now - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M")
    samples = []
    for phase, body in (("scheduled", "We will reset usage for all paid users at " + planned + " UTC, together with a small update."),
                        ("confirmed", "The reset completed for all paid users at " + executed + " UTC.")):
        events = [Event(source, now, body, "regular", phase, "", "合成演示，非真实网站公告", "format-demo")
                  for source in ("com", "org")]
        samples.append(format_message(events, previous_phases=("scheduled",) if phase == "confirmed" else (),
                                      collected_at=now, demo=True))
    return "\n\n──────────\n\n".join(samples)
