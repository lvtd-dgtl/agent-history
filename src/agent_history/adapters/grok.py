"""Grok CLI adapter (xAI, @xai-official/grok).

Grok writes one directory per session under
``~/.grok/sessions/<url-encoded-cwd>/<session-id>/``. `updates.jsonl` is the
authoritative conversation log: one ACP session update per line, shaped
``{"timestamp": <unix s>, "method": "session/update", "params": {"update":
{"sessionUpdate": "user_message_chunk" | "agent_message_chunk" | ..., "content":
{"type": "text", "text": ...}}}}``. Messages arrive as many chunks, so
consecutive chunks of one kind are joined into one message. Reasoning
(`agent_thought_chunk`), tool calls and plans are not conversation and are
skipped. The log is append-only.

A message still streaming when the sweep runs would be indexed as a fragment
and then again whole, so the trailing message of a file that changed within
`settle_seconds` is held back until the next pass.

Only `sessions/` is read. The grok home also holds auth.json (live tokens),
which this adapter never opens; `summary.json` is read only for the working
directory.
"""
from __future__ import annotations

import datetime
import json
import time
import urllib.parse
from pathlib import Path
from typing import Iterator

from .. import config
from ..textproc import MIN_CHARS, scrub
from . import Record

AGENT = "grok"

_ROLES = {"user_message_chunk": "user", "agent_message_chunk": "assistant"}


class GrokAdapter:
    name = AGENT
    settle_seconds = 30.0

    def roots(self) -> list[Path]:
        return [d for d in config.grok_session_dirs() if d.is_dir()]

    def available(self) -> bool:
        return bool(self.roots())

    def records(self) -> Iterator[Record]:
        for root in self.roots():
            for updates in sorted(root.glob("*/*/updates.jsonl")):
                yield from self._read_session(updates)

    def _read_session(self, updates: Path) -> Iterator[Record]:
        session_dir = updates.parent
        session_id = session_dir.name
        cwd = _cwd(session_dir)
        try:
            settled = (time.time() - updates.stat().st_mtime) >= self.settle_seconds
            handle = updates.open(encoding="utf-8", errors="replace")
        except OSError:
            return
        role = ""
        stamp = 0.0
        parts: list[str] = []

        def flush() -> Iterator[Record]:
            text = "".join(parts).strip()
            if role and len(text) >= MIN_CHARS:
                yield Record(agent=AGENT, origin=str(updates),
                             session_id=session_id, cwd=cwd,
                             timestamp=_iso(stamp), role=role, text=scrub(text))

        with handle as lines:
            for line in lines:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                update = ((entry.get("params") or {}).get("update")
                          if isinstance(entry, dict) else None)
                if not isinstance(update, dict):
                    continue
                kind = update.get("sessionUpdate")
                if kind in _ROLES:
                    new_role = _ROLES[kind]
                    if new_role != role:
                        yield from flush()
                        role, parts, stamp = new_role, [], entry.get("timestamp")
                    parts.append(_text(update.get("content")))
                elif kind == "agent_thought_chunk":
                    continue  # reasoning does not end the message in progress
                else:
                    # A tool call, plan or other event closes the message.
                    yield from flush()
                    role, parts = "", []
        if settled:
            yield from flush()


def _text(content) -> str:
    if isinstance(content, dict):
        return content.get("text") if isinstance(content.get("text"), str) else ""
    if isinstance(content, list):
        return "".join(_text(c) for c in content)
    return content if isinstance(content, str) else ""


def _cwd(session_dir: Path) -> str:
    """The session's working directory: summary.json, else the group name."""
    try:
        summary = json.loads((session_dir / "summary.json").read_text(
            encoding="utf-8", errors="replace"))
        info = summary.get("info") if isinstance(summary, dict) else None
        found = info.get("cwd") if isinstance(info, dict) else None
        if isinstance(found, str) and found:
            return found
    except (OSError, ValueError):
        pass
    group = session_dir.parent
    try:  # long paths are a slug + hash, with the original in .cwd
        original = (group / ".cwd").read_text(encoding="utf-8").strip()
        if original:
            return original
    except OSError:
        pass
    return urllib.parse.unquote(group.name)


def _iso(epoch) -> str:
    try:
        return datetime.datetime.fromtimestamp(float(epoch)).isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        return ""
