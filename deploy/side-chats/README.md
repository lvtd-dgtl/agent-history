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

## Install (root; run from the agent-history checkout)

    sudo install -d -m 0755 /usr/local/lib/agent-history
    sudo install -m 0644 -o root -g root hooks/side_chat_capture.py /usr/local/lib/agent-history/side_chat_capture.py
    sudo install -d -m 0755 /etc/claude-code/managed-settings.d
    sudo install -m 0644 -o root -g root deploy/managed-settings.d/agent-history-side-chats.json /etc/claude-code/managed-settings.d/agent-history-side-chats.json

The drop-in uses the same `hooks` shape as `settings.json`. Claude Code merges
`managed-settings.json`, then each `*.json` in `managed-settings.d/` in
alphabetical order (https://code.claude.com/docs/en/managed-settings.md,
"managed-settings.d"). If an MDM or server-managed policy sets policy keys it
can take precedence over the file source; check `/status` or `claude doctor`.

## Verify

1. Start Claude Code once (any session) and run `claude doctor`: no managed settings error.
2. Open a side chat in the desktop app, ask something, wait for the reply.
3. `ls -l ~/.claude/side-chats/` shows `<session-id>.jsonl` (0600); each line has
   role user and assistant, `parent_session_id`, and `entrypoint` (expect `claude-desktop`).
4. Normal sessions must NOT create files there.
5. `agent-history index`, then `agent-history find "<words from the side chat>" --agent claude-code-side`.

If nothing appears: the managed drop-in may not load under the side chat's
`--setting-sources=` (unverified before install). Report that; the design
notes list the fallback.

## Rollback

    sudo rm /etc/claude-code/managed-settings.d/agent-history-side-chats.json /usr/local/lib/agent-history/side_chat_capture.py

New sessions stop capturing immediately. Already captured files stay in
`~/.claude/side-chats/` and indexed chunks stay in the index (`agent-history purge`
to remove them).
