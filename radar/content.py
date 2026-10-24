"""Conservative Chinese reset summaries and times explicitly stated in posts.

Post creation time is only an anchor for relative language, never reset time.
Snowflake layout: https://docs.x.com/fundamentals/x-ids and twitter-archive/snowflake.
"""

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

BEIJING = timezone(timedelta(hours=8))
MONTHS = {name: number for number, name in enumerate(
    ("january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"), 1)}
MONTHS.update({name[:3]: number for name, number in list(MONTHS.items())})
ZONE_PATTERN = r"(?:UTC|GMT)(?:[+-]\d{1,2}(?::?\d{2})?)?|PST|PDT|PT|CST|Z|[+-]\d{2}:?\d{2}"
CLOCK = re.compile(r"(?<![\d:])(?P<h>\d{1,2})(?::(?P<m>\d{2})(?::(?P<s>\d{2}))?)?\s*(?P<ap>am|pm)?\s*(?P<tz>" + ZONE_PATTERN + r")(?![\w:+-])", re.I)
CN_CLOCK = re.compile(r"(?P<part>凌晨|早上|上午|下午|晚上|中午)?\s*(?P<h>\d{1,2})(?:[:点时](?P<m>\d{1,2})?(?:分)?)(?::(?P<s>\d{2}))?")


@dataclass(frozen=True)
class StatedTime:
    at: datetime
    qualifier: str = ""

    def display(self):
        value = self.at.astimezone(BEIJING).strftime("%Y-%m-%d %H:%M")
        return self.qualifier + value if self.qualifier in {"约 ", "不晚于 "} else value + self.qualifier


def post_created_at(event):
    """Decode only plausible modern post IDs. Do not fall back to tracker time."""
    match = re.search(r"/status/(\d{15,20})(?:/|$)", event.url)
    if not match:
        return None
    value = int(match.group(1))
    if value >= 2 ** 64:
        return None
    try:
        created = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(milliseconds=(value >> 22) + 1288834974657)
    except (ValueError, OverflowError):
        return None
    if created.year < 2020 or abs(created - event.occurred_at) > timedelta(days=90):
        return None
    return created


def _zone(token):
    token = token.upper().replace(" ", "")
    if token == "PT":
        try:
            return ZoneInfo("America/Los_Angeles")
        except ZoneInfoNotFoundError:
            return None
    if token in {"PST", "PDT", "UTC", "GMT", "Z"}:
        return timezone(timedelta(hours={"PST": -8, "PDT": -7, "UTC": 0, "GMT": 0, "Z": 0}[token]))
    # CST is ambiguous (China / Central / Cuba); never guess.
    match = re.fullmatch(r"(?:UTC|GMT)?([+-])(\d{1,2})(?::?(\d{2}))?", token)
    if not match:
        return None
    hours, minutes = int(match[2]), int(match[3] or 0)
    if hours > 14 or minutes > 59 or (hours == 14 and minutes):
        return None
    return timezone(timedelta(minutes=(hours * 60 + minutes) * (1 if match[1] == "+" else -1)))


def _local_date(text, zone, anchor):
    explicit = re.search(r"(?<!\d)(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})(?:日)?", text)
    if explicit:
        return date(*map(int, explicit.groups()))
    month = re.search(r"\b(" + "|".join(MONTHS) + r")\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(20\d{2}))?\b", text, re.I)
    if month:
        if not month[3] and not anchor:
            return None
        year = int(month[3]) if month[3] else anchor.astimezone(zone).year
        result = date(year, MONTHS[month[1].lower()], int(month[2]))
        if not month[3]:
            local_day = anchor.astimezone(zone).date()
            alternatives = [result]
            for other_year in (year - 1, year + 1):
                try:
                    alternatives.append(result.replace(year=other_year))
                except ValueError:
                    pass
            result = min(alternatives, key=lambda candidate: abs(candidate - local_day))
        return result
    if not anchor:
        return None
    local_day = anchor.astimezone(zone).date()
    if re.search(r"\btomorrow\b|明天|明日", text, re.I):
        return local_day + timedelta(days=1)
    if re.search(r"\byesterday\b|昨天|昨日", text, re.I):
        return local_day - timedelta(days=1)
    if re.search(r"\btoday\b|今天|今日", text, re.I):
        return local_day
    # A clock alone does not establish the calendar date.
    return None


