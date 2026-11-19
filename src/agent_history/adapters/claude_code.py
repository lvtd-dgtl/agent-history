"""Claude Code adapter.

Claude Code writes one JSONL file per session under ~/.claude/projects/<slug>/
(or ~/.claude-<name>/projects/ for each other config profile),
one JSON object per line. Subagent transcripts live in nested directories, so
the scan is recursive.

Project memory files (~/.claude/projects/<slug>/memory/*.md) are indexed too:
they are hand-written durable notes, and they are frequently the most useful
thing a search can surface.

A session driven through remote control has a second identity, the
``cse_…`` / ``session_…`` pair of the cloud bridge. Claude Code records it in
the transcript (a ``bridge-session`` line) and in ~/.claude/sessions/*.json
(``bridgeSessionId``); either is hard evidence, and every record of that
session carries it as an alias so the index can link the two.
"""
from __future__ import annotations

import dataclasses
import datetime
import json
import re
from pathlib import Path, PurePath
from typing import Iterator

from .. import config
from ..textproc import MIN_CHARS, scrub
from . import (Record, clean_agent_id, clean_agent_type, clean_session_ref)

AGENT = "claude-code"

# Harness-generated text that is noise in a search index.
SKIP_PREFIXES = (
    "<local-command",
    "<command-",
    "<task-notification>",
    "<system-reminder>",
    "[{'tool_use_id'",
)


