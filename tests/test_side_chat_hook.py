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
                                 "abcdefgh\0../x", "abcdefgh\n", "abcdefgh\r\n", "C:\\evil\\abcdefgh", None, 12345678])
def test_bad_session_id_writes_nothing(tmp_path, bad):
    assert not run(tmp_path, event(tmp_path, session_id=bad))
    assert not (tmp_path / ".claude").exists()


@pytest.mark.parametrize("value", [PARENT + "\n", PARENT + "\r\n", "abcdefgh\n"])
@pytest.mark.parametrize("form", ["split", "equals"])
def test_parent_id_with_trailing_newline_is_no_parent(value, form):
    args = ["--resume", value] if form == "split" else [f"--resume={value}"]
    assert hook.side_chat_launch(chain(args), pid=50) == (True, None)


def test_other_events_and_empty_text_are_ignored(tmp_path):
    assert not run(tmp_path, event(tmp_path, "SessionStart"))
    assert not run(tmp_path, event(tmp_path, last_assistant_message="  "))


def test_text_is_capped(tmp_path):
    run(tmp_path, event(tmp_path, last_assistant_message="x" * (hook.MAX_TEXT + 500)))
    assert len(lines(tmp_path)[0]["text"]) == hook.MAX_TEXT


def test_script_exits_zero_on_garbage(tmp_path):
    env = {**os.environ, "HOME": str(tmp_path)}
    for stdin in ("not json", "", "[1]", "\udcff\x00{"):
        r = subprocess.run([sys.executable, "-I", str(HOOK)], input=stdin,
                           text=True, env=env, capture_output=True)
        assert r.returncode == 0 and r.stdout == "" and r.stderr == ""
    r = subprocess.run([sys.executable, "-I", str(HOOK)], input=b"\xff\xfe{",
                       env=env, capture_output=True)
    assert r.returncode == 0 and r.stdout == b"" and r.stderr == b""
    assert not (tmp_path / ".claude").exists()


# --- H1: the managed-settings command can never block a prompt ---------------

DROPIN = (Path(__file__).resolve().parent.parent / "deploy" / "managed-settings.d"
          / "agent-history-side-chats.json")
INSTALLED = "/usr/local/lib/agent-history/side_chat_capture.py"


def dropin_commands():
    data = json.loads(DROPIN.read_text())  # also proves the JSON is valid
    return [h["command"] for event in ("UserPromptSubmit", "Stop")
            for group in data["hooks"][event] for h in group["hooks"]]


@pytest.mark.skipif(sys.platform == "win32", reason="the command is a POSIX shell line")
def test_dropin_command_exits_zero_when_script_is_missing(tmp_path):
    """A missing script made `python3 -I` exit 2, which Claude Code treats as
    blocking: every prompt on the machine would be rejected."""
    cmds = dropin_commands()
    assert len(cmds) == 2 and all(INSTALLED in c for c in cmds)
    for cmd in cmds:
        missing = cmd.replace(INSTALLED, str(tmp_path / "nope.py"))
        assert missing != cmd
        r = subprocess.run(missing, shell=True, input="{}", text=True,
                           capture_output=True)
        assert (r.returncode, r.stdout, r.stderr) == (0, "", "")
        # and exactly as shipped, whether or not the script is installed here
        r = subprocess.run(cmd, shell=True, input="{}", text=True, capture_output=True)
        assert r.returncode == 0 and r.stdout == ""


@pytest.mark.skipif(sys.platform == "win32", reason="the command is a POSIX shell line")
def test_dropin_command_exits_zero_with_real_script_on_garbage_stdin(tmp_path):
    for cmd in dropin_commands():
        real = cmd.replace(INSTALLED, str(HOOK))
        for stdin in ("not json", "", "[1]", '{"hook_event_name": "Stop"}'):
            r = subprocess.run(real, shell=True, input=stdin, text=True,
                               capture_output=True, env={**os.environ, "HOME": str(tmp_path)})
            assert (r.returncode, r.stdout, r.stderr) == (0, "", "")
        # a script that crashes outright must not block either
        bad = tmp_path / "crash.py"
        bad.write_text("import sys; sys.exit(2)\n")
        r = subprocess.run(cmd.replace(INSTALLED, str(bad)), shell=True, text=True,
                           input="{}", capture_output=True)
        assert r.returncode == 0 and r.stdout == ""


# --- H2: only the nearest claude process decides -----------------------------

OUTER = ["claude", "-p", "--fork-session", "--no-session-persistence", "--resume", PARENT]


def test_normal_claude_nested_under_a_side_chat_run_is_not_captured(tmp_path):
    nested = fake_proc({
        60: (50, ["/bin/sh", "-c", "hook"]),
        50: (40, ["/usr/bin/claude", "--model", "opus"]),   # the session's own claude
        40: (1, OUTER),                                      # an outer fork run above it
    })
    assert hook.side_chat_launch(nested, pid=60) == (False, None)
    assert not run(tmp_path, event(tmp_path), read=nested, ppid=60)
    assert not (tmp_path / ".claude").exists()


