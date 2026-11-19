"""Claude desktop-app side chats, captured by hooks/side_chat_capture.py.

A side chat runs `claude -p --fork-session --no-session-persistence`, so Claude
Code writes no transcript. The capture hook appends one JSON line per turn to
~/.claude/side-chats/<session_id>.jsonl:

    {v, session_id, parent_session_id, cwd, timestamp, role, text, entrypoint}

Each line becomes a Record under agent `claude-code-side`. `fork_of` is the
parent Claude Code session, so the indexer links the two (the parent's agent
is `claude-code`; see sessions.fork_parent_agent).

Redaction is NOT on by default: the hook stores text verbatim, and the indexer
redacts secrets only when `AGENT_HISTORY_REDACT=on`, strict mode or hub mode is
set. Without that, the local index and the capture files hold plaintext;
`ship` redacts what it sends. These files are written by another process, so
they are read defensively: only regular, non-symlink files owned by this user,
lines whose session_id is the file's own name, bounded line and field sizes.
"""
from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path
from typing import Iterator

from .. import config
from ..textproc import MIN_CHARS, scrub
from . import Record

AGENT = "claude-code-side"
# The session id keeps the loose shape the hook accepts; the parent is the
# Claude Code session uuid from `--resume`.
ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{7,127}")
UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
MAX_LINE = 2 * 1024 * 1024   # bytes; the hook caps text at 200 KB before escaping
MAX_TEXT = 200 * 1024
MAX_CWD = 4096
MAX_TIMESTAMP = 64
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)


def _mine(st: os.stat_result) -> bool:
    """A regular file owned by the user running the indexer."""
    if not stat.S_ISREG(st.st_mode):
        return False
    return not hasattr(os, "getuid") or st.st_uid == os.getuid()


def _lines(path: Path) -> Iterator[bytes]:
    """Lines of a safe capture file; overlong ones are skipped, not read in."""
    try:
        if not _mine(os.lstat(path)):
            return
        fd = os.open(path, os.O_RDONLY | _NOFOLLOW | _NONBLOCK)
    except OSError:
        return
    try:
        if not _mine(os.fstat(fd)):
            os.close(fd)
            return
        handle = os.fdopen(fd, "rb")
    except OSError:
        os.close(fd)
        return
    with handle:
        while True:
            raw = handle.readline(MAX_LINE + 1)
            if not raw:
                return
            if len(raw) > MAX_LINE and not raw.endswith(b"\n"):
                while raw and not raw.endswith(b"\n"):  # drop the rest of it
                    raw = handle.readline(MAX_LINE)
                continue
            yield raw


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
        for raw in _lines(path):
            try:
                entry = json.loads(raw.decode("utf-8", "replace"))
            except (ValueError, RecursionError):
                continue
            if not isinstance(entry, dict) or entry.get("v") != 1:
                continue
            role = entry.get("role")
            text = entry.get("text")
            if role not in ("user", "assistant") or not isinstance(text, str):
                continue
            text = text[:MAX_TEXT].strip()
            if len(text) < MIN_CHARS:
                continue
            session_id = entry.get("session_id")
            # A line may only speak for the session its file is named after.
            if (not isinstance(session_id, str) or not ID_RE.fullmatch(session_id)
                    or session_id != path.stem):
                continue
            parent = entry.get("parent_session_id")
            if (not isinstance(parent, str) or not UUID_RE.fullmatch(parent)
                    or parent == session_id):
                parent = None
            cwd = entry.get("cwd")
            stamp = entry.get("timestamp")
            yield Record(
                agent=AGENT,
                origin=str(path),
                session_id=session_id,
                cwd=cwd[:MAX_CWD] if isinstance(cwd, str) else "",
                timestamp=(stamp if isinstance(stamp, str)
                           and len(stamp) <= MAX_TIMESTAMP else ""),
                role=role,
                text=scrub(text),
                fork_of=parent,
            )
