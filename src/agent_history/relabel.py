"""Label existing Claude Code subagent chunks in the local index.

Chunks indexed before subagent labels existed have NULL `agent_id` /
`sidechain`. A subagent transcript's origin (`.../subagents/agent-<id>.jsonl`)
says what it is, so the label is derivable without the transcript. The agent
type (`attributionAgent`) is only on the transcript lines, so it is filled
when the origin file is under an adapter transcript root and still exists, for
rows of this machine only (a shared index's other machines' rows get the id
and sidechain flag from the origin string; their type stays empty). A row
labelled earlier without a type is filled on a later run.

Local index only: nothing is re-embedded, re-hashed or shipped. Preview is the
default; `--apply` writes. The preview still opens the index through
maintenance, which runs the additive migration and takes the indexing lock.

Undo: relabel only UPDATEs `agent_id`, `agent_type` and `sidechain` where
`sidechain IS NULL` (and `agent_type` where it was NULL). Restore a SQLite
backup, or set those three columns NULL for the affected rows.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from . import config, store
from .adapters import clean_agent_type

_ORIGIN = re.compile(r"[\\/]subagents[\\/]agent-([A-Za-z0-9._:@-]{1,64})\.jsonl\Z")


def _under_a_root(origin: str) -> bool:
    from .adapters.claude_code import ClaudeCodeAdapter
    try:
        path = Path(origin).resolve()
        return any(path.is_relative_to(root.resolve())
                   for root in ClaudeCodeAdapter().roots())
    except (OSError, ValueError):
        return False


def agent_type_of(origin: str) -> str | None:
    """`attributionAgent` from the first line that has a valid one, or None.
    Only files under an adapter transcript root are opened."""
    if not _under_a_root(origin):
        return None
    try:
        with Path(origin).open(encoding="utf-8", errors="replace") as lines:
            for line in lines:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                value = (clean_agent_type(entry.get("attributionAgent"))
                         if isinstance(entry, dict) else None)
                if value:
                    return value
    except (OSError, ValueError):
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
            me = config.machine_id()
            groups = db.execute(
                "SELECT DISTINCT origin, machine FROM chunks "
                "WHERE agent = 'claude-code' AND origin LIKE '%agent-%.jsonl' "
                "AND (sidechain IS NULL OR (sidechain = 1 AND agent_type IS NULL))"
            ).fetchall()
            chunks = transcripts = 0
            for origin, machine in groups:
                match = _ORIGIN.search(origin or "")
                if not match:
                    continue
                agent_id = match.group(1)
                local = machine is None or machine == me
                agent_type = agent_type_of(origin) if local else None
                where = "agent = 'claude-code' AND origin = ? AND machine IS ?"
                key = (origin, machine)
                n = db.execute(
                    f"SELECT count(*) FROM chunks WHERE {where} "
                    "AND sidechain IS NULL", key).fetchone()[0]
                if apply and n:
                    db.execute(
                        "UPDATE chunks SET agent_id = ?, sidechain = 1, "
                        "agent_type = coalesce(agent_type, ?) "
                        f"WHERE {where} AND sidechain IS NULL",
                        (agent_id, agent_type, *key))
                if agent_type:
                    # Second pass: labelled earlier, type unknown until now.
                    m = db.execute(
                        f"SELECT count(*) FROM chunks WHERE {where} "
                        "AND sidechain = 1 AND agent_type IS NULL",
                        key).fetchone()[0]
                    if apply and m:
                        db.execute(
                            "UPDATE chunks SET agent_type = ? "
                            f"WHERE {where} AND sidechain = 1 "
                            "AND agent_type IS NULL", (agent_type, *key))
                    n += m
                if n:
                    chunks += n
                    transcripts += 1
            if apply:
                db.commit()
            verb = "relabelled" if apply else "would relabel"
            print(f"{verb} {chunks} chunk(s) in {transcripts} subagent transcript(s)")
            if not apply and transcripts:
                print("run again with --apply to write")
            return 0
        finally:
            db.close()
