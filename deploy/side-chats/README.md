# Side-chat capture: install

Claude desktop-app side chats write no transcript, so `hooks/side_chat_capture.py`
records them from a hook and the `claude-code-side` adapter indexes the result.
Design and limits: `docs/design/side-chat-capture.md`.

## Platform support

Linux and WSL only. The hook finds the side-chat launch by reading
`/proc/<pid>/cmdline` and `/proc/<pid>/stat`; native Windows and macOS have no
`/proc`, so there it silently captures nothing (no error, no file). Whether
the Claude desktop app's `claude` process on the MSI runs inside WSL is decided
by the live test there: if it does, capture works; if it runs natively on
Windows, it will not.

## What gets captured, and when

The hook captures a turn only if ALL of these hold:

1. The nearest `claude` process above the hook was launched with both
   `--no-session-persistence` and `--fork-session`. A normal claude nested
   under an outer fork run is not a side chat.
2. The session has no transcript file.
3. The environment variable `CLAUDE_CODE_ENTRYPOINT` is in the allowlist,
   default `claude-desktop`. **This value is an unverified guess**; the live
   test on the MSI confirms it. Headless runs (`claude -p`, the Agent SDK) show
   `sdk-cli` on fleet-1 and never match. To try other values, set
   `AGENT_HISTORY_SIDE_CHAT_ENTRYPOINTS` (comma separated; it replaces the
   default) in the environment Claude Code runs in.
4. Not opted out: `AGENT_HISTORY_SIDE_CHATS=off` (also `0`, `false`, `no`) in
   the environment, or the marker file `~/.claude/side-chats/.disabled` exists
   (`mkdir -p ~/.claude/side-chats && touch ~/.claude/side-chats/.disabled`).
   The hook never creates the directory just to check.

## What gets installed

- `/usr/local/lib/agent-history/side_chat_capture.py` (the hook, stdlib only)
- `/etc/claude-code/managed-settings.d/agent-history-side-chats.json`
  (UserPromptSubmit and Stop hooks, timeout 5 s)

The drop-in command is
`[ -r <script> ] && python3 -I <script> >/dev/null 2>&1; exit 0`. The `exit 0`
matters: Claude Code treats a hook exit code 2 as BLOCKING, and a missing
script would make `python3 -I` exit 2 and reject every prompt on the machine.
With this command a missing script, a missing `python3` or a crash all end in
exit 0. It relies on `python3` being on the PATH of the shell Claude Code uses;
if it is not, capture silently does nothing. The script itself catches every
error and returns 0 as well, but that does not cover those cases, which is why
the command carries its own guard.

Managed settings apply to every Claude Code session of every user on the
machine. The hook does nothing unless all four conditions above hold, so a
normal session costs one `python3` start per prompt and per Stop. Captures go
to each user's own `~/.claude/side-chats/` (dir 0700, files 0600; the hook
refuses a symlinked directory or file, a hard-linked file, or anything not
owned by the user).

## Install (root)

Check first whether other managed policy already exists:

    ls /etc/claude-code/

### On the MSI (WSL Ubuntu, no checkout of this worktree)

Fetch the two files from GitHub at a pinned commit. `SHA` is the full 40-char
merged or reviewed commit on lvtd-dgtl/agent-history; it must be pushed first.

    set -e
    SHA=<40-char commit sha>
    d=$(mktemp -d)
    git clone --no-checkout https://github.com/lvtd-dgtl/agent-history.git "$d/repo"
    git -C "$d/repo" show "$SHA:hooks/side_chat_capture.py" > "$d/side_chat_capture.py"
    git -C "$d/repo" show "$SHA:deploy/managed-settings.d/agent-history-side-chats.json" > "$d/agent-history-side-chats.json"
    test -s "$d/side_chat_capture.py"
    test -s "$d/agent-history-side-chats.json"
    python3 -m json.tool "$d/agent-history-side-chats.json" >/dev/null
    python3 -m py_compile "$d/side_chat_capture.py"
    sudo install -d -o root -g root -m 0755 /usr/local/lib/agent-history /etc/claude-code/managed-settings.d
    sudo install -m 0644 -o root -g root "$d/side_chat_capture.py" /usr/local/lib/agent-history/side_chat_capture.py
    sudo install -m 0644 -o root -g root "$d/agent-history-side-chats.json" /etc/claude-code/managed-settings.d/agent-history-side-chats.json

