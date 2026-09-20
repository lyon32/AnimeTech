"""Release-day reading: both formats of the source, "today since midnight", and doubt = not today."""
from datetime import date, datetime, timedelta, timezone

import pytest

from v2_automation.release_date import release_day, released_today

TZ = timezone(timedelta(hours=2))
NOW = datetime(2026, 9, 20, 14, 30, tzinfo=TZ)                    # Sunday 20 September, 14:30 local


@pytest.mark.parametrize("raw,today", [
    ("3 seconds ago", True), ("16 seconds ago", True), ("2 minutes ago", True), ("1 hour ago", True),
    ("14 hours ago", True),                    # 00:30 today: after midnight
    ("15 hours ago", False),                   # 23:30 yesterday: before midnight
    ("1 day ago", False), ("2 days ago", False), ("1 week ago", False),
    ("just now", True), ("il y a 5 minutes", True), ("il y a 1 jour", False),
    ("September 20, 2026", True), ("September 12, 2026", False), ("September 19, 2026", False),
    ("20 septembre 2026", True), ("12 septembre 2026", False), ("1er septembre 2026", False),
])
def test_both_formats_of_the_source(raw, today):
    assert released_today(raw, NOW, TZ) is today


@pytest.mark.parametrize("raw", [None, "", "  ", "yesterday", "hier", "a month ago", "2 years ago", "soon",
                                 "September 31, 2026", "Smarch 3, 2026", "3 seconds", "20/09/2026", "<i>3 seconds ago</i>"])
def test_unreadable_or_ambiguous_is_never_today(raw):
    assert release_day(raw, NOW, TZ) is None and released_today(raw, NOW, TZ) is False


def test_release_day_values_and_spacing():
    assert release_day("September 12, 2026", NOW, TZ) == date(2026, 9, 12)
    assert release_day("  3   Seconds   AGO ", NOW, TZ) == date(2026, 9, 20)
    assert release_day("15 hours ago", NOW, TZ) == date(2026, 9, 19)


def test_midnight_is_local_not_utc():
    just_after_midnight = datetime(2026, 9, 20, 0, 10, tzinfo=TZ)            # 22:10 UTC on the 19th
    assert released_today("5 minutes ago", just_after_midnight, TZ) is True
    assert released_today("15 minutes ago", just_after_midnight, TZ) is False   # 23:55 yesterday, local
    utc_now = datetime(2026, 9, 19, 22, 10, tzinfo=timezone.utc)               # same instant, given in UTC
    assert released_today("5 minutes ago", utc_now, TZ) is True
