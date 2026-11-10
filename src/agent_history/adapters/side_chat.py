"""Claude desktop-app side chats, captured by hooks/side_chat_capture.py.

A side chat runs `claude -p --fork-session --no-session-persistence`, so Claude
Code writes no transcript. The capture hook appends one JSON line per turn to
~/.claude/side-chats/<session_id>.jsonl:

    {v, session_id, parent_session_id, cwd, timestamp, role, text, entrypoint}

Each line becomes a Record under agent `claude-code-side`. `fork_of` is the
parent Claude Code session, so the indexer links the two (the parent's agent
is `claude-code`; see sessions.fork_parent_agent). Redaction happens in the
indexer, as for every adapter; the hook stores text verbatim.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

from .. import config
from ..textproc import MIN_CHARS, scrub
from . import Record

AGENT = "claude-code-side"


class SideChatAdapter:
    name = AGENT

    def roots(self) -> list[Path]:
        return [d for d in config.side_chat_dirs() if d.is_dir()]

    def available(self) -> bool:
        return bool(self.roots())

    def records(self) -> Iterator[Record]:
        for root in self.roots():
            for path in sorted(root.glob("*.jsonl")):
                yield from self._read(path)

    def _read(self, path: Path) -> Iterator[Record]:
        try:
            handle = path.open(encoding="utf-8", errors="replace")
        except OSError:
            return
        with handle as lines:
            for line in lines:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(entry, dict) or entry.get("v") != 1:
                    continue
                role = entry.get("role")
                text = entry.get("text")
                if role not in ("user", "assistant") or not isinstance(text, str):
                    continue
                text = text.strip()
                if len(text) < MIN_CHARS:
                    continue
                session_id = entry.get("session_id")
                if not isinstance(session_id, str) or not session_id:
                    session_id = path.stem
                parent = entry.get("parent_session_id")
                if not isinstance(parent, str) or not parent or parent == session_id:
                    parent = None
                yield Record(
                    agent=AGENT,
                    origin=str(path),
                    session_id=session_id,
                    cwd=entry.get("cwd") if isinstance(entry.get("cwd"), str) else "",
                    timestamp=(entry.get("timestamp")
                               if isinstance(entry.get("timestamp"), str) else ""),
                    role=role,
                    text=scrub(text),
                    fork_of=parent,
                )
