"""hooks/side_chat_capture.py: capture rules and parent discovery."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parent.parent / "hooks" / "side_chat_capture.py"
spec = importlib.util.spec_from_file_location("side_chat_capture", HOOK)
hook = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hook)

SID = "ecf3deb0-1111-4222-8333-444455556666"
PARENT = "e18e5130-5211-4d3c-8162-addd565dd983"


def fake_proc(chain: dict[int, tuple[int, list[str]]]):
    """A /proc reader: pid -> (ppid, argv)."""
    def read(path: str) -> bytes:
        pid, name = path.split("/")[2:]
        ppid, argv = chain[int(pid)]
        if name == "cmdline":
            return b"\0".join(a.encode() for a in argv) + b"\0"
        return f"{pid} (my proc) S {ppid} 0 0".encode()
    return read


def chain(resume_args):
    return fake_proc({
        50: (40, ["/bin/sh", "-c", "python3 -I hook.py"]),
        40: (30, ["claude", "-p", "--fork-session", "--no-session-persistence", *resume_args]),
        30: (1, ["bash"]),
    })


@pytest.mark.parametrize("args", [["--resume", PARENT], [f"--resume={PARENT}"]])
def test_parent_from_both_resume_forms(args):
    assert hook.parent_session_id(chain(args), pid=50) == PARENT


def test_launch_signature_needs_both_flags():
    only_nsp = fake_proc({50: (1, ["claude", "-p", "--no-session-persistence",
                                   "--resume", PARENT])})
    assert hook.side_chat_launch(only_nsp, pid=50) == (False, None)
    only_fork = fake_proc({50: (1, ["claude", "--fork-session", "--resume", PARENT])})
    assert hook.side_chat_launch(only_fork, pid=50) == (False, None)
    assert hook.side_chat_launch(chain(["--resume", PARENT]), pid=50) == (True, PARENT)


def test_side_chat_without_resume_is_captured_unlinked(tmp_path):
    read = fake_proc({50: (1, ["claude", "--fork-session", "--no-session-persistence"])})
    assert run(tmp_path, event(tmp_path), read=read)
    assert lines(tmp_path)[0]["parent_session_id"] is None


def test_new_normal_session_first_prompt_is_not_captured(tmp_path):
    # transcript not yet written, but no side-chat flags on the claude process
    normal = fake_proc({50: (40, ["/bin/sh", "-c", "hook"]),
                        40: (1, ["claude", "--model", "opus"])})
    assert not run(tmp_path, event(tmp_path, "UserPromptSubmit"), read=normal)
    nsp = fake_proc({50: (1, ["claude", "-p", "--no-session-persistence"])})
    assert not run(tmp_path, event(tmp_path, "UserPromptSubmit"), read=nsp)
    assert not (tmp_path / ".claude").exists()


def test_no_fork_flag_or_no_resume_means_no_parent():
    plain = fake_proc({50: (40, ["sh"]), 40: (1, ["claude", "-p", "--resume", PARENT])})
    assert hook.parent_session_id(plain, pid=50) is None
    noresume = fake_proc({50: (1, ["claude", "--fork-session", "--no-session-persistence"])})
    assert hook.parent_session_id(noresume, pid=50) is None


def test_unreadable_proc_is_none():
    def boom(path):
        raise OSError("gone")
    assert hook.parent_session_id(boom, pid=50) is None


def event(tmp_path, name="Stop", transcript=None, **extra):
    data = {"hook_event_name": name, "session_id": SID, "cwd": "/work",
            "transcript_path": str(transcript if transcript is not None
                                   else tmp_path / "missing.jsonl")}
    if name == "Stop":
        data["last_assistant_message"] = "the reply to the side question"
    else:
        data["prompt"] = "a side question about caching"
    data.update(extra)
    return data


def lines(tmp_path):
    path = tmp_path / ".claude/side-chats" / f"{SID}.jsonl"
    return [json.loads(x) for x in path.read_text().splitlines()]


def run(tmp_path, data, **kw):
    kw.setdefault("read", chain(["--resume", PARENT]))
    kw.setdefault("ppid", 50)
    kw.setdefault("env", {"CLAUDE_CODE_ENTRYPOINT": "claude-desktop"})
    return hook.capture(data, home=str(tmp_path), **kw)


def test_missing_transcript_writes_both_roles(tmp_path):
    assert run(tmp_path, event(tmp_path, "UserPromptSubmit"))
    assert run(tmp_path, event(tmp_path, "Stop"))
    first, second = lines(tmp_path)
    assert (first["role"], second["role"]) == ("user", "assistant")
    assert first["v"] == 1 and first["session_id"] == SID
    assert first["parent_session_id"] == PARENT
    assert first["entrypoint"] == "claude-desktop"
    assert first["text"] == "a side question about caching"
    assert first["timestamp"].endswith("Z")


@pytest.mark.skipif(sys.platform == "win32", reason="Windows has no POSIX mode bits")
def test_files_are_private_on_posix(tmp_path):
    assert run(tmp_path, event(tmp_path))
    d = tmp_path / ".claude/side-chats"
    assert oct(d.stat().st_mode & 0o777) == "0o700"
    assert oct((d / f"{SID}.jsonl").stat().st_mode & 0o777) == "0o600"


def test_existing_or_empty_transcript_writes_nothing(tmp_path):
    exists = tmp_path / "real.jsonl"
    exists.write_text("{}")
    assert not run(tmp_path, event(tmp_path, transcript=exists))
    assert not run(tmp_path, event(tmp_path, transcript=""))
    assert not (tmp_path / ".claude").exists()


@pytest.mark.parametrize("bad", ["../../etc/x", "a/b", "", "x", "a" * 200, ".hidden-id",
                                 "/etc/passwd-abcdefgh", "..\\..\\evil-abcdefgh",
                                 "abcdefgh\0../x", "C:\\evil\\abcdefgh", None, 12345678])
def test_bad_session_id_writes_nothing(tmp_path, bad):
    assert not run(tmp_path, event(tmp_path, session_id=bad))
    assert not (tmp_path / ".claude").exists()


def test_other_events_and_empty_text_are_ignored(tmp_path):
    assert not run(tmp_path, event(tmp_path, "SessionStart"))
    assert not run(tmp_path, event(tmp_path, last_assistant_message="  "))


def test_text_is_capped(tmp_path):
    run(tmp_path, event(tmp_path, last_assistant_message="x" * (hook.MAX_TEXT + 500)))
    assert len(lines(tmp_path)[0]["text"]) == hook.MAX_TEXT


def test_script_exits_zero_on_garbage_and_writes_for_real(tmp_path):
    env = {**os.environ, "HOME": str(tmp_path)}
    for stdin in ("not json", "", "[1]"):
        r = subprocess.run([sys.executable, "-I", str(HOOK)], input=stdin,
                           text=True, env=env, capture_output=True)
        assert r.returncode == 0 and r.stdout == "" and r.stderr == ""
    # real run: this pytest process is no side chat, so nothing is written
    r = subprocess.run([sys.executable, "-I", str(HOOK)], text=True, env=env,
                       input=json.dumps(event(tmp_path)), capture_output=True)
    assert r.returncode == 0
    assert not (tmp_path / ".claude").exists()
