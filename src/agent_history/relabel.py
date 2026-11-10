"""Label existing Claude Code subagent chunks in the local index.

Chunks indexed before subagent labels existed have NULL `agent_id` /
`sidechain`. A subagent transcript's origin (`.../subagents/agent-<id>.jsonl`)
says what it is, so the label is derivable without the transcript. The agent
type (`attributionAgent`) is only on the transcript lines, so it is filled
when the origin file still exists, reading until the first line that has it.

Local index only: nothing is re-embedded, re-hashed or shipped. Preview is the
default; `--apply` writes.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from . import config, store

_ORIGIN = re.compile(r"[\\/]subagents[\\/]agent-([^\\/]+)\.jsonl$")


def agent_type_of(origin: str) -> str | None:
    """`attributionAgent` from the first line that has one, or None."""
    try:
        with Path(origin).open(encoding="utf-8", errors="replace") as lines:
            for line in lines:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                value = entry.get("attributionAgent") if isinstance(entry, dict) else None
                if isinstance(value, str) and 0 < len(value) <= 200:
                    return value
    except OSError:
        pass
    return None


def run(apply: bool = False) -> int:
    from . import trigger

    if not config.db_path().exists():
        print("no index yet — nothing to relabel")
        return 0
    with trigger._exclusive(config.lock_path()) as acquired:
        if not acquired:
            print("another indexing run holds the lock; retry shortly",
                  file=sys.stderr)
            return 1
        db = store.open_for_maintenance()
        try:
            origins = [row[0] for row in db.execute(
                "SELECT DISTINCT origin FROM chunks WHERE agent = 'claude-code' "
                "AND sidechain IS NULL AND origin LIKE '%agent-%.jsonl'")]
            plan = []
            for origin in origins:
                match = _ORIGIN.search(origin or "")
                if match:
                    plan.append((origin, match.group(1), agent_type_of(origin)))
            chunks = 0
            for origin, agent_id, agent_type in plan:
                n = db.execute("SELECT count(*) FROM chunks WHERE origin = ? "
                               "AND sidechain IS NULL", (origin,)).fetchone()[0]
                chunks += n
                if apply:
                    db.execute(
                        "UPDATE chunks SET agent_id = ?, sidechain = 1, "
                        "agent_type = coalesce(agent_type, ?) "
                        "WHERE origin = ? AND sidechain IS NULL",
                        (agent_id, agent_type, origin))
            if apply:
                db.commit()
            verb = "relabelled" if apply else "would relabel"
            print(f"{verb} {chunks} chunk(s) in {len(plan)} subagent transcript(s)")
            if not apply and plan:
                print("run again with --apply to write")
            return 0
        finally:
            db.close()