Install the script before the drop-in (as above), so the drop-in never points at
a missing file. (If it ever does, the command still exits 0.) With `gh` signed
in, this also fetches a file:

    gh api "repos/lvtd-dgtl/agent-history/contents/hooks/side_chat_capture.py?ref=$SHA" -H 'Accept: application/vnd.github.raw'

Capture works with just those files. To index the captured files the MSI's
agent-history needs a build containing the `claude-code-side` adapter. After
the PR merges (the README's install method is `uv tool install`):

    uv tool install --force "agent-history @ git+https://github.com/lvtd-dgtl/agent-history@<merged ref>"

### On a machine with this checkout

    set -e
    python3 -m json.tool deploy/managed-settings.d/agent-history-side-chats.json >/dev/null
    test -s hooks/side_chat_capture.py
    sudo install -d -o root -g root -m 0755 /usr/local/lib/agent-history /etc/claude-code/managed-settings.d
    sudo install -m 0644 -o root -g root hooks/side_chat_capture.py /usr/local/lib/agent-history/side_chat_capture.py
    sudo install -m 0644 -o root -g root deploy/managed-settings.d/agent-history-side-chats.json /etc/claude-code/managed-settings.d/agent-history-side-chats.json

The drop-in uses the same `hooks` shape as `settings.json`. Claude Code merges
`managed-settings.json`, then each `*.json` in `managed-settings.d/` in
alphabetical order (https://code.claude.com/docs/en/managed-settings.md,
"managed-settings.d"). If an MDM or server-managed policy sets policy keys it
can take precedence over the file source; check `claude doctor`.

## Redaction: turn it on where the indexer runs

Secret redaction is NOT on by default locally. It runs only with
`AGENT_HISTORY_REDACT=on`, strict mode, or on a hub. Without it, the capture
files AND the local index hold side-chat text in plaintext (a pasted key
included); `agent-history ship` redacts what it sends, and a hub always
redacts. So set it in the environment of whatever runs `agent-history index`
(a shell profile, a systemd unit `Environment=`, or the scheduled task):

    export AGENT_HISTORY_REDACT=on

The capture files themselves are never redacted: the hook stores text verbatim.

## Verify

1. Start Claude Code once and run `claude doctor`: no managed settings error.
2. Open a side chat in the desktop app, ask something, wait for the reply.
3. `ls -l ~/.claude/side-chats/` shows `<session-id>.jsonl` (0600); lines have
   role user and assistant, `parent_session_id`, and `entrypoint`.
   **If nothing appears, check `CLAUDE_CODE_ENTRYPOINT` first:** the default
   allowlist (`claude-desktop`) is a guess. Find the real value (a side chat's
   `/proc/<pid>/environ`, or temporarily set `AGENT_HISTORY_SIDE_CHAT_ENTRYPOINTS`
   to a list of candidates) and update the allowlist. Only then suspect the
   managed drop-in not loading under the side chat's `--setting-sources=`
   (unverified before install), and report that.
4. Normal sessions must NOT create files there (start one and check).
5. `agent-history index` (incremental; `agent-history ship` then uploads to the
   hub), then `agent-history search --agent claude-code-side "<words from the side chat>"`.

## Retention

Nothing deletes captured files: they stay in `~/.claude/side-chats/` until you
remove them, and there is no expiry.

## Rollback

1. Stop capture first: remove ONLY the drop-in. Leave the script in place until
   running Claude Code sessions have restarted (a session that already loaded
   the hooks keeps running the command; the command's `[ -r ... ]` guard means
   a missing script is harmless, but there is no reason to rely on it).

       sudo rm /etc/claude-code/managed-settings.d/agent-history-side-chats.json

   New sessions stop capturing; to stop it right now for one user, create
   `~/.claude/side-chats/.disabled`.
2. Once sessions have restarted, remove the script:

       sudo rm /usr/local/lib/agent-history/side_chat_capture.py

3. Remove already captured text. This is per user, for every user on the
   machine that used side chats:

       rm -r ~/.claude/side-chats

   `agent-history purge` removes indexed chunks only (their text, vectors and
   full-text rows): `agent-history purge --agent claude-code-side` previews and
   `--yes` deletes. It does not touch the capture files above, and session
   rows (ids, cwd, fork links) are not chunks, so they stay. If the index was
   shipped to a hub, the hub holds its own copy: it needs a purge on the hub
   side too.
