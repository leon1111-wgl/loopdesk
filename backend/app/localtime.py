"""The tenant's local time. Timestamps are stored and compared in UTC; dates and times shown to
people (dashboard days, order dates told to customers, exports) are in the tenant's zone, so a
delivery at 06:30 Beijing time is not reported as the day before (review round 3, N-024)."""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo, available_timezones

DEFAULT_TIMEZONE = "Asia/Shanghai"


def as_utc(value: datetime) -> datetime:
    """SQLite drops tzinfo on read; every timestamp in this app is written in UTC."""
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def zone_of(tenant) -> ZoneInfo:
    return ZoneInfo(getattr(tenant, "timezone", None) or DEFAULT_TIMEZONE)


def is_valid_zone(name: str) -> bool:
    """IANA names only (``Asia/Shanghai``, ``UTC``): ZoneInfo would also try odd paths."""
    return name in available_timezones()


def local_date(value: datetime, zone: ZoneInfo) -> str:
    return as_utc(value).astimezone(zone).strftime("%Y-%m-%d")


def local_time(value: datetime | None, zone: ZoneInfo) -> str:
    return as_utc(value).astimezone(zone).strftime("%Y-%m-%d %H:%M:%S") if value else ""


def start_of_day(day: date, zone: ZoneInfo) -> datetime:
    """Local midnight of ``day``, as a UTC instant for queries."""
    return datetime.combine(day, time.min, tzinfo=zone).astimezone(timezone.utc)