def test_nearest_claude_with_flags_still_wins_over_a_plain_one_above(tmp_path):
    chain2 = fake_proc({
        60: (50, ["/bin/sh", "-c", "hook"]),
        50: (40, ["/opt/claude/bin/claude", *OUTER[1:]]),
        40: (1, ["claude", "--model", "opus"]),
    })
    assert hook.side_chat_launch(chain2, pid=60) == (True, PARENT)


def test_node_run_claude_is_recognised():
    r = fake_proc({60: (50, ["sh"]), 50: (1, ["node", "/x/bin/claude", *OUTER[1:]])})
    assert hook.side_chat_launch(r, pid=60) == (True, PARENT)


def test_short_resume_flag_gives_the_parent():
    r = fake_proc({50: (1, ["claude", "--fork-session", "--no-session-persistence",
                            "-r", PARENT])})
    assert hook.side_chat_launch(r, pid=50) == (True, PARENT)


def test_paren_in_comm_name_does_not_break_the_ppid_parse():
    def read(path):
        pid, name = path.split("/")[2:]
        if name == "cmdline":
            return b"\0".join(a.encode() for a in (
                ["sh"] if pid == "60" else OUTER)) + b"\0"
        return b"60 (evil) S 1 (x) y) S 50 0 0" if pid == "60" else b"1 (claude) S 1 0"
    assert hook.side_chat_launch(read, pid=60) == (True, PARENT)


def test_self_parent_cycle_ends_and_is_not_a_side_chat():
    loop = fake_proc({50: (50, ["sh", "-c", "x"])})
    assert hook.side_chat_launch(loop, pid=50) == (False, None)
    two = fake_proc({50: (40, ["sh"]), 40: (50, ["sh"])})
    assert hook.side_chat_launch(two, pid=50) == (False, None)


def test_stat_failing_after_cmdline_succeeded_is_not_a_side_chat():
    def read(path):
        if path.endswith("/cmdline"):
            return b"sh\0-c\0hook\0"
        raise OSError("process vanished")
    assert hook.side_chat_launch(read, pid=50) == (False, None)


@pytest.mark.parametrize("raw", [b"", b"\0", b"\0\0"])
def test_empty_cmdline_is_not_a_side_chat(raw):
    def read(path):
        if path.endswith("/cmdline"):
            return raw
        return b"50 (x) S 40 0"
    assert hook.side_chat_launch(read, pid=50) == (False, None)


# --- H3: entrypoint gate and opt-out -----------------------------------------

def test_headless_sdk_entrypoint_captures_nothing_even_with_both_flags(tmp_path):
    assert not run(tmp_path, event(tmp_path), env={"CLAUDE_CODE_ENTRYPOINT": "sdk-cli"})
    assert not run(tmp_path, event(tmp_path), env={})
    assert not (tmp_path / ".claude").exists()


def test_desktop_entrypoint_captures(tmp_path):
    assert run(tmp_path, event(tmp_path), env={"CLAUDE_CODE_ENTRYPOINT": "claude-desktop"})


def test_entrypoint_override_for_the_live_test(tmp_path):
    env = {"CLAUDE_CODE_ENTRYPOINT": "sdk-cli",
           "AGENT_HISTORY_SIDE_CHAT_ENTRYPOINTS": "claude-desktop, sdk-cli"}
    assert run(tmp_path, event(tmp_path), env=env)
    # the override replaces the default rather than adding to it
    env = {"CLAUDE_CODE_ENTRYPOINT": "claude-desktop",
           "AGENT_HISTORY_SIDE_CHAT_ENTRYPOINTS": "something-else"}
    assert not run(tmp_path, event(tmp_path), env=env)


@pytest.mark.parametrize("value", ["off", "OFF", "0", "false"])
def test_opt_out_env_stops_capture(tmp_path, value):
    env = {"CLAUDE_CODE_ENTRYPOINT": "claude-desktop", "AGENT_HISTORY_SIDE_CHATS": value}
    assert not run(tmp_path, event(tmp_path), env=env)
    assert not (tmp_path / ".claude").exists()


def test_opt_out_marker_file_stops_capture_and_is_not_created_by_the_check(tmp_path):
    d = tmp_path / ".claude/side-chats"
    d.mkdir(parents=True)
    (d / ".disabled").write_text("")
    assert not run(tmp_path, event(tmp_path))
    assert not (d / f"{SID}.jsonl").exists()
    (d / ".disabled").unlink()
    assert run(tmp_path, event(tmp_path))


def test_checking_the_gate_does_not_create_the_directory(tmp_path):
    assert hook.enabled({"CLAUDE_CODE_ENTRYPOINT": "claude-desktop"}, str(tmp_path))
    assert not (tmp_path / ".claude").exists()