def _unambiguous_local(day, hour, minute, second, zone):
    naive = datetime.combine(day, datetime.min.time()).replace(hour=hour, minute=minute, second=second)
    candidates = set()
    for fold in (0, 1):
        local = naive.replace(tzinfo=zone, fold=fold)
        utc = local.astimezone(timezone.utc)
        if utc.astimezone(zone).replace(tzinfo=None) == naive:
            candidates.add(utc)
    return candidates.pop() if len(candidates) == 1 else None


def _segments(text):
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"\ba\.m\.", "am", text, flags=re.I)
    text = re.sub(r"\bp\.m\.", "pm", text, flags=re.I)
    return [part.strip() for part in re.split(r"[;；\n]+|[.!?。！？](?:\s+|$)", text) if part.strip()]


def _read_segment(text, anchor, allow_future_relative=True):
    """Return all explicit clocks in one reset-related clause, or no reading."""
    readings = []
    relative = re.search(r"\b(?P<qual>in|within|in the next)\s+(?P<approx>~|about\s+|approximately\s+)?(?P<n>\d+(?:\.\d+)?)\s*(?P<unit>hours?|minutes?)\b", text, re.I)
    if relative and anchor and allow_future_relative:
        count = float(relative["n"])
        if 0 < count <= 168 * 60:
            delta = timedelta(hours=count) if relative["unit"].lower().startswith("hour") else timedelta(minutes=count)
            if delta <= timedelta(days=7):
                qualifier = "约 " if relative["approx"] else "不晚于 " if relative["qual"].lower() != "in" else ""
                readings.append(StatedTime(anchor + delta, qualifier))
    ago = re.search(r"\b(?P<n>\d+(?:\.\d+)?)\s*(?P<unit>hours?|minutes?)\s+ago\b", text, re.I)
    if ago and anchor and not allow_future_relative:
        delta = timedelta(hours=float(ago["n"])) if ago["unit"].lower().startswith("hour") else timedelta(minutes=float(ago["n"]))
        if delta <= timedelta(days=7):
            readings.append(StatedTime(anchor - delta))
    matches = [(match, _zone(match["tz"])) for match in CLOCK.finditer(text)]
    if "北京时间" in text or re.search(r"\bbeijing\s+time\b", text, re.I):
        matches.extend((match, BEIJING) for match in CN_CLOCK.finditer(text))
    for match, zone in matches:
        if zone is None:
            continue
        try:
            day = _local_date(text, zone, anchor)
            if day is None:
                continue
            hour, minute, second = int(match["h"]), int(match["m"] or 0), int(match["s"] or 0)
            period = match.groupdict().get("ap")
            if period:
                if not 1 <= hour <= 12:
                    continue
                hour = hour % 12 + (12 if period.lower() == "pm" else 0)
            part = match.groupdict().get("part")
            if part in {"下午", "晚上", "中午"} and hour < 12:
                hour += 12
            elif part == "凌晨" and hour == 12:
                hour = 0
            at = _unambiguous_local(day, hour, minute, second, zone)
            if at is None:
                continue
            prefix = text[max(0, match.start() - 80):match.start()]
            qualifier = " 前" if re.search(r"\bbefore\b.{0,60}$|早于.{0,30}$", prefix, re.I) else "不晚于 " if re.search(r"\bby\b.{0,60}$|不晚于.{0,30}$", prefix, re.I) else "约 " if re.search(r"\b(?:around|about)\b.{0,60}$|大约.{0,30}$", prefix, re.I) else ""
            if re.search(r"之前|以前", text[match.end():match.end() + 4]):
                qualifier = " 前"
            readings.append(StatedTime(at, qualifier))
        except (ValueError, OverflowError):
            continue
    return readings


def reset_times(event):
    """Keep planned deadlines and actual execution times separate."""
    expected, actual = [], []
    anchor = post_created_at(event)
    for segment in _segments(event.text):
        lower = segment.lower()
        if not re.search(r"\b(?:reset|resets|land|lands|landing|landed|done|completed|propagated)\b|重置|执行|发放|到账", lower):
            continue
        # A release/model time elsewhere in the post is not reset time.
        if re.search(r"\b(?:ship|shipped|release|released|launch|launched)\b", lower) and re.search(r"\band\b|随后|然后", lower):
            reset_position = re.search(r"\breset\b|重置", lower)
            first_clock = CLOCK.search(segment)
            if reset_position and first_clock and first_clock.start() < reset_position.start():
                continue
        future = re.search(r"\b(?:will|tomorrow|planned|scheduled|expected)\b|预计|计划|明天|预告", lower)
        completed = re.search(r"\b(?:completed|done|processed|propagated|landed|have been reset|has been reset)\b|已完成|已执行|已重置", lower)
        if event.phase == "confirmed" and completed and not future:
            actual.extend(_read_segment(segment, anchor, allow_future_relative=False))
        elif event.phase in {"scheduled", "possible", "executing", "confirmed"} and (future or event.phase in {"scheduled", "possible"}):
            expected.extend(_read_segment(segment, anchor))
    return expected, actual


