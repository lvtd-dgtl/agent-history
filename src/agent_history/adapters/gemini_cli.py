"""Gemini CLI adapter.

Gemini CLI keeps chats under ``~/.gemini/tmp/<project>/chats/session-*.jsonl``
(the project is a name or hash; ``.project_root`` beside ``chats/`` holds its
directory). The file is a header line (``sessionId``, ``startTime`` ...)
followed by mutation lines: ``{"$set": {"messages": [...]}}`` replaces the
message list, and a bare ``{"id", "type", "content"}`` line adds a message.
The same message id can recur as it is updated, so the file is folded to one
final message per id before anything is yielded. ``type`` is ``user`` or
``gemini``; info/error entries are not conversation.

Gemini CLI opens every session with a ``<session_context>`` user message
(date, OS, directory listing). It is scaffolding, not conversation, so it is
skipped. The a2a-server sessions seen on this machine contain nothing else.

Only ``tmp/*/chats`` is read; oauth_creds.json and the rest of ~/.gemini are
never opened.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

from .. import config
from ..textproc import MIN_CHARS, scrub
from . import Record

AGENT = "gemini-cli"

_ROLES = {"user": "user", "gemini": "assistant"}
SKIP_PREFIXES = ("<session_context>",)


class GeminiCliAdapter:
    name = AGENT

    def roots(self) -> list[Path]:
        return [d for d in config.gemini_cli_dirs() if d.is_dir()]

    def available(self) -> bool:
        return bool(self.roots())

    def records(self) -> Iterator[Record]:
        for root in self.roots():
            for chat in sorted(root.glob("*/chats/*.jsonl")):
                yield from self._read_chat(chat)

    def _read_chat(self, path: Path) -> Iterator[Record]:
        cwd = _project_root(path.parent.parent)
        session_id = path.stem
        messages: dict[str, dict] = {}
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
                if not isinstance(entry, dict):
                    continue
                if isinstance(entry.get("sessionId"), str) and "type" not in entry:
                    session_id = f"{entry['sessionId']}:{path.stem}"
                    continue
                update = entry.get("$set")
                if isinstance(update, dict):
                    if "messages" in update:
                        # A $set that carries messages replaces the list (an
                        # empty one clears it); metadata-only keeps it.
                        messages.clear()
                        for msg in update["messages"] or []:
                            _fold(messages, msg)
                elif "type" in entry:
                    _fold(messages, entry)
        for msg in messages.values():
            role = _ROLES.get(msg.get("type"))
            text = _content(msg.get("content")).strip()
            if (not role or len(text) < MIN_CHARS
                    or text.startswith(SKIP_PREFIXES)):
                continue
            yield Record(agent=AGENT, origin=str(path), session_id=session_id,
                         cwd=cwd, timestamp=msg.get("timestamp") or "",
                         role=role, text=scrub(text))


def _fold(messages: dict[str, dict], msg) -> None:
    if isinstance(msg, dict) and isinstance(msg.get("id"), str):
        messages[msg["id"]] = msg  # dicts keep first-seen order


def _content(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        return content["text"] if isinstance(content.get("text"), str) else ""
    if isinstance(content, list):
        return "\n".join(t for t in map(_content, content) if t)
    return ""


def _project_root(project_dir: Path) -> str:
    try:
        root = (project_dir / ".project_root").read_text(encoding="utf-8").strip()
        if root:
            return root
    except OSError:
        pass
    return project_dir.name
