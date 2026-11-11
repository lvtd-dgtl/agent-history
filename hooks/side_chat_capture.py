#!/usr/bin/env python3
"""Capture Claude desktop-app "side chats" that write no transcript.

Side chats run as `claude -p ... --fork-session --no-session-persistence`, so
Claude Code never writes a session file for them. UserPromptSubmit and Stop
hooks still fire, though, so this hook appends each turn to
~/.claude/side-chats/<session_id>.jsonl, which the `claude-code-side` adapter
of agent-history indexes.

Stdlib only, no repo imports, safe under `python3 -I`. It never raises, always
exits 0 and does nothing for sessions that have a transcript or were not launched as a side chat. It does not
redact: the indexer does that at ingest.
"""
from __future__ import annotations

import datetime
import json
import os
import re
import sys

MAX_TEXT = 200 * 1024
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{7,127}")
EVENTS = {"UserPromptSubmit": "user", "Stop": "assistant"}
MAX_ANCESTORS = 4


def _read(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def side_chat_launch(read=_read, pid: int | None = None):
    """Look for the side-chat launch signature among the hook's ancestors.

    Returns (is_side_chat, parent_session_id). A side chat is a process whose
    argv has both --no-session-persistence and --fork-session; the parent is
    its `--resume <id>` / `--resume=<id>` value (None if absent or invalid).
    The hook's own parent is `/bin/sh -c`, so claude is a level or two up.
    `read(path) -> bytes` reads /proc files; injectable for tests.
    """
    pid = os.getppid() if pid is None else pid
    for _ in range(MAX_ANCESTORS):
        if pid <= 1:
            return False, None
        try:
            argv = [a.decode("utf-8", "replace")
                    for a in read(f"/proc/{pid}/cmdline").split(b"\0") if a]
        except Exception:
            return False, None
        if "--fork-session" in argv and "--no-session-persistence" in argv:
            for i, arg in enumerate(argv):
                if arg == "--resume" and i + 1 < len(argv):
                    value = argv[i + 1]
                elif arg.startswith("--resume="):
                    value = arg[len("--resume="):]
                else:
                    continue
                return True, (value if ID_RE.fullmatch(value) else None)
            return True, None
        try:
            # /proc/<pid>/stat: "pid (comm) state ppid ..."; comm may hold spaces.
            stat = read(f"/proc/{pid}/stat").decode("utf-8", "replace")
            pid = int(stat.rsplit(")", 1)[1].split()[1])
        except Exception:
            return False, None
    return False, None


def parent_session_id(read=_read, pid: int | None = None) -> str | None:
    return side_chat_launch(read, pid)[1]


def capture(data: dict, home: str | None = None, read=_read,
            env=None, now=None, ppid=None) -> bool:
    """Append one line for this hook event. True if a line was written."""
    env = os.environ if env is None else env
    role = EVENTS.get(data.get("hook_event_name"))
    if role is None:
        return False
    transcript = data.get("transcript_path")
    if not transcript or os.path.exists(transcript):
        return False  # normal session (or nothing to go on)
    session_id = data.get("session_id")
    if not isinstance(session_id, str) or not ID_RE.fullmatch(session_id):
        return False
    # A new normal session can fire its first prompt before its transcript
    # exists, so the missing file alone is not proof: require the launch
    # signature too.
    is_side, parent = side_chat_launch(read, ppid)
    if not is_side:
        return False
    text = data.get("prompt") if role == "user" else data.get("last_assistant_message")
    if not isinstance(text, str) or not text.strip():
        return False
    text = text[:MAX_TEXT]
    stamp = (now or datetime.datetime.now(datetime.timezone.utc)).strftime(
        "%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    record = {
        "v": 1,
        "session_id": session_id,
        "parent_session_id": parent,
        "cwd": data.get("cwd") or env.get("CLAUDE_PROJECT_DIR") or "",
        "timestamp": stamp,
        "role": role,
        "text": text,
        "entrypoint": env.get("CLAUDE_CODE_ENTRYPOINT") or "",
    }
    home = home or os.path.expanduser("~")
    directory = os.path.join(home, ".claude", "side-chats")
    os.makedirs(directory, mode=0o700, exist_ok=True)
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass
    path = os.path.join(directory, session_id + ".jsonl")
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return True


def main() -> int:
    try:
        data = json.loads(sys.stdin.read())
        if isinstance(data, dict):
            capture(data)
    except BaseException:  # never block or fail the chat
        pass
    return 0


if __name__ == "__main__":
    main()
    sys.exit(0)