def _merge_times(readings):
    unique = {(r.at.astimezone(timezone.utc), r.qualifier): r for r in readings}
    if len(unique) == 1:
        return next(iter(unique.values())).display() + "（北京时间）"
    return "待确认（公告时间表述不一致）" if unique else "待确认"


def time_labels(events, as_of=None):
    expected, actual = [], []
    for event in events:
        planned, executed = reset_times(event)
        expected.extend(planned)
        actual.extend(executed)
    if as_of is not None and any(item.at > as_of + timedelta(minutes=5) for item in actual):
        return _merge_times(expected), "待确认（公告实际时间晚于采集时间）"
    return _merge_times(expected), _merge_times(actual)


def chinese_summary(events, phase):
    """Extract reset-specific facts without a paid model API or invented details."""
    if phase == "disputed":
        return "来源对重置状态或类型的描述不一致，暂不确认已执行。"
    primary = max(events, key=lambda e: (e.phase == phase, e.source == "com"))
    segments = _segments(primary.text)
    text = " ".join(segments).lower()
    if primary.kind == "banked":
        actions = {"possible": "公告暗示可能发放备用重置机会，尚无明确执行确认",
                   "scheduled": "公告预告将发放备用重置机会，尚未确认执行",
                   "reported": "站点收录了备用重置相关消息，发放情况待核实",
                   "executing": "公告称正在发放备用重置机会，尚未确认全部到账",
                   "confirmed": "公告确认备用重置机会已发放"}
    else:
        actions = {"possible": "公告暗示可能重置 Codex 使用额度，尚无明确执行确认",
                   "scheduled": "公告预告将重置 Codex 使用额度，尚未确认执行",
                   "reported": "站点收录了 Codex 额度重置相关消息，执行情况待核实",
                   "executing": "公告称正在重置 Codex 使用额度，尚未确认全部生效",
                   "confirmed": "公告确认 Codex 使用额度已重置"}
    result = actions[phase]
    reset_clauses = " ".join(part.lower() for part in segments if re.search(r"reset|usage|loading|重置|发放", part, re.I))
    plans = [label for pattern, label in ((r"\bplus\b", "Plus"), (r"\bpro\b", "Pro"), (r"\bbusiness\b", "Business")) if re.search(pattern, reset_clauses)]
    if plans:
        result += "，涉及 " + "、".join(plans) + " 用户"
    elif re.search(r"\b(?:some|affected)\s+(?:paid\s+)?(?:users|accounts)\b|部分.*用户|受影响.*用户", reset_clauses):
        result += "，涉及部分或受影响用户"
    elif re.search(r"all\s+paid|everyone.{0,25}paid|所有付费", reset_clauses):
        result += "，面向所有付费用户"
    elif re.search(r"paid\s+(?:users|accounts|plans|subscriptions)", reset_clauses):
        result += "，面向付费用户"
    elif re.search(r"all\s+(?:codex\s+)?users|所有用户", reset_clauses):
        result += "，面向所有用户"
    extras = []
    if re.search(r"small\s+(?:ship|release|update)|小版本更新", text):
        extras.append("同时提到一次小版本更新")
    if re.search(r"\boutage|\bincident|服务故障|宕机", text):
        extras.append("公告还提到服务故障")
    if re.search(r"\bcelebrat|庆祝", text):
        extras.append("与庆祝活动有关")
    if primary.kind == "banked":
        number = re.search(r"\b(a|an|one|two|three|\d+)\s+(?:full\s+)?banked\s+resets?\b", text)
        if number:
            value = {"a": "1", "an": "1", "one": "1", "two": "2", "three": "3"}.get(number[1], number[1])
            extras.append("公告提到 " + value + " 次备用重置机会")
    return result + ("；" + "，".join(extras) if extras else "") + "。"