class ClaudeCodeAdapter:
    name = AGENT

    def roots(self) -> list[Path]:
        # Every config profile (~/.claude, ~/.claude-org, $CLAUDE_CONFIG_DIR,
        # the WSL Windows side), then any explicitly named extra roots.
        roots = [*config.claude_profile_dirs(), *config.extra_dirs()]
        # Preserve order while dropping duplicates and absent paths.
        seen: set[Path] = set()
        out: list[Path] = []
        for root in roots:
            if root.exists() and root not in seen:
                seen.add(root)
                out.append(root)
        return out

    def available(self) -> bool:
        return bool(self.roots())

    def records(self) -> Iterator[Record]:
        self._bridges = self._session_bridges()
        files = self._transcript_files()
        # Sessions whose transcript this scan covers: only their copies can
        # be dropped from a fork, since only then is the text kept elsewhere.
        known = self._parents = _KnownParents(files)
        for path in files:
            yield from self._read_transcript(path, known)
        for path in self._memory_files():
            yield from self._read_memory(path)

    # ------------------------------------------------------------------ files

    def _transcript_files(self) -> list[Path]:
        files: list[Path] = []
        for root in self.roots():
            files.extend(root.rglob("*.jsonl"))
        return _dedupe(files)

    def _session_bridges(self) -> dict[str, str]:
        """sessionId -> bridgeSessionId from ~/.claude/sessions/*.json.

        Those files describe running sessions; one names its remote-control
        bridge once the session is connected to it.
        """
        out: dict[str, str] = {}
        for root in self.roots():
            directory = root.parent / "sessions"
            if root.name != "projects" or not directory.is_dir():
                continue
            for path in directory.glob("*.json"):
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if not isinstance(data, dict):
                    continue
                sid, bridge = data.get("sessionId"), data.get("bridgeSessionId")
                if isinstance(sid, str) and _bridge_kind(bridge):
                    out[sid] = bridge
        return out

    def transcript_files(self) -> list[Path]:
        return self._transcript_files()

    def memory_files(self) -> list[Path]:
        return self._memory_files()

    def session_bridges(self) -> dict[str, str]:
        return self._session_bridges()

    def read_memory(self, path: Path) -> Iterator[Record]:
        return self._read_memory(path)

    def _memory_files(self) -> list[Path]:
        files: list[Path] = []
        for root in self.roots():
            files.extend(root.glob("*/memory/*.md"))
        return _dedupe(files)

    # ---------------------------------------------------------------- parsing

    def _read_transcript(self, path: Path, known=frozenset(),
                         drop_copies: bool = False) -> Iterator[Record]:
        records, _end, _state = self.read_tail(
            path, 0, parents=known, drop_copies=drop_copies)
        yield from records

    def read_tail(self, path: Path, offset: int = 0,
                  bridges: dict | None = None, *, parents=None,
                  drop_copies: bool = False,
                  ) -> tuple[list[Record], int, dict]:
        """Records from ``path`` starting at byte ``offset``.

        Returns ``(records, end_offset, state)``. ``end_offset`` is the byte
        after the last line consumed: a trailing line that is still being
        written (no newline yet, not valid JSON) is left for the next call, so
        a tail read never loses or splits a record. ``state`` is passed back
        in on the next call (sync keeps it in ``ship.db``): the sessionId ->
        remote-control id map, since the ``bridge-session`` line can sit in a
        different read than the messages it labels, plus, under ``_FORK``,
        what the head of the file said about forks, which a later tail read
        no longer sees.

        ``parents`` holds the sessions whose own file yields entries, so a
        fork's copies of them are dropped (built from this adapter's files
        when not given); ``drop_copies`` drops every copied line (a job never
        ships another session's history).
        """
        out: list[Record] = []
        state = dict(getattr(self, "_bridges", {}))
        state.update(bridges or {})
        fork = dict(state.get(_FORK) or {}) if offset else {}
        if parents is None:
            parents = self._known_parents()
        try:
            handle = path.open("rb")
        except OSError:
            return [], offset, state
        # <proj>/<session>/subagents/agent-<id>.jsonl
        in_subagents = path.parent.name == "subagents"
        file_agent_id = clean_agent_id(
            path.stem[len("agent-"):]
            if in_subagents and path.stem.startswith("agent-") else None)
        end = offset
        with handle:
            handle.seek(offset)
            for raw in handle:
                complete = raw.endswith(b"\n")
                entry = _parse_line(raw)
                if entry is _BAD and not complete:
                    break  # half-written last line: retry from here next time
                end += len(raw)
                if entry is _BAD or not isinstance(entry, dict) \
                        or entry.get("isMeta"):
                    continue
                role = entry.get("type")
                if role == "bridge-session":
                    bridge = entry.get("bridgeSessionId")
                    if entry.get("sessionId") and _bridge_kind(bridge):
                        state[entry["sessionId"]] = bridge
                    continue
                if role not in ("user", "assistant"):
                    continue
                forked = entry.get("forkedFrom")
                if drop_copies and isinstance(forked, dict):
                    continue  # decided on structure: a bad id must not leak a copy
                parent = (clean_session_ref(forked.get("sessionId"))
                          if isinstance(forked, dict) else None)
                if parent and parent == entry.get("sessionId"):
                    parent = None  # a self-reference is not a fork
                if parent:
                    # The last copied line is the fork point, so a fork of a
                    # fork names its direct parent.
                    fork["parent"] = parent
                    if parent in parents:
                        # Copied from a parent indexed from its own file:
                        # those lines belong to the parent, not to this one.
                        continue
                sidechain = bool(entry.get("isSidechain")) or in_subagents
                if role == "user" and not sidechain and not fork.get("first_user"):
                    # Only the first user turn of the main thread may name a
                    # parent (see _brief_parent); a later one never does.
                    fork["first_user"] = True
                    first = next(_texts(entry.get("message") or {}), "")
                    fork["brief"] = _brief_parent(first)
                raw_id = entry.get("agentId")
                # The file name stands in only when the line has no id; a
                # present but malformed id is dropped, not replaced.
                agent_id = ((file_agent_id if raw_id in (None, "")
                             else clean_agent_id(raw_id))
                            if sidechain else None)
                agent_type = (clean_agent_type(entry.get("attributionAgent"))
                              if sidechain else None)
                for text in _texts(entry.get("message") or {}):
                    text = text.strip()
                    if len(text) < MIN_CHARS:
                        continue
                    if text.startswith(SKIP_PREFIXES):
                        continue
                    if not fork.get("session"):
                        fork["session"] = entry.get("sessionId") or path.stem
                    out.append(Record(
                        agent=AGENT,
                        origin=str(path),
                        session_id=entry.get("sessionId") or "",
                        cwd=entry.get("cwd") or "",
                        timestamp=entry.get("timestamp", ""),
                        role=role,
                        text=scrub(text),
                        agent_id=agent_id,
                        agent_type=agent_type,
                        sidechain=sidechain,
                    ))
        fork_of = fork.get("parent")
        brief = fork.get("brief")
        if not fork_of and brief and fork.get("session"):
            # A real fork wins; a self-reference is not a link.
            if brief != fork["session"]:
                fork_of = brief
        # Records are held until the read ends: the bridge-session line can
        # come after messages that belong to the same session.
        final: list[Record] = []
        for record in out:
            if fork_of:
                record = dataclasses.replace(record, fork_of=fork_of)
            bridge = state.get(record.session_id)
            if bridge:
                record = dataclasses.replace(
                    record, aliases=((_bridge_kind(bridge), bridge),))
            final.append(record)
        if fork:
            state[_FORK] = fork
        return final, end, state

    def _known_parents(self) -> "_KnownParents":
        """Parents for a tail read that was not given any: built once per
        adapter from its transcript files, as a full scan would."""
        cached = getattr(self, "_parents", None)
        if cached is None:
            cached = self._parents = _KnownParents(self._transcript_files())
        return cached

    def _read_memory(self, path: Path) -> Iterator[Record]:
        try:
            text = path.read_text(encoding="utf-8", errors="replace").strip()
            mtime = path.stat().st_mtime
        except OSError:
            return
        if len(text) < MIN_CHARS:
            return
        yield Record(
            agent=AGENT,
            origin=str(path),
            session_id=f"memory:{path.stem}",
            cwd=path.parent.parent.name,  # the project slug
            timestamp=datetime.datetime.fromtimestamp(mtime).isoformat(),
            role="memory",
            text=scrub(text),
        )


