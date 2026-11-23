"""The time axis: date bounds, half-lives, and comparable instants.

Every chunk carries an ISO-8601 timestamp, but adapters write two flavours —
UTC with an offset (``...Z``, ``+00:00``) and naive local time — so stored
strings cannot be compared as text. Everything here turns them into epoch
seconds first.

User-facing values, as accepted by ``--since``, ``--until``, ``--as-of`` and
``--recency``:

- a relative age: ``12h``, ``30d``, ``2w``, ``6mo``, ``1y`` (that long before now)
- ``now``, ``today``, ``yesterday``
- an ISO date, ``2026-10-01`` — local time; a lower bound starts the day and an
  upper bound ends it, so ``--until 2026-10-01`` includes all of that day
- an ISO date-time, with or without an offset (naive is local time)

A half-life takes only the relative form.
"""
from __future__ import annotations

import datetime
import re
import time
from dataclasses import dataclass

UNITS = {"h": 3600.0, "d": 86400.0, "w": 7 * 86400.0,
         "mo": 30 * 86400.0, "y": 365 * 86400.0}
_RELATIVE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(h|d|w|mo|y)\s*$", re.IGNORECASE)
_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class TimeError(ValueError):
    """A date bound or half-life that cannot be read."""


def instant(timestamp: str | None) -> float:
    """A comparable epoch for an ISO timestamp of any stored flavour.

    Naive values are taken as local time (that is how they were written);
    empty or unparseable ones are ``-inf``, so they sort last and fall outside
    every bounded window.
    """
    if not timestamp:
        return float("-inf")
    try:
        parsed = datetime.datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.astimezone()
        return parsed.timestamp()
    except (ValueError, OverflowError, OSError):
        return float("-inf")


def iso(epoch: float) -> str:
    """An unambiguous UTC ISO string for an epoch — what travels to a hub."""
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).isoformat()


def local(timestamp: str | None, length: int = 16) -> str:
    """A stored timestamp shown in this machine's local time, ``YYYY-MM-DD
    HH:MM`` cut to ``length`` — so a fact set ``--since 2026-10-11`` reads
    2026-10-11 wherever the clock is, though it is stored in UTC."""
    when = instant(timestamp)
    if when == float("-inf"):
        return (timestamp or "")[:length]
    try:
        return datetime.datetime.fromtimestamp(when).strftime("%Y-%m-%d %H:%M")[:length]
    except (ValueError, OverflowError, OSError):
        return (timestamp or "")[:length]


def _span(text: str) -> float | None:
    match = _RELATIVE.match(text)
    if not match:
        return None
    return float(match.group(1)) * UNITS[match.group(2).lower()]


def parse_span(text: str) -> float:
    """Seconds in a relative span such as ``30d``; TimeError otherwise."""
    seconds = _span(text or "")
    if seconds is None:
        hint = " (use 'mo' for months)" if re.match(r"^\s*\d+\s*m\s*$", text or "") else ""
        raise TimeError(f"not a span: {text!r}; use e.g. 12h, 30d, 2w, 6mo, 1y{hint}")
    if seconds <= 0:
        raise TimeError(f"span must be positive: {text!r}")
    return seconds


def parse_bound(text: str, *, end: bool, now: float | None = None) -> float:
    """Epoch for one side of a window. ``end`` makes a bare date cover its
    whole day (an upper bound); otherwise it starts at local midnight."""
    try:
        return _parse_bound(text, end=end, now=now)
    except TimeError:
        raise
    except (ValueError, OverflowError, OSError) as exc:  # year 9999 + 1 day,
        raise TimeError(f"date out of range: {text!r}") from exc  # pre-1970 on Windows


def _parse_bound(text: str, *, end: bool, now: float | None) -> float:
    now = time.time() if now is None else now
    raw = (text or "").strip()
    if not raw:
        raise TimeError("empty date")
    span = _span(raw)
    if span is not None:
        return now - span
    word = raw.lower()
    if word == "now":
        return now
    if word in ("today", "yesterday"):
        day = datetime.datetime.fromtimestamp(now).date()
        if word == "yesterday":
            day -= datetime.timedelta(days=1)
        raw = day.isoformat()
    if _DATE_ONLY.match(raw):
        try:
            day = datetime.date.fromisoformat(raw)
        except ValueError as exc:
            raise TimeError(f"not a date: {text!r}") from exc
        if end:
            day += datetime.timedelta(days=1)
        start = datetime.datetime.combine(day, datetime.time()).astimezone()
        return start.timestamp() - (1e-6 if end else 0.0)
    try:
        parsed = datetime.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise TimeError(
            f"not a date: {text!r}; use YYYY-MM-DD, an ISO date-time, "
            "today/yesterday/now, or an age like 30d") from exc
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed.timestamp()


@dataclass(frozen=True)
class Window:
    """An inclusive time window. Either side may be open."""

    since: float | None = None
    until: float | None = None

    def __bool__(self) -> bool:
        return self.since is not None or self.until is not None

    def contains(self, timestamp: str | None) -> bool:
        if not self:
            return True
        when = instant(timestamp)
        if when == float("-inf"):
            return False  # an undated chunk cannot be shown to be inside
        if self.since is not None and when < self.since:
            return False
        return self.until is None or when <= self.until

    def args(self) -> list[str]:
        """The window as absolute CLI flags, so a hub in another time zone
        reads exactly the same instants."""
        out = []
        if self.since is not None:
            out += ["--since", iso(self.since)]
        if self.until is not None:
            out += ["--until", iso(self.until)]
        return out

    def describe(self) -> str:
        parts = []
        if self.since is not None:
            parts.append(f"since {iso(self.since)[:19]}Z")
        if self.until is not None:
            parts.append(f"until {iso(self.until)[:19]}Z")
        return ", ".join(parts)


def window(since: str | None = None, until: str | None = None,
           as_of: str | None = None, *, now: float | None = None) -> Window:
    """A Window from user strings. ``as_of`` is ``until`` by another name —
    "the past as it stood" — and naming both is an error."""
    if until and as_of:
        raise TimeError("--until and --as-of are the same bound; pass one")
    upper = until or as_of
    low = parse_bound(since, end=False, now=now) if since else None
    high = parse_bound(upper, end=True, now=now) if upper else None
    if low is not None and high is not None and low > high:
        raise TimeError("--since is after --until")
    return Window(low, high)


def decay(age_seconds: float, half_life: float) -> float:
    """Weight for something ``age_seconds`` old: 1.0 now, 0.5 at one
    half-life, 0.25 at two. Future timestamps count as age zero."""
    if age_seconds != age_seconds or age_seconds == float("inf"):  # NaN or undated
        return 0.0
    return 0.5 ** (max(age_seconds, 0.0) / half_life)
