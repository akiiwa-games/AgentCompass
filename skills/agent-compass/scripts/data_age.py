"""How old a dated fact is and whether it is stale, computed where it is read and never stored with the data.

Reference data holds facts with their dates (`as_of`, the data date, else the fetch date). An age depends on the day it is
read: a file that stored one would differ from day to day with no change in meaning, and a published set would change its
sha256 with the calendar. Every reader that shows an age or warns on one (the refresh's printed report, the tray, doctor)
asks this module; STALE_DAYS is declared here only.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

STALE_DAYS = 21     # a value whose date is older than this is a yellow note: still used, refresh its source


def data_date(as_of) -> date | None:
    """The calendar date of an ISO date or datetime string, or None when it is not one."""
    try:
        return date.fromisoformat(str(as_of)[:10])
    except ValueError:
        return None


def age_days(as_of, now: datetime | None = None) -> int | None:
    """Whole days from `as_of` to `now` (today in UTC when not given); None when `as_of` is not a date."""
    day = data_date(as_of)
    return None if day is None else ((now or datetime.now(timezone.utc)).date() - day).days


def is_stale(as_of, now: datetime | None = None) -> bool:
    """Whether `as_of` is older than STALE_DAYS on `now`; a fact without a readable date is stale."""
    age = age_days(as_of, now)
    return age is None or age > STALE_DAYS
