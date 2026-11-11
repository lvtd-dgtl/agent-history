#!/usr/bin/env python3
"""Capture Claude desktop-app "side chats" that write no transcript.

Side chats run as `claude -p ... --fork-session --no-session-persistence`, so
Claude Code never writes a session file for them. UserPromptSubmit and Stop
hooks still fire, though, so this hook appends each turn to
~/.claude/side-chats/<session_id>.jsonl, which the `claude-code-side` adapter
of agent-history indexes.

Stdlib only, no repo imports, safe under `python3 -I`. It catches everything
and returns 0, and does nothing for sessions that have a transcript, that were
not launched as a side chat, or that did not come from an allowed entrypoint
(CLAUDE_CODE_ENTRYPOINT in AGENT_HISTORY_SIDE_CHAT_ENTRYPOINTS, default
`claude-desktop`). Opt out with AGENT_HISTORY_SIDE_CHATS=off or the marker file
~/.claude/side-chats/.disabled. It does not redact: the indexer does that at
ingest (only when redaction is on; see deploy/side-chats/README.md).
Whether the process can still die before exit 0 (a missing script, no python)
is the managed-settings command's job: see the `; exit 0` there.
"""
from __future__ import annotations

import datetime
import json
import os
import re
import stat
import sys

MAX_TEXT = 200 * 1024
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{7,127}")
# The --resume parent is a Claude Code session uuid (lowercase, 36 chars).
UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
EVENTS = {"UserPromptSubmit": "user", "Stop": "assistant"}
MAX_ANCESTORS = 4
# UNVERIFIED guess for the desktop app; the live test on the MSI confirms it.
# Headless runs (claude -p, the Agent SDK) report `sdk-cli` and never match.
DEFAULT_ENTRYPOINTS = ("claude-desktop",)
O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)


def _read(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def _is_claude(argv: list[str]) -> bool:
    """Is this argv the claude process itself (not a shell or wrapper)?"""
    if not argv:
        return False
    names = [os.path.basename(a.replace("\\", "/")).lower() for a in argv[:2]]
    if names[0] in ("claude", "claude.exe"):
        return True
    if names[0] in ("node", "node.exe", "bun", "bun.exe") and len(names) > 1:
        return names[1] in ("claude", "claude.exe", "cli.js", "claude.js")
    # A launch that carries the side-chat flags is the claude process whatever
    # its argv[0] is called.
    return "--fork-session" in argv and "--no-session-persistence" in argv


def _parent_of(argv: list[str]) -> str | None:
    for i, arg in enumerate(argv):
        if arg in ("--resume", "-r") and i + 1 < len(argv):
            value = argv[i + 1]
        elif arg.startswith("--resume="):
            value = arg[len("--resume="):]
        else:
            continue
        return value if UUID_RE.fullmatch(value) else None
    return None


def side_chat_launch(read=_read, pid: int | None = None):
    """Look for the side-chat launch signature on the nearest claude ancestor.

    Returns (is_side_chat, parent_session_id). Only the NEAREST claude
    process above the hook decides: a normal claude nested under an outer
    fork-session run is not a side chat and must not be mistaken for one. A
    side chat is a claude whose argv has both --no-session-persistence and
    --fork-session; the parent is its `--resume <id>` / `--resume=<id>` /
    `-r <id>` value (None if absent or not a uuid). The hook's own parent is
    `/bin/sh -c`, so claude is a level or two up. Anything odd (unreadable or
    empty cmdline, failing stat, a pid cycle) means "not a side chat".
    `read(path) -> bytes` reads /proc files; injectable for tests.
    """
    pid = os.getppid() if pid is None else pid
    seen = set()
    for _ in range(MAX_ANCESTORS):
        if pid <= 1 or pid in seen:
            return False, None
        seen.add(pid)
        try:
            argv = [a.decode("utf-8", "replace")
                    for a in read(f"/proc/{pid}/cmdline").split(b"\0") if a]
        except Exception:
            return False, None
        if not argv:
            return False, None
        if _is_claude(argv):
            if "--fork-session" in argv and "--no-session-persistence" in argv:
                return True, _parent_of(argv)
            return False, None
        try:
            # /proc/<pid>/stat: "pid (comm) state ppid ..."; comm may hold spaces.
            stat_line = read(f"/proc/{pid}/stat").decode("utf-8", "replace")
            pid = int(stat_line.rsplit(")", 1)[1].split()[1])
        except Exception:
            return False, None
    return False, None


def parent_session_id(read=_read, pid: int | None = None) -> str | None:
    return side_chat_launch(read, pid)[1]


def _entrypoints(env) -> set[str]:
    raw = (env.get("AGENT_HISTORY_SIDE_CHAT_ENTRYPOINTS") or "").strip()
    names = [n.strip() for n in raw.split(",")] if raw else list(DEFAULT_ENTRYPOINTS)
    return {n for n in names if n}


def enabled(env, home: str) -> bool:
    """Opt-out and entrypoint gate; reads nothing from disk but one marker."""
    if (env.get("AGENT_HISTORY_SIDE_CHATS") or "").strip().lower() in (
            "off", "0", "false", "no"):
        return False
    if (env.get("CLAUDE_CODE_ENTRYPOINT") or "") not in _entrypoints(env):
        return False
    # lexists, not exists: a dangling symlink named .disabled still opts out.
    return not os.path.lexists(os.path.join(home, ".claude", "side-chats", ".disabled"))


def _euid() -> int | None:
    return os.geteuid() if hasattr(os, "geteuid") else None


def _safe_dir(home: str) -> str | None:
    """~/.claude/side-chats as a real directory we own, forced to 0700."""
    parent = os.path.join(home, ".claude")
    os.makedirs(parent, mode=0o700, exist_ok=True)
    directory = os.path.join(parent, "side-chats")
    try:
        os.mkdir(directory, 0o700)
    except FileExistsError:
        pass
    st = os.lstat(directory)  # lstat: a symlink is not a directory here
    if not stat.S_ISDIR(st.st_mode):
        return None
    if _euid() is not None and st.st_uid != _euid():
        return None
    if stat.S_IMODE(st.st_mode) != 0o700:
        os.chmod(directory, 0o700)
    return directory


def _append(directory: str, session_id: str, line: str) -> bool:
    path = os.path.join(directory, session_id + ".jsonl")
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | O_NOFOLLOW | O_CLOEXEC,
                 0o600)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            return False  # a hard link could point at someone else's file
        if _euid() is not None and st.st_uid != _euid():
            return False
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        # One write, so concurrent turns cannot interleave inside a line, and
        # a lone surrogate becomes "?" instead of losing the turn.
        os.write(fd, line.encode("utf-8", "replace"))
        return True
    finally:
        os.close(fd)


def capture(data: dict, home: str | None = None, read=_read,
            env=None, now=None, ppid=None) -> bool:
    """Append one line for this hook event. True if a line was written."""
    env = os.environ if env is None else env
    home = home or os.path.expanduser("~")
    if not enabled(env, home):
        return False
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
    directory = _safe_dir(home)
    if directory is None:
        return False
    return _append(directory, session_id,
                   json.dumps(record, ensure_ascii=False) + "\n")


def main() -> int:
    try:
        data = json.loads(sys.stdin.buffer.read().decode("utf-8", "replace"))
        if isinstance(data, dict):
            capture(data)
    except BaseException:  # never block or fail the chat
        pass
    return 0


if __name__ == "__main__":
    main()
    sys.exit(0)
