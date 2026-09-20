"""Release day of an episode, read from the date text the source shows next to it.

The source writes a date in two ways:
  * recent episodes, relative:   "3 seconds ago", "2 minutes ago", "5 hours ago", "1 day ago"
  * older episodes, absolute:    "September 12, 2026"   (also read in French: "12 septembre 2026")

`release_day` returns the LOCAL calendar day of the release, or None when the text cannot be read with
certainty.  `released_today` is what the catch-up rule uses: unreadable or ambiguous -> False, so an episode
is never published on a doubtful date.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, tzinfo

_UNITS = {"second": 1, "sec": 1, "minute": 60, "min": 60, "hour": 3600, "hr": 3600, "day": 86400,
          "week": 7 * 86400}
_REL = re.compile(r"^(\d+)\s*(second|sec|minute|min|hour|hr|day|week)s?\s+ago$", re.I)
_FR_REL = re.compile(r"^il y a\s+(\d+)\s*(seconde|minute|heure|jour|semaine)s?$", re.I)
_FR_UNITS = {"seconde": 1, "minute": 60, "heure": 3600, "jour": 86400, "semaine": 7 * 86400}
_JUST_NOW = {"just now", "now", "à l'instant", "a l'instant", "maintenant", "à l’instant"}

_EN_MONTHS = {m: i for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july", "august",
                                          "september", "october", "november", "december"], start=1)}
_FR_MONTHS = {m: i for i, m in enumerate(["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août",
                                          "septembre", "octobre", "novembre", "décembre"], start=1)}
_FR_MONTHS.update({"fevrier": 2, "aout": 8, "decembre": 12})
_EN_DATE = re.compile(r"^([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})$")
_FR_DATE = re.compile(r"^(\d{1,2})(?:er)?\s+([A-Za-zéûôàè]+)\s+(\d{4})$")


def _local_now(now: datetime | None, tz: tzinfo | None) -> datetime:
    now = now or datetime.now().astimezone()
    if now.tzinfo is None:
        now = now.astimezone()
    return now.astimezone(tz) if tz is not None else now


def release_day(raw: str | None, now: datetime | None = None, tz: tzinfo | None = None) -> date | None:
    """Local calendar day of the release, or None when the text is not a date we understand for sure."""
    text = " ".join((raw or "").split())
    if not text:
        return None
    local = _local_now(now, tz)
    low = text.lower()
    if low in _JUST_NOW:
        return local.date()
    m = _REL.match(low)
    if m:
        return (local - timedelta(seconds=int(m.group(1)) * _UNITS[m.group(2)])).date()
    m = _FR_REL.match(low)
    if m:
        return (local - timedelta(seconds=int(m.group(1)) * _FR_UNITS[m.group(2)])).date()
    m = _EN_DATE.match(text)
    if m and m.group(1).lower() in _EN_MONTHS:
        return _safe_date(int(m.group(3)), _EN_MONTHS[m.group(1).lower()], int(m.group(2)))
    m = _FR_DATE.match(low)
    if m and m.group(2) in _FR_MONTHS:
        return _safe_date(int(m.group(3)), _FR_MONTHS[m.group(2)], int(m.group(1)))
    return None                                     # months / years ago, odd formats: not sure -> not a day


def _safe_date(y: int, m: int, d: int) -> date | None:
    try:
        return date(y, m, d)
    except ValueError:
        return None


def released_today(raw: str | None, now: datetime | None = None, tz: tzinfo | None = None) -> bool:
    day = release_day(raw, now, tz)
    return day is not None and day == _local_now(now, tz).date()
