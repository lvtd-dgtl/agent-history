"""Cheap "possibly superseded" hints for drifting operational identifiers.

Retrieval returns what was said, including things later changed. Telling a
reversed decision from its reversal in general needs a model, but the most
damaging case — an operational identifier that moved (a server's address, the
account and host an agent logs in to, a key file, a pinned version) — can be
spotted with regexes, at retrieval time, with no model and no index change.

Among the hits of one search, an older hit is flagged when a newer hit from a
different session states a different value for the same thing and does not
mention the old value at all:

- ``ssh-target``: ``ssh``/``scp``/``rsync``/``sftp`` ... ``user@host`` and
  ``ssh://user@host``. Same user, different host.
- ``version``: ``name 1.2.3`` / ``name@1.2.3`` / ``name v1.2.3``. Same name,
  different version.
- ``ipv4`` and ``ssh-key`` (a ``~/.ssh/...`` path): these carry no name of
  their own, so the two hits must also share a distinctive word near the
  identifier ("fleet-1 is at 10.0.0.5" / "fleet-1 moved to 10.0.0.9").

Guards against the usual false positives: two hits from the same session are
never compared (one conversation legitimately names several hosts), a newer
hit that still names the old value is a mention, not a replacement, and loopback,
unspecified, broadcast and netmask-looking addresses are ignored.

The hint is advisory: nothing is hidden, reordered or deleted.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass

from . import timeaxis

_IPV4 = re.compile(
    r"(?<![\w.])((?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)"
    r"(?:\.(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)){3})(?![\w.]*\d)")
_SSH_TARGET = re.compile(
    r"(?:\b(?:ssh|scp|rsync|sftp|mosh)\b[^\n`]{0,80}?|ssh://)"
    r"(?<![\w.-])([a-z_][a-z0-9_.-]{0,31})@([a-z0-9][a-z0-9.-]*[a-z0-9])",
    re.IGNORECASE)
_SSH_KEY = re.compile(r"(?:~|/home/[\w.-]+|/root|/Users/[\w.-]+)/\.ssh/([\w.-]+)")
_VERSION = re.compile(
    r"\b([a-z][a-z0-9_-]{2,40})(?:\s+v?|@v?|==|\s+version\s+v?)"
    r"(\d+\.\d+\.\d+(?:[-+][0-9a-z.]+)?)\b(?!\.\d)", re.IGNORECASE)

# Too generic to name the thing a version or context belongs to.
_GENERIC = frozenset("""
a an and are as at be but by can do for from has have how i if in into is it
its me my no not of on or our so than that the their them then there these they
this to too us was we were what when where which who why will with would you
version release tag ver v update upgrade upgraded bumped bump now new old
moved changed change use using used server host hostname ip ipv4 address addr
ssh key keys port user username login log machine box vm node instance at via
connect connecting connected run running file path see also set setting
config here run then just still been out
""".split())
_WORD = re.compile(r"[a-z0-9][a-z0-9_.-]*[a-z0-9]|[a-z0-9]", re.IGNORECASE)
CONTEXT_CHARS = 48


@dataclass(frozen=True)
class Identifier:
    kind: str
    anchor: str      # what the value belongs to; "" when only context can tell
    value: str
    context: frozenset


def _ignored_ip(ip: str) -> bool:
    first = int(ip.split(".", 1)[0])
    return (first in (0, 127, 255) or ip.startswith("255.") or ip.endswith(".255")
            or ip.endswith(".0.0") or ip in ("1.1.1.1", "8.8.8.8", "8.8.4.4"))


def _context(text: str, start: int, end: int, skip: set[str]) -> frozenset:
    window = text[max(0, start - CONTEXT_CHARS):start] + " " + text[end:end + CONTEXT_CHARS]
    words = {w.lower().strip(".-_") for w in _WORD.findall(window)}
    return frozenset(w for w in words
                     if w and w not in _GENERIC and w not in skip
                     and not w.replace(".", "").isdigit() and len(w) > 2)


def identifiers(text: str) -> list[Identifier]:
    """Every operational identifier the heuristic understands, in order."""
    text = text or ""
    found: list[Identifier] = []
    seen: set[tuple] = set()

    def add(kind, anchor, value, start, end, skip=()):
        key = (kind, anchor, value)
        if key in seen:
            return
        seen.add(key)
        found.append(Identifier(kind, anchor, value,
                                _context(text, start, end, {anchor, value, *skip})))

    for m in _SSH_TARGET.finditer(text):
        user, host = m.group(1).lower(), m.group(2).lower()
        add("ssh-target", user, host, m.start(1), m.end(2), {host})
    for m in _IPV4.finditer(text):
        ip = m.group(1)
        if not _ignored_ip(ip):
            add("ipv4", "", ip, m.start(1), m.end(1))
    for m in _SSH_KEY.finditer(text):
        add("ssh-key", "", m.group(1), m.start(), m.end())
    for m in _VERSION.finditer(text):
        name = m.group(1).lower()
        if name not in _GENERIC:
            add("version", name, m.group(2).lower(), m.start(1), m.end(2))
    return found


def _mentions(text: str, value: str) -> bool:
    """Whether ``value`` appears in ``text`` as a whole identifier — so
    ``10.0.0.1`` is not found inside ``10.0.0.12``, nor ``winch_deploy``
    inside ``winch_deploy_2026``."""
    pattern = r"(?<![\w.-])" + re.escape(value) + r"(?![\w-]|\.\w)"
    return re.search(pattern, text, re.IGNORECASE) is not None


def _same_thing(old: Identifier, new: Identifier) -> bool:
    if old.kind != new.kind or old.value == new.value:
        return False
    if old.anchor or new.anchor:
        return old.anchor == new.anchor
    return bool(old.context & new.context)


def _session(hit: dict) -> str:
    return (hit.get("session_uid") or hit.get("session_id") or hit.get("session")
            or hit.get("file") or "")


def enabled() -> bool:
    return os.environ.get("AGENT_HISTORY_SUPERSEDE_HINTS", "1").strip().lower() not in (
        "0", "false", "no", "off")


def annotate(hits: list[dict]) -> list[dict]:
    """Add a ``superseded_hint`` to each hit a newer hit appears to replace.

    Returns the same hits (as new dicts where a hint was added), in the same
    order. A hint names the kind, the old and new values, and the newer hit:
    its rank in this list, timestamp, and session.
    """
    # Hints are relative to one list (they name a rank in it), so any that
    # arrived with these hits — a hub's, worked out over its own list — go.
    if any("superseded_hint" in h for h in hits):
        hits = [{k: v for k, v in h.items() if k != "superseded_hint"} for h in hits]
    if not enabled() or len(hits) < 2:
        return hits
    parsed = [(i, h, timeaxis.instant(h.get("timestamp")),
               identifiers(h.get("text") or h.get("excerpt") or ""))
              for i, h in enumerate(hits)]
    out = list(hits)
    for i, hit, when, ids in parsed:
        if not ids or when == float("-inf"):
            continue
        best = None
        for j, other, other_when, other_ids in parsed:
            if j == i or other_when <= when or not other_ids:
                continue
            if _session(other) and _session(other) == _session(hit):
                continue
            other_text = other.get("text") or other.get("excerpt") or ""
            for old in ids:
                if _mentions(other_text, old.value):
                    continue  # the newer hit still mentions it: not a replacement
                for new in other_ids:
                    if _same_thing(old, new):
                        candidate = (other_when, j, old, new)
                        if best is None or candidate[0] > best[0]:
                            best = candidate
        if best is not None:
            _, j, old, new = best
            newer = hits[j]
            out[i] = {**hit, "superseded_hint": {
                "kind": old.kind,
                "old": old.value,
                "new": new.value,
                "by_rank": j + 1,
                "by_timestamp": newer.get("timestamp") or "",
                "by_session": _session(newer),
            }}
    return out


def describe(hint: dict, *, with_rank: bool = True) -> str:
    """One line for people (with the newer hit's rank) and for injected
    context (without: the newer excerpt may not have been injected)."""
    what = {"ssh-target": "login host", "ipv4": "address", "ssh-key": "key file",
            "version": "version"}.get(hint.get("kind"), hint.get("kind", "value"))
    when = (hint.get("by_timestamp") or "")[:10]
    where = f"#{hint.get('by_rank')}, {when}" if with_rank else when
    return (f"possibly superseded: {what} {hint.get('old')} -> {hint.get('new')} "
            f"in a newer statement ({where})")
