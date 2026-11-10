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
2. The hook acts only when the claude ancestor's argv has both
   `--no-session-persistence` and `--fork-session` (the side-chat launch
   signature) and `transcript_path` is non-empty with no file behind it. A
   missing file alone is not enough: a new normal session can fire its first
   prompt before its transcript exists. It appends one line
   `{v, session_id, parent_session_id, cwd, timestamp, role, text, entrypoint}`
   to `~/.claude/side-chats/<session_id>.jsonl`. User text is the hook's
   `prompt`; assistant text is Stop's `last_assistant_message`.
3. The parent comes from the process tree: the hook's parent is `/bin/sh -c`,
   the claude process is above it (up to 4 levels are walked via
   `/proc/<pid>/stat`), and its cmdline carries `--fork-session` and
   `--resume <id>` or `--resume=<id>`.
4. `adapters/side_chat.py` (agent `claude-code-side`, dirs from
   `config.side_chat_dirs()`, env `AGENT_HISTORY_SIDE_CHAT_DIRS`) turns lines
   into Records with `fork_of` = parent id. Text goes through the same
   `scrub`, MIN_CHARS and indexer-time redaction as every adapter; the hook
   stores text verbatim. Ship and the hub inbox reuse existing Record fields
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
- Not tested with the real desktop launch; `CLAUDE_CODE_ENTRYPOINT` is
  recorded so the value can be checked (`claude-desktop` expected).
- Whether a managed-settings drop-in loads under `--setting-sources=` is
  unverified until installed (managed settings are normally not governed by
  that flag; the spike only proved `--settings` hooks).
- Only text is captured: no tool calls, and only the final Stop message of a
  turn.
- Parent id depends on the cmdline carrying `--resume`; otherwise the side
  chat is indexed unlinked.
- The hook runs for every Claude Code session on the machine (no-op when a
  transcript exists), costing one python start per prompt and Stop.
- Captured files are plain text, 0600, in each user's home; secrets are
  redacted only at index time.
