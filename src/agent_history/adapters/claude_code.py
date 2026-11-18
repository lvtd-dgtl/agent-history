"""Claude Code adapter.

Claude Code writes one JSONL file per session under ~/.claude/projects/<slug>/
(or ~/.claude-<name>/projects/ for each other config profile),
one JSON object per line. Subagent transcripts live in nested directories, so
the scan is recursive.

Project memory files (~/.claude/projects/<slug>/memory/*.md) are indexed too:
they are hand-written durable notes, and they are frequently the most useful
thing a search can surface.
"""
from __future__ import annotations

import datetime
import json
from pathlib import Path, PurePath
from typing import Iterator

from .. import config
from ..textproc import MIN_CHARS, scrub
from . import Record

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
        for path in self._transcript_files():
            yield from self._read_transcript(path)
        for path in self._memory_files():
            yield from self._read_memory(path)

    # ------------------------------------------------------------------ files

    def _transcript_files(self) -> list[Path]:
        files: list[Path] = []
        for root in self.roots():
            files.extend(root.rglob("*.jsonl"))
        return _dedupe(files)

    def _memory_files(self) -> list[Path]:
        files: list[Path] = []
        for root in self.roots():
            files.extend(root.glob("*/memory/*.md"))
        return _dedupe(files)

    # ---------------------------------------------------------------- parsing

    def _read_transcript(self, path: Path) -> Iterator[Record]:
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
                if not isinstance(entry, dict) or entry.get("isMeta"):
                    continue
                role = entry.get("type")
                if role not in ("user", "assistant"):
                    continue
                for text in _texts(entry.get("message") or {}):
                    text = text.strip()
                    if len(text) < MIN_CHARS:
                        continue
                    if text.startswith(SKIP_PREFIXES):
                        continue
                    yield Record(
                        agent=AGENT,
                        origin=str(path),
                        session_id=entry.get("sessionId") or "",
                        cwd=entry.get("cwd") or "",
                        timestamp=entry.get("timestamp", ""),
                        role=role,
                        text=scrub(text),
                    )

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


def profile_of(origin: str) -> str | None:
    """The config profile a Claude Code origin came from.

    "claude" for ~/.claude/projects/..., "claude-org" for
    ~/.claude-org/projects/..., the directory name for a $CLAUDE_CONFIG_DIR
    elsewhere. Read from the stored path, so it needs no extra column and
    works for chunks indexed before profiles were tracked. None when the
    origin is not under a <profile>/projects/ directory (e.g. a custom
    $AGENT_HISTORY_PROJECTS root).
    """
    parts = PurePath(origin).parts
    # The last "projects" segment: a home or repo path may contain the word,
    # but Claude Code's own slugs and session directories never equal it.
    for i in range(len(parts) - 1, 0, -1):
        if parts[i] == "projects":
            name = parts[i - 1].lstrip(".")
            return name or None
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
