"""T3 Code adapter: threads of providers that have no transcript of their own.

T3 Code keeps every thread, whatever provider drives it, in one SQLite store
(``~/.t3/userdata/statev2.sqlite``, WSL and Windows). User and assistant turns
are rows of ``orchestration_v2_projection_messages`` (``payload_json`` holds
the text); the thread's provider is on ``orchestration_v2_projection_threads``
and ``..._provider_threads``; the project directory is
``projection_projects.workspace_root``.

Dedup is the point of this adapter. A T3 Claude thread is ALSO a normal Claude
Code transcript in ~/.claude*/projects (T3 records its session id as the
provider thread's ``nativeThreadRef.nativeId``), and Codex, OpenCode, Hermes
and Grok threads are likewise written by their own CLIs to stores this tool
already reads. Indexing both would put every such conversation in search twice.
So a thread is indexed here only when some provider of it has no readable
native adapter (`NATIVE_DRIVERS`). Today that is Antigravity, whose own
conversation files are encrypted protobuf, and any provider T3 adds that this
tool has no adapter for. A thread handed between providers counts as
uncovered if any of its providers is.

Each record's agent is the provider driver (``antigravity``, ...) and its
origin is ``db:<store>#t3code:<instance>``, so both ``--agent antigravity`` and
the T3 source are visible.

Only named columns of the thread tables are read; the same file holds auth and
pairing tables. Streaming (unfinished) messages are skipped, so a message is
indexed once, in its final form.
"""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Iterator

from .. import config
from ..textproc import MIN_CHARS, scrub
from . import Record
from ._sqlite import open_readonly

AGENT = "t3code"  # the source; records carry the provider driver as agent

# Provider drivers (and instance-name prefixes) whose threads also live in a
# store another adapter reads: claude-code, codex, opencode, hermes, grok.
NATIVE_DRIVERS = ("claude", "codex", "opencode", "hermes", "grok")


def is_native(name: str | None) -> bool:
    """Whether a driver / provider-instance name has its own adapter."""
    if not name:
        return False
    low = name.lower()
    return low.startswith(NATIVE_DRIVERS) or "hermes" in low


class T3CodeAdapter:
    name = AGENT

    def roots(self) -> list[Path]:
        return [p for p in config.t3_state_dbs() if p.is_file()]

    def available(self) -> bool:
        return bool(self.roots())

    def records(self) -> Iterator[Record]:
        for path in self.roots():
            with open_readonly(path) as db:
                if db is None:
                    continue
                try:
                    yield from self._read(db, path)
                except sqlite3.Error:
                    continue

    def _read(self, db: sqlite3.Connection, path: Path) -> Iterator[Record]:
        # One (driver, instance) pair per provider thread; the pair is native
        # if either name is (Hermes over ACP has driver "acp", instance
        # "..._hermes_...").
        drivers: dict[str, list[tuple]] = {}
        for thread_id, driver, instance in db.execute(
                "SELECT thread_id, driver, provider_instance_id "
                "FROM orchestration_v2_projection_provider_threads"):
            drivers.setdefault(thread_id, []).append((driver, instance))
        roots = dict(db.execute(
            "SELECT project_id, workspace_root FROM projection_projects"))

        for tid, project, provider, instance in db.execute(
                "SELECT thread_id, project_id, default_provider, "
                "provider_instance_id FROM orchestration_v2_projection_threads "
                "WHERE deleted_at IS NULL"):
            pairs = drivers.get(tid) or [(provider, instance)]
            uncovered = [(d, i) for d, i in pairs
                         if not (is_native(d) or is_native(i))]
            if not uncovered:
                continue  # a native adapter already indexes this thread
            driver, inst = uncovered[0]
            agent = _agent_name(driver or provider)
            cwd = roots.get(project) or ""
            origin = f"db:{path}#{AGENT}:{inst or instance or provider}"
            for role, created, payload in db.execute(
                    "SELECT role, created_at, payload_json FROM "
                    "orchestration_v2_projection_messages WHERE thread_id = ? "
                    "AND streaming = 0 AND role IN ('user', 'assistant') "
                    "ORDER BY created_at, rowid", (tid,)):
                text = _text(payload).strip()
                if len(text) < MIN_CHARS:
                    continue
                yield Record(agent=agent, origin=origin, session_id=tid,
                             cwd=cwd, timestamp=created or "", role=role,
                             text=scrub(text))


def _agent_name(driver: str | None) -> str:
    """The agent a thread is filed under: its provider driver (antigravity,
    cursor ...), so `--agent antigravity` finds it. The origin keeps `t3code`."""
    name = re.sub(r"[^a-z0-9._-]", "", (driver or "").lower())
    return name[:64] or AGENT


def _text(payload) -> str:
    try:
        data = json.loads(payload)
    except (TypeError, ValueError):
        return ""
    text = data.get("text") if isinstance(data, dict) else None
    return text if isinstance(text, str) else ""
