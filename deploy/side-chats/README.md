# Side-chat capture: install

Claude desktop-app side chats write no transcript, so `hooks/side_chat_capture.py`
records them from a hook and the `claude-code-side` adapter indexes the result.
Design and limits: `docs/design/side-chat-capture.md`.

## What gets installed

- `/usr/local/lib/agent-history/side_chat_capture.py` (the hook, stdlib only)
- `/etc/claude-code/managed-settings.d/agent-history-side-chats.json`
  (UserPromptSubmit and Stop hooks, timeout 5 s)

Managed settings apply to every Claude Code session of every user on the
machine. The hook is a no-op whenever the session has a transcript file (all
normal sessions), exits 0 on any error, and does not block a chat. Captures go
to each user's own `~/.claude/side-chats/` (dir 0700, files 0600).

## Install (root)

Check first whether other managed policy already exists:

    ls /etc/claude-code/

The hook acts only when the claude process was launched as a side chat (argv
has both `--no-session-persistence` and `--fork-session`) and the session has no
transcript file, so a brand-new normal session's first prompt is never captured.

### On the MSI (WSL Ubuntu, no checkout of this worktree)

Fetch the two files from GitHub at a pinned commit (`<SHA>` = the merged or
reviewed commit on lvtd-dgtl/agent-history; it must be pushed first):

    git clone --no-checkout https://github.com/lvtd-dgtl/agent-history.git /tmp/ah-side && cd /tmp/ah-side
    git show <SHA>:hooks/side_chat_capture.py > /tmp/side_chat_capture.py
    git show <SHA>:deploy/managed-settings.d/agent-history-side-chats.json > /tmp/agent-history-side-chats.json
    sudo install -d -m 0755 /usr/local/lib/agent-history /etc/claude-code/managed-settings.d
    sudo install -m 0644 -o root -g root /tmp/side_chat_capture.py /usr/local/lib/agent-history/side_chat_capture.py
    sudo install -m 0644 -o root -g root /tmp/agent-history-side-chats.json /etc/claude-code/managed-settings.d/agent-history-side-chats.json

(`gh api repos/lvtd-dgtl/agent-history/contents/hooks/side_chat_capture.py?ref=<SHA> -H 'Accept: application/vnd.github.raw'` works too if `gh` is signed in.)

Capture works with just those files. To index the captured files the MSI's
agent-history needs a build containing the `claude-code-side` adapter. After
the PR merges (the README's install method is `uv tool install`):

    uv tool install --force "agent-history @ git+https://github.com/lvtd-dgtl/agent-history@<merged ref>"

### On a machine with this checkout

    sudo install -d -m 0755 /usr/local/lib/agent-history /etc/claude-code/managed-settings.d
    sudo install -m 0644 -o root -g root hooks/side_chat_capture.py /usr/local/lib/agent-history/side_chat_capture.py
    sudo install -m 0644 -o root -g root deploy/managed-settings.d/agent-history-side-chats.json /etc/claude-code/managed-settings.d/agent-history-side-chats.json

The drop-in uses the same `hooks` shape as `settings.json`. Claude Code merges
`managed-settings.json`, then each `*.json` in `managed-settings.d/` in
alphabetical order (https://code.claude.com/docs/en/managed-settings.md,
"managed-settings.d"). If an MDM or server-managed policy sets policy keys it
can take precedence over the file source; check `claude doctor`.

## Verify

1. Start Claude Code once and run `claude doctor`: no managed settings error.
2. Open a side chat in the desktop app, ask something, wait for the reply.
3. `ls -l ~/.claude/side-chats/` shows `<session-id>.jsonl` (0600); lines have
   role user and assistant, `parent_session_id`, and `entrypoint`.
4. Normal sessions must NOT create files there (start one and check).
5. `agent-history index` (incremental; `agent-history ship` then uploads to the
   hub), then `agent-history search --agent claude-code-side "<words from the side chat>"`.

If nothing appears: the managed drop-in may not load under the side chat's
`--setting-sources=` (unverified before install). Report that.

## Rollback

    sudo rm /etc/claude-code/managed-settings.d/agent-history-side-chats.json /usr/local/lib/agent-history/side_chat_capture.py

New sessions stop capturing immediately. Already captured files stay in
`~/.claude/side-chats/` and indexed chunks stay in the index (`agent-history purge`
to remove them).
