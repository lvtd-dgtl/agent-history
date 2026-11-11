"""Source adapters.

Each adapter knows how one agent stores its transcripts and turns that into a
stream of `Record`s. Adding support for another agent means adding one module
here and registering it below — the indexer and search layer never change.

Transcripts come in two shapes in the wild: append-only JSONL files, and SQLite
databases. `Record.origin` accommodates both — a file path, or `db:<path>#<table>`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Protocol, runtime_checkable

from .. import config


_AGENT_ID = re.compile(r"[A-Za-z0-9._:@-]{1,64}")
_AGENT_TYPE = re.compile(r"[A-Za-z0-9._:@/ -]{1,64}")
_SESSION_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{7,127}")


def _clean(pattern: re.Pattern, value: object) -> str | None:
    return value if isinstance(value, str) and pattern.fullmatch(value) else None


def clean_agent_id(value: object) -> str | None:
    """A subagent id, or None for anything not shaped like one (non-string,
    control characters, ANSI escapes, newlines, over 64 characters)."""
    return _clean(_AGENT_ID, value)


def clean_agent_type(value: object) -> str | None:
    """A subagent type label, or None; same rules as `clean_agent_id`."""
    return _clean(_AGENT_TYPE, value)


def clean_session_ref(value: object) -> str | None:
    """A session id named by a fork link, or None if it is not id-shaped."""
    return _clean(_SESSION_REF, value)


@dataclass(frozen=True)
class Record:
    """One message, normalised across agents."""

    agent: str
    origin: str
    session_id: str
    cwd: str
    timestamp: str  # ISO 8601
    role: str
    text: str
    # Which machine the message came from. None means "this one"; only the hub
    # inbox sets it, from the directory a shipper uploaded into.
    machine: str | None = None
    # The session's uid when the source already knows it — only shipped
    # records carry one; everything else is derived (see sessions.py).
    session_uid: str | None = None
    # (kind, value) links to the session's other ids, found on hard
    # evidence by the adapter, e.g. ("cse", "cse_01…") for remote control.
    aliases: tuple[tuple[str, str], ...] = ()
    # Claude Code subagent transcripts: the subagent's id and type, and
    # whether the message came from a sidechain. They share the parent's
    # session id; these fields keep them tellable apart.
    agent_id: str | None = None
    agent_type: str | None = None
    sidechain: bool = False
    # The session this one was forked from (its source session id).
    fork_of: str | None = None


@runtime_checkable
class Adapter(Protocol):
    name: str

    def available(self) -> bool:
        """Whether this agent's data exists on this machine."""

    def roots(self) -> list[Path]:
        """Directories and files to watch for changes."""

    def records(self) -> Iterator[Record]:
        """Every indexable message this agent has stored."""


# Agents whose records arrive only through `agent-history import` (a data
# export, read once on demand) rather than a registered adapter. A rebuild
# from disk cannot recreate them, so `reindex` treats them like deleted
# sources and refuses to wipe them without --force.
IMPORT_ONLY_AGENTS = frozenset({"claude-ai"})


def all_adapters() -> list[Adapter]:
    from .claude_code import ClaudeCodeAdapter
    from .codex import CodexAdapter
    from .docs import DocsAdapter
    from .hermes import HermesAdapter
    from .inbox import InboxAdapter
    from .openclaw import OpenClawAdapter
    from .opencode import OpenCodeAdapter

    # The hub indexes what machines ship it and nothing local: whatever
    # transcripts happen to sit on that box are not its history.
    if config.hub_mode():
        return [InboxAdapter()]
    return [
        ClaudeCodeAdapter(),
        CodexAdapter(),
        OpenCodeAdapter(),
        HermesAdapter(),
        OpenClawAdapter(),
        DocsAdapter(),
        InboxAdapter(),
    ]


def available_adapters() -> list[Adapter]:
    return [a for a in all_adapters() if a.available()]
