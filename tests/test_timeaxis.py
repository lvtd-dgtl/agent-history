"""The time axis: bounds, windows over mixed timestamp flavours, decay."""
from __future__ import annotations

import datetime

import pytest

from agent_history import timeaxis

NOW = datetime.datetime(2026, 11, 23, 12, 0, tzinfo=datetime.timezone.utc).timestamp()


def local_midnight(day: str) -> float:
    return datetime.datetime.combine(
        datetime.date.fromisoformat(day), datetime.time()).astimezone().timestamp()


def test_relative_ages_count_back_from_now():
    assert timeaxis.parse_bound("30d", end=False, now=NOW) == NOW - 30 * 86400
    assert timeaxis.parse_bound("12h", end=False, now=NOW) == NOW - 12 * 3600
    assert timeaxis.parse_bound("2w", end=False, now=NOW) == NOW - 14 * 86400
    assert timeaxis.parse_bound("6mo", end=False, now=NOW) == NOW - 180 * 86400
    assert timeaxis.parse_bound("1y", end=False, now=NOW) == NOW - 365 * 86400


def test_a_bare_date_starts_or_ends_its_whole_local_day():
    start = timeaxis.parse_bound("2026-10-01", end=False)
    end = timeaxis.parse_bound("2026-10-01", end=True)
    assert start == local_midnight("2026-10-01")
    assert local_midnight("2026-10-02") - 1e-3 < end < local_midnight("2026-10-02")


def test_iso_datetimes_with_and_without_offsets():
    assert timeaxis.parse_bound("2026-10-01T05:00:00Z", end=False) == \
        datetime.datetime(2026, 10, 1, 5, tzinfo=datetime.timezone.utc).timestamp()
    naive = datetime.datetime(2026, 10, 1, 5).astimezone().timestamp()
    assert timeaxis.parse_bound("2026-10-01T05:00:00", end=True) == naive


def test_words():
    assert timeaxis.parse_bound("now", end=False, now=NOW) == NOW
    today = datetime.datetime.fromtimestamp(NOW).date()
    assert timeaxis.parse_bound("today", end=False, now=NOW) == \
        local_midnight(today.isoformat())
    assert timeaxis.parse_bound("yesterday", end=False, now=NOW) == \
        local_midnight((today - datetime.timedelta(days=1)).isoformat())


@pytest.mark.parametrize("bad", ["", "soon", "30", "30m", "2026-13-01", "-5d", "d"])
def test_bad_bounds_are_refused(bad):
    with pytest.raises(timeaxis.TimeError):
        timeaxis.parse_bound(bad, end=False)


def test_minutes_get_a_hint_toward_months():
    with pytest.raises(timeaxis.TimeError, match="mo"):
        timeaxis.parse_span("30m")


def test_spans_must_be_positive():
    with pytest.raises(timeaxis.TimeError):
        timeaxis.parse_span("0d")
    assert timeaxis.parse_span("1.5d") == 1.5 * 86400


def test_until_and_as_of_are_one_bound():
    with pytest.raises(timeaxis.TimeError):
        timeaxis.window(until="2026-10-01", as_of="2026-10-02")
    assert timeaxis.window(as_of="2026-10-01") == timeaxis.window(until="2026-10-01")


def test_since_after_until_is_refused():
    with pytest.raises(timeaxis.TimeError):
        timeaxis.window(since="2026-10-02", until="2026-10-01")


def test_an_empty_window_is_falsy_and_contains_everything():
    window = timeaxis.window()
    assert not window
    assert window.contains("") and window.contains("garbage")


def test_window_compares_mixed_timestamp_flavours_by_instant():
    window = timeaxis.Window(
        since=datetime.datetime(2026, 10, 1, 12, tzinfo=datetime.timezone.utc).timestamp())
    assert window.contains("2026-10-01T12:00:00Z")
    assert window.contains("2026-10-01T13:30:00+01:00")      # 12:30 UTC
    assert not window.contains("2026-10-01T12:30:00+01:00")  # 11:30 UTC
    assert not window.contains(""), "an undated chunk is outside every bounded window"
    assert not window.contains("not a time")


def test_window_args_travel_as_absolute_utc():
    window = timeaxis.window(since="2026-10-01", until="2026-10-31")
    args = window.args()
    assert args[0] == "--since" and args[2] == "--until"
    for value in (args[1], args[3]):
        assert value.endswith("+00:00")
    again = timeaxis.window(since=args[1], until=args[3])
    assert again.since == pytest.approx(window.since)
    assert again.until == pytest.approx(window.until)


def test_decay_halves_each_half_life():
    day = 86400.0
    assert timeaxis.decay(0, 30 * day) == 1.0
    assert timeaxis.decay(30 * day, 30 * day) == pytest.approx(0.5)
    assert timeaxis.decay(60 * day, 30 * day) == pytest.approx(0.25)
    assert timeaxis.decay(-5 * day, 30 * day) == 1.0, "the future counts as now"
    assert timeaxis.decay(float("inf"), 30 * day) == 0.0, "an undated chunk weighs nothing"


def test_a_day_past_the_last_date_is_refused_cleanly():
    # The end of 9999-12-31 is the start of a day that does not exist.
    with pytest.raises(timeaxis.TimeError, match="out of range"):
        timeaxis.parse_bound("9999-12-31", end=True)


@pytest.mark.parametrize("tz", ["UTC", "America/Chicago", "Asia/Tokyo"])
def test_edge_times_parse_or_are_refused_never_raise_otherwise(tz, monkeypatch):
    """Whether a naive local time near year 9999 (or before 1970) still fits
    depends on the zone; either answer is fine, a traceback is not."""
    import time

    if not hasattr(time, "tzset"):
        pytest.skip("cannot switch zones on this platform")
    monkeypatch.setenv("TZ", tz)
    time.tzset()
    try:
        for text in ("9999-12-31T23:59:59", "0001-01-01T00:00:00"):
            try:
                assert isinstance(timeaxis.parse_bound(text, end=True), float)
            except timeaxis.TimeError as exc:
                assert "out of range" in str(exc)
    finally:
        monkeypatch.undo()
        time.tzset()


def test_extreme_stored_timestamps_do_not_raise():
    for value in ("0001-01-01T00:00:00", "9999-12-31T23:59:59"):
        assert isinstance(timeaxis.instant(value), float)
        assert isinstance(timeaxis.local(value), str)
