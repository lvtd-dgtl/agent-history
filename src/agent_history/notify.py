"""`agent-history-notify`: the hook entry point. Tiny on purpose.

A Claude Code hook (Stop, SubagentStop, PreCompact, SessionEnd) runs this. It
reads the hook payload, drops the transcript path into a spool directory
(one file per path, so repeats coalesce and nothing needs a lock), starts the
detached syncer, and returns. It imports almost nothing, so it adds a few
tens of milliseconds to a hook, and it always exits 0 with no output.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys

from . import config

SWEEP = "_sweep"


def payload_paths(payload: object) -> list[str]:
    """Absolute transcript paths named by a hook payload."""
    if not isinstance(payload, dict):
        return []
    out = []
    for key in ("transcript_path", "agent_transcript_path"):
        value = payload.get(key)
        if isinstance(value, str) and value.endswith(".jsonl") \
                and os.path.isabs(value) and value not in out:
            out.append(value)
    return out


def spool(path: str) -> None:
    """Record that ``path`` ('*' for a full sweep) needs syncing. Atomic."""
    directory = config.dirty_dir()
    directory.mkdir(parents=True, exist_ok=True)
    name = SWEEP if path == "*" else hashlib.sha1(
        path.encode("utf-8", "surrogateescape")).hexdigest()[:24]
    tmp = directory / f".{name}.{os.getpid()}"
    tmp.write_text(path, encoding="utf-8")
    os.replace(tmp, directory / name)


def notify(payload: object, *, start: bool = True) -> None:
    for path in payload_paths(payload) or ["*"]:
        spool(path)
    if start:
        from . import trigger
        trigger._relaunch_detached(["sync"])


def main() -> int:
    try:
        raw = "" if sys.stdin is None or sys.stdin.isatty() else sys.stdin.read()
        try:
            payload = json.loads(raw) if raw.strip() else None
        except ValueError:
            payload = None
        notify(payload)
    except Exception:  # noqa: BLE001 - a hook must never fail loudly
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
