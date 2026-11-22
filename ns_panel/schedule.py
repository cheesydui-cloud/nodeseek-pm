"""每日存活消息和关键词免打扰的时间判断。"""

from __future__ import annotations

from datetime import datetime


def parse_hhmm(value: str, fallback: tuple[int, int]) -> tuple[int, int]:
    text = (value or "").strip()
    parts = text.split(":")
    if len(parts) != 2:
        return fallback
    try:
        hour = int(parts[0])
        minute = int(parts[1])
    except ValueError:
        return fallback
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        return fallback
    return hour, minute


def valid_hhmm(value: str) -> bool:
    text = (value or "").strip()
    parts = text.split(":")
    if len(parts) != 2 or any(len(part) != 2 for part in parts):
        return False
    try:
        hour = int(parts[0])
        minute = int(parts[1])
    except ValueError:
        return False
    return 0 <= hour <= 23 and 0 <= minute <= 59


def in_quiet_hours(moment: datetime, start: str, end: str) -> bool:
    """跨夜时段按 23:00 到次日 08:00 这样计算。起止相同表示不免打扰。"""
    start_m = _minutes(parse_hhmm(start, (23, 0)))
    end_m = _minutes(parse_hhmm(end, (8, 0)))
    now_m = moment.hour * 60 + moment.minute
    if start_m == end_m:
        return False
    if start_m < end_m:
        return start_m <= now_m < end_m
    return now_m >= start_m or now_m < end_m


def heartbeat_due(moment: datetime, hour: int, last_day: str) -> bool:
    if moment.hour < hour:
        return False
    return (last_day or "") != moment.date().isoformat()


def _minutes(clock: tuple[int, int]) -> int:
    return clock[0] * 60 + clock[1]
