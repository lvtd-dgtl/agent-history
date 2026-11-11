# Side-chat capture

## Why
The Claude desktop app runs "side chats" as a CLI process:
`-p --input-format stream-json --output-format stream-json --resume=<parent>
--fork-session --no-session-persistence --tools=Read,Grep,Glob
--setting-sources= --strict-mcp-config`. `--no-session-persistence` means no
transcript is written, so nothing is searchable.

## Mechanism
1. A managed-settings drop-in installs UserPromptSubmit and Stop hooks running
   `hooks/side_chat_capture.py`.
2. The hook acts only when all of these hold: (a) the NEAREST claude process
   above it has both `--no-session-persistence` and `--fork-session` in its
   argv (the side-chat launch signature); the walk stops at the first claude
   so a normal session nested under an outer fork run is not captured;
   (b) `CLAUDE_CODE_ENTRYPOINT` is in an allowlist, default `claude-desktop`
   (an unverified guess; override with `AGENT_HISTORY_SIDE_CHAT_ENTRYPOINTS`,
   comma separated; headless fleet-1 runs report `sdk-cli` and never match);
   (c) not opted out: `AGENT_HISTORY_SIDE_CHATS=off` or the marker file
   `~/.claude/side-chats/.disabled`; and (d) `transcript_path` is non-empty
   with no file behind it. A
   missing file alone is not enough: a new normal session can fire its first
   prompt before its transcript exists. It appends one line
   `{v, session_id, parent_session_id, cwd, timestamp, role, text, entrypoint}`
   to `~/.claude/side-chats/<session_id>.jsonl`. User text is the hook's
   `prompt`; assistant text is Stop's `last_assistant_message`.
3. The parent comes from the process tree: the hook's parent is `/bin/sh -c`,
   the claude process is above it (up to 4 levels are walked via
   `/proc/<pid>/stat`, stopping at the nearest claude), and its cmdline
   carries `--fork-session` and `--resume <id>`, `--resume=<id>` or `-r <id>`.
   The parent must be a lowercase uuid, else the chat is captured unlinked.
4. `adapters/side_chat.py` (agent `claude-code-side`, dirs from
   `config.side_chat_dirs()`, env `AGENT_HISTORY_SIDE_CHAT_DIRS`) turns lines
   into Records with `fork_of` = parent id. A line is accepted only if its
   `session_id` equals its file name; only regular, non-symlink files owned by
   the indexing user are read, with bounded line and field sizes. Text goes
   through the same `scrub` and MIN_CHARS as every adapter; the hook stores
   text verbatim. Redaction is not automatic: it runs at index time only with
   `AGENT_HISTORY_REDACT=on`, strict mode or hub mode, so by default the local
   index and the capture files hold plaintext (shipping redacts). Ship and the hub inbox reuse existing Record fields
   and accept any agent name, so nothing new is needed there.
5. Linking: the indexer derives the parent uid from the child's agent. A side
   chat's parent is a `claude-code` session, so `sessions.fork_parent_agent`
   maps `claude-code-side` to `claude-code`; the link works on a hub too since
   the agent name travels on the wire.

## Evidence (spike, 2026-11-09, claude 2.1.318 on fleet-1)
Both hooks fire under `--no-session-persistence` and also with
`--setting-sources=` when hooks come from `--settings`; Stop carries
`last_assistant_message`; `transcript_path` is given but the file does not
exist; with `--resume --fork-session` the hook session id differs from the
parent and the claude process is the grandparent; env has
CLAUDE_CODE_SESSION_ID and CLAUDE_PROJECT_DIR; a failing hook (exit 1) does
not block the chat. Raw notes: `~/fleet/experiments/side-chat-hook-2026-11-09/FINDINGS.md`.

## Limits and unknowns
- Linux and WSL only. The hook finds the side-chat launch by reading
  `/proc/<pid>/cmdline` and `/proc/<pid>/stat`. On native Windows or macOS
  there is no `/proc`, so it silently captures nothing (no error, no file).
  The live test on the MSI decides whether the desktop app's `claude` process
  runs inside WSL (then it works) or natively on Windows (then it does not,
  and detection would need a different method). The detection logic is
  unchanged; the hook's tests use a fake `/proc` and run on any OS.
- Not tested with the real desktop launch. `CLAUDE_CODE_ENTRYPOINT` is
  recorded in each line, but the gate depends on it: `claude-desktop` is a
  guess. If the MSI test leaves no file, check `CLAUDE_CODE_ENTRYPOINT` first
  (or set `AGENT_HISTORY_SIDE_CHAT_ENTRYPOINTS` to candidates).
- Decision (Dennis, 2026-11-11): narrow capture plus an opt-out; it still
  ships to the hub.
- The managed-settings command ends in `; exit 0` and is guarded by `[ -r
  script ]`: exit 2 from a hook blocks the prompt, so a missing script must not
  be able to produce it.
- Retention: captured files are never deleted. Removal is `rm -r
  ~/.claude/side-chats` per user; `agent-history purge` deletes indexed
  chunks only (not session rows, not capture files); hub copies need a
  hub-side purge.
- Whether a managed-settings drop-in loads under `--setting-sources=` is
  unverified until installed (managed settings are normally not governed by
  that flag; the spike only proved `--settings` hooks).
- Only text is captured: no tool calls, and only the final Stop message of a
  turn.
- Parent id depends on the cmdline carrying `--resume`; otherwise the side
  chat is indexed unlinked.
- The hook runs for every Claude Code session on the machine (no-op when a
  transcript exists), costing one python start per prompt and Stop.
- Captured files are plain text, 0600, in each user's home. Secrets are
  redacted only when `AGENT_HISTORY_REDACT=on`, strict or hub mode applies at
  index time, and on ship; the default local index is plaintext.