# --- H5: file safety ----------------------------------------------------------

posix = pytest.mark.skipif(sys.platform == "win32", reason="POSIX files only")


@posix
def test_symlinked_capture_file_is_not_followed(tmp_path):
    d = tmp_path / ".claude/side-chats"
    d.mkdir(parents=True)
    victim = tmp_path / "victim.txt"
    victim.write_text("keep")
    (d / f"{SID}.jsonl").symlink_to(victim)
    assert not run(tmp_path, event(tmp_path))
    assert victim.read_text() == "keep"


@posix
def test_symlinked_directory_is_not_followed(tmp_path):
    (tmp_path / ".claude").mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (tmp_path / ".claude/side-chats").symlink_to(elsewhere)
    assert not run(tmp_path, event(tmp_path))
    assert list(elsewhere.iterdir()) == []


@posix
def test_hardlinked_capture_file_is_refused(tmp_path):
    d = tmp_path / ".claude/side-chats"
    d.mkdir(parents=True)
    target = tmp_path / "other.txt"
    target.write_text("keep")
    os.link(target, d / f"{SID}.jsonl")
    assert not run(tmp_path, event(tmp_path))
    assert target.read_text() == "keep"


@posix
def test_loose_modes_are_tightened(tmp_path):
    d = tmp_path / ".claude/side-chats"
    d.mkdir(parents=True)
    d.chmod(0o755)
    f = d / f"{SID}.jsonl"
    f.write_text("")
    f.chmod(0o644)
    assert run(tmp_path, event(tmp_path))
    assert oct(d.stat().st_mode & 0o777) == "0o700"
    assert oct(f.stat().st_mode & 0o777) == "0o600"


def test_lone_surrogate_in_text_does_not_drop_the_turn(tmp_path):
    assert run(tmp_path, event(tmp_path, prompt="before \ud800 after", hook_event_name="UserPromptSubmit"))
    assert lines(tmp_path)[0]["text"].startswith("before ")
    assert lines(tmp_path)[0]["text"].endswith(" after")


def test_a_turn_is_written_with_a_single_write(tmp_path, monkeypatch):
    calls = []
    real = os.write
    monkeypatch.setattr(os, "write", lambda fd, b: (calls.append(b), real(fd, b))[1])
    assert run(tmp_path, event(tmp_path))
    assert len(calls) == 1 and calls[0].endswith(b"\n")


# --- H6: parent id shape -------------------------------------------------------

@pytest.mark.parametrize("bad", ["abcdefgh", "ABCDEF12-1111-4222-8333-444455556666",
                                 PARENT[:-1], PARENT + "0", "../../etc/passwd",
                                 "e18e5130-5211-4d3c-8162-addd565dd98g"])
def test_parent_must_be_a_lowercase_uuid(bad):
    for args in (["--resume", bad], [f"--resume={bad}"], ["-r", bad]):
        assert hook.side_chat_launch(chain(args), pid=50) == (True, None)


# --- H4: a real /proc, no fakes -------------------------------------------------

needs_proc = pytest.mark.skipif(
    not (sys.platform.startswith("linux") and os.path.isdir("/proc/self")
         and __import__("shutil").which("bash")),
    reason="needs Linux /proc and bash")

CHILD = (
    "import json,os,subprocess,sys\n"
    "r=subprocess.run([sys.executable,'-I',sys.argv[1]],input=os.environ['EVT'],"
    "text=True,capture_output=True)\n"
    "sys.exit(r.returncode)\n")


def _run_under(tmp_path, claude_args, env_extra=None):
    """Run the hook as a child of a process whose argv looks like claude's."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(
        ("CLAUDE", "AGENT_HISTORY"))}
    env.update(HOME=str(tmp_path), CLAUDE_CODE_ENTRYPOINT="claude-desktop",
               EVT=json.dumps(event(tmp_path)), **(env_extra or {}))
    # `exec -a claude` makes argv[0] "claude", as the real launch has.
    return subprocess.run(
        ["bash", "-c", 'exec -a claude "$0" -c "$1" "$2" "${@:3}"',
         sys.executable, CHILD, str(HOOK), *claude_args],
        env=env, capture_output=True, text=True)


@needs_proc
def test_real_proc_side_chat_launch_is_captured_with_its_parent(tmp_path):
    r = _run_under(tmp_path, ["--fork-session", "--no-session-persistence",
                              f"--resume={PARENT}"])
    assert r.returncode == 0, r.stderr
    assert lines(tmp_path)[0]["parent_session_id"] == PARENT


@needs_proc
def test_real_proc_plain_claude_is_not_captured_wherever_pytest_runs(tmp_path):
    """Robust when pytest itself runs under a side-chat claude: the nearest
    claude (this child, with no flags) decides, not anything above it."""
    r = _run_under(tmp_path, ["--model", "opus"])
    assert r.returncode == 0, r.stderr
    assert not (tmp_path / ".claude").exists()