# Key of the fork state inside read_tail's state dict. Session ids are UUIDs
# (or file stems), so it never collides with a sessionId -> bridge entry.
_FORK = "_fork"

_BAD = object()


def _parse_line(raw: bytes) -> object:
    """One transcript line as JSON, or ``_BAD`` when it is not valid."""
    try:
        return json.loads(raw.decode("utf-8", errors="replace"))
    except ValueError:
        return _BAD


def _bridge_kind(bridge_id: object) -> str | None:
    """Alias kind of a remote-control id: ``cse`` or ``bridge``; None if it
    is not one."""
    if isinstance(bridge_id, str):
        if bridge_id.startswith("cse_") and len(bridge_id) > 4:
            return "cse"
        if bridge_id.startswith("session_") and len(bridge_id) > 8:
            return "bridge"
    return None


class _KnownParents:
    """Session ids whose own transcript file opens and yields at least one
    entry that is not a fork copy. Checked on demand and cached, so only
    parents that a fork actually names are read. An empty, truncated or
    unreadable parent is not known: its copies would be the only record."""

    def __init__(self, files: list[Path]) -> None:
        self._paths: dict[str, list[Path]] = {}
        for path in files:
            self._paths.setdefault(path.stem, []).append(path)
        self._cache: dict[str, bool] = {}

    def __contains__(self, session: object) -> bool:
        if not isinstance(session, str) or session not in self._paths:
            return False
        if session not in self._cache:
            self._cache[session] = any(
                _has_own_entry(p) for p in self._paths[session])
        return self._cache[session]


def _has_own_entry(path: Path) -> bool:
    try:
        with path.open(encoding="utf-8", errors="replace") as lines:
            for line in lines:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if (isinstance(entry, dict) and not entry.get("isMeta")
                        and entry.get("type") in ("user", "assistant")
                        and not isinstance(entry.get("forkedFrom"), dict)):
                    return True
    except OSError:
        return False
    return False


# A fresh-context session (Claude desktop's start_session) has no forkedFrom;
# its brief may name the parent on the first line of the first user message.
_PARENT_LINE = re.compile(
    r"Parent session id: ([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{12})")


def _brief_parent(text: str) -> str | None:
    """The parent id on the first line of ``text``, else None.

    Case-sensitive, nothing else on the line; a BOM and trailing whitespace
    or CRLF are tolerated. Only the strict UUID shape is ever returned.
    """
    first = text.removeprefix("\ufeff").split("\n", 1)[0].rstrip()
    match = _PARENT_LINE.fullmatch(first)
    return match.group(1) if match else None


def profile_of(origin: str) -> str | None:
    """The config profile a Claude Code origin came from.

    "claude" for ~/.claude/projects/..., "claude-org" for
    ~/.claude-org/projects/..., and so on for any .claude* directory. Read
    from the stored path, so it needs no extra column and works for chunks
    indexed before profiles were tracked. None for any other root, including
    a $CLAUDE_CONFIG_DIR or $AGENT_HISTORY_PROJECTS not named .claude*: a
    directory called "projects" alone says nothing about which account wrote it.
    """
    parts = PurePath(origin).parts
    # The last "projects" segment: a home or repo path may contain the word,
    # but Claude Code's own slugs and session directories never equal it.
    for i in range(len(parts) - 1, 0, -1):
        if parts[i] == "projects":
            parent = parts[i - 1]
            if parent == ".claude" or parent.startswith(".claude-"):
                return parent[1:]
            return None
    return None


def _texts(message: dict) -> Iterator[str]:
    """Yield the text of a message, whether it is a plain string or blocks."""
    content = message.get("content")
    if isinstance(content, str):
        yield content
    elif isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                yield block.get("text") or ""


def _dedupe(paths: list[Path]) -> list[Path]:
    """Drop duplicates that arrive via different roots or symlinks."""
    seen: set[Path] = set()
    out: list[Path] = []
    for path in paths:
        try:
            key = path.resolve()
        except OSError:
            key = path
        if key not in seen:
            seen.add(key)
            out.append(path)
    return out
