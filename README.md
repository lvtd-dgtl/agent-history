# agent-history

[![CI](https://github.com/d3layd/agent-history/actions/workflows/ci.yml/badge.svg)](https://github.com/d3layd/agent-history/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

Local, private, zero-cost semantic memory for your AI coding agents — search
everything you and your agents have ever discussed, across every agent on the
machine, by meaning.

You had a long conversation three weeks ago where you worked out why the auth
flow had to be structured a particular way. You remember deciding it. You do
not remember which project, which day, or what the reasoning was. `grep` is no
help, because you don't remember the words you used.

```console
$ agent-history search "why did we split the auth middleware"
#1  score=0.0325  2026-06-14T22:03  claude-code/assistant  ~/repos/example
    file: /home/you/.claude/projects/-home-you-repos-example/a1b2c3.jsonl
    The reason to split it is that the session refresh needs to run before...
```

Everything runs on your machine. Embeddings come from your own Ollama
instance; search runs inside SQLite. No API keys, no per-query cost, and
nothing leaves the box — the tool refuses a non-loopback endpoint unless you
explicitly allow it.

## What it does

**One index across every agent on the machine.** Each source has an adapter
that knows where that agent keeps its transcripts and how to read them:

| source | reads | format |
|---|---|---|
| **Claude Code** | every `~/.claude*/projects/**` profile (and `$CLAUDE_CONFIG_DIR`) + project memory `*.md`, including the WSL Windows side | JSONL |
| **OpenAI Codex CLI** | `~/.codex/sessions/**` rollouts, including the WSL Windows side | JSONL |
| **OpenCode** | `~/.local/share/opencode/opencode.db` | SQLite |
| **Hermes** | `~/.hermes*/state.db`, one per profile | SQLite |
| **OpenClaw** | `~/.openclaw/agents/*/sessions/` + workspace markdown | JSONL |
| **docs** (opt-in) | any documentation trees you name — runbooks, ops notes, session summaries | `*.md` / `*.txt` |
| **claude.ai** (import) | a claude.ai data export you download — chats, attachments, projects, memories | JSON |

Everything found is detected automatically (docs and imports excepted — see
[docs](#indexing-documentation-trees) and
[backfilling claude.ai](#backfilling-claudeai-chats-from-a-data-export)),
merged into one index, searchable together or filtered with `--agent`.

**Hybrid search.** Vector similarity finds meaning; BM25 full-text finds the
exact identifiers embeddings are bad at (`os error 11001`, a function name);
the rankings are fused. `--mode text` needs no Ollama at all, and hybrid
degrades to it gracefully when the embedder is down.

**What was true when.** Search takes a time window (`--since`, `--until`,
`--as-of`) and an opt-in recency half-life; curated **facts** keep the
current value of a thing with what it replaced; and a hit that a newer hit
appears to replace — a server moved, a version bumped — is flagged
*possibly superseded*. All without a model:
[details below](#what-was-true-when-the-time-axis).

**The conversation, not just the hit.** `show` prints the conversation a
search hit belongs to, and `serve` is a local web viewer over the same search
— both read from the index, which matters because the source transcript
[often no longer exists](#your-index-outlives-the-transcripts).

**Context injection (on by default).** Relevant past-session excerpts ride
into your new prompts automatically — the "agent already knows your project"
experience, with no LLM anywhere in the loop. You see a `📚 N memories
recalled` line each time; toggle with `/history:inject`:
[details below](#context-injection-on-by-default).

**An archive that agents don't keep.** Claude Code deletes transcripts after
30 days by default. The index doesn't. On the machine this was built for, 67%
of the index came from conversations whose files no longer exist.

## Quickstart

```console
$ uv tool install git+https://github.com/d3layd/agent-history
$ agent-history index          # first run embeds everything; minutes, once
$ agent-history "that thing we decided about caching"
```

The first index of a large history takes a while — it embeds every message
once. Every run after is incremental and takes seconds.

Or clone and run the installer, which also checks Ollama, pulls the model if
missing, and offers to wire up automatic indexing:

```console
$ git clone https://github.com/d3layd/agent-history
$ cd agent-history && ./scripts/install.sh
```

### As a Claude Code plugin

```
/plugin marketplace add d3layd/agent-history
/plugin install history@agent-history
```

This adds **`/history:search`** (runs the search and *synthesizes an answer*
rather than making you read raw JSONL) and **`/history:inject`** (toggle
context injection), plus three hooks: SessionEnd and PreCompact keep the
index current automatically, and UserPromptSubmit powers
[context injection](#context-injection-on-by-default), which is on by
default.

The plugin is named `history` rather than `agent-history` purely for
ergonomics: Claude Code namespaces every plugin component as
`<plugin>:<component>` with no way to opt out, so a long plugin name makes for
a long command. The repository and the CLI keep the full `agent-history` name.

## Requirements

- **Python 3.10+**, with a `sqlite3` module that permits extension loading
  (see below)
- **[Ollama](https://ollama.com) reachable over HTTP**, with an embedding
  model pulled — needed for indexing and for vector/hybrid search.
  `--mode text` (BM25 full-text) works without it.
- **[`uv`](https://docs.astral.sh/uv/)** to install

```console
$ ollama pull nomic-embed-text
```

The Python side is deliberately small — two direct dependencies, six once
resolved:

| | |
|---|---|
| direct | `sqlite-vec`, `requests` |
| transitive | `certifi`, `charset-normalizer`, `idna`, `urllib3` |

No vector database, no ML framework, no ORM. The index is a single SQLite
file.

**Two things that are easy to get wrong:**

*Extension loading.* Vectors live in the
[`sqlite-vec`](https://github.com/asg017/sqlite-vec) extension, so the
`sqlite3` module must allow loading it. Check with:

```console
$ python3 -c "import sqlite3; sqlite3.connect(':memory:').enable_load_extension(True)"
```

If that raises `AttributeError`, that Python was built without extension
support and the tool cannot work under it. `agent-history doctor` reports this
explicitly rather than failing obscurely.

**Known: this affects macOS.** CI shows uv-managed CPython on macOS lacks
extension support on **Python 3.11, 3.12 and 3.13**, while **3.10 works**. It
is a property of the Python build, not of macOS itself — Homebrew's `python3`
generally works. On macOS either install under 3.10:

```console
$ uv tool install --python 3.10 git+https://github.com/d3layd/agent-history
```

or use a Python where the check above succeeds. The same limitation appears
in some Conda and Linux distribution builds.

*The Ollama CLI is not required.* Only the HTTP endpoint is. Ollama can run on
another OS entirely — the setup this was built on has Ollama native on Windows
with the tool running in WSL, reaching it over forwarded localhost, and no
`ollama` binary present at all. `install.sh` will offer to `ollama pull` the
model when the CLI exists, and tell you to pull it yourself when it doesn't.

**No shell is required.** Automatic indexing lives in the CLI itself
(`agent-history trigger`), so detaching, locking and change detection behave
identically on Linux, macOS and Windows. The scripts under `scripts/` are
conveniences for installation and scheduling — `.sh` for Unix, `.ps1` for
Windows — and the Unix ones avoid bash 4 builtins so macOS's bash 3.2 works.

## Usage

```console
$ agent-history search "query"              # top 8 hybrid matches
$ agent-history search "query" -k 20        # more results
$ agent-history search "query" --full       # full chunk text, not a preview
$ agent-history search "query" --cwd myapp  # only sessions from a directory
$ agent-history search "query" --agent codex   # one source only
$ agent-history search "query" --agent claude-code,codex --exclude-agent docs
$ agent-history search "query" --subagents exclude  # skip subagent transcripts
$ agent-history search "query" --role assistant     # answers, not prompts
$ agent-history search "query" --per-session 1      # one hit per conversation
$ agent-history search "query" --min-chars 120      # skip one-line chunks
$ agent-history search "query" --mode text  # exact match, no Ollama needed
$ agent-history search "query" --mode vector   # embeddings only
$ agent-history search "query" --sort newest   # chronological, not relevance
$ agent-history search "query" --json       # machine-readable
$ agent-history search "query" --machine laptop   # one machine's history
$ agent-history search "query" --since 30d  # only the last 30 days
$ agent-history search "query" --as-of 2026-09-01  # the past as it stood then
$ agent-history search "query" --recency 30d       # lean toward recent chunks
$ agent-history "query"                     # `search` is the default

$ agent-history fact set lvtd-fleet-1 ssh linuxuser@100.103.244.8
$ agent-history fact get lvtd-fleet-1 --history    # every value, current marked
$ agent-history fact list                   # every fact in force
$ agent-history eval queries.jsonl --mode all      # hit@1, recall@k, MRR

$ agent-history sessions                    # the 20 most recently active sessions
$ agent-history sessions -n 50 --cwd myapp  # more, from one directory (--json too)
$ agent-history show <session-or-origin>    # the conversation around a hit
$ agent-history show s1 --around 2026-06-14T22:03  # centred on a timestamp
$ agent-history serve                       # web viewer at http://127.0.0.1:8127

$ agent-history ship                        # send redacted records to the hub
$ agent-history search "query" --hub        # hub only (see "Hub" below)
$ agent-history mcp serve                   # MCP tools on 127.0.0.1:8765 (hub; [mcp] extra)

$ agent-history docs add ~/ops-library      # index a documentation tree
$ agent-history import claude-ai <dir>      # backfill a claude.ai data export
$ agent-history inject off                  # injection is on by default
$ agent-history inject toggle               # or /history:inject in Claude Code
$ agent-history strict on                   # compliance profile (see below)

$ agent-history index                       # incremental update (the default)
$ agent-history reembed                     # switch embedding model, keep everything
$ agent-history purge --cwd ~/clients/acme  # permanently delete matching chunks
$ agent-history purge --all --yes           # total destruction
$ agent-history purge --tenant kim --all    # hub: one tenant's index only
$ agent-history reindex --force             # wipe and rebuild (destructive)
$ agent-history sources                     # what will be scanned
$ agent-history doctor                      # diagnose the installation
$ agent-history doctor --coverage           # on disk vs indexed, torn transcripts
```

### Narrowing a search

Every chunk carries its agent, role, conversation, and whether it came from a
subagent, so a search can be narrowed on any of them. All of these are off
by default and combine freely, locally and on a hub:

| Option | Keeps | Why |
|---|---|---|
| `--agent a,b` / `--exclude-agent x` | chunks from those agents / without that one | a dozen sources now; `docs` and `claude-ai` imports can crowd out transcripts |
| `--role user,assistant,…` | those roles: `user`, `assistant`, `memory`, `doc`, `title`, `attachment`, `instructions`, `summary`, `fact` | `assistant` for what was answered or decided, `memory,doc` for what was written down, `user` for "what did I ask" |
| `--subagents exclude` / `only` | the main conversations / subagent transcripts alone | a subagent's `user` chunks are the brief its parent wrote, so they echo your own instructions back; on one real hub they were 19% of hits |
| `--min-chars N` | chunks of at least N characters | short prompts ("ssh into fleet") match semantically and say little |
| `--per-session N` | at most N hits per conversation | one long session can fill most of a result list; with `1`, `-k 8` means eight conversations |

Curated facts follow the same filters: `--role assistant`, `--subagents only`
or `--exclude-agent fact` leave them out, and `--agent fact,codex` asks for
both. `--min-chars` is the exception: a fact is short by design, and the
floor is there to drop one-line prompts. `--per-session` chooses the k hits
in relevance order and then sorts them as asked, fetching deeper only when
the cap removed something. With a hub, an `--agent` list is asked one agent
at a time, so a hub older than agent lists still answers it. To try one as a default before adopting it, run it through
[`eval`](#measuring-search-quality-eval), which takes the same options.

A search hit is a chunk; `show` prints the conversation it came from.
Relevance ranking has a mild recency preference: results whose scores are
effectively tied go to the newer chunk, so superseded information only
outranks its own correction when relevance genuinely separates them.
`--sort newest` (also a dropdown in the viewer) orders results
chronologically instead — useful for "what's the latest on X". Start
with `doctor` if anything misbehaves — it reports whether Ollama is reachable,
what was detected, how much is indexed, and flags misconfigured docs roots.

### Coverage: what never made it into the index

`doctor --coverage` answers "would a search even see it?" before a search
quietly comes back empty. It is read-only (the index is opened with SQLite
`mode=ro`) and looks exactly where indexing looks, because it asks the
adapters for their sources. It reports:

- **Per source** — each adapter root, including the WSL→Windows profile and
  `AGENT_HISTORY_EXTRA_DIRS`: files (or, for OpenCode/Hermes, sessions) on
  disk versus in the index, how many indexed files have since vanished from
  disk (the index is their only copy), and the newest source mtime against
  the newest indexed timestamp. A gap over `--lag-hours` (default 6) is a
  warning: indexing is not reaching that source.
- **Suspect transcripts** — 0-byte files, files whose last line is not valid
  JSON (cut off mid-write by a crash), and session files under 2 KiB that
  hold no conversation. For Claude Code remote-control sessions the
  `bridgeSessionId` is shown — from the transcript itself or from
  `~/.claude/sessions/*.json` — so the conversation can be re-fetched later.
- **Surfaces no adapter can see** — claude.ai chats, claude.ai/code cloud
  sessions, Claude Desktop Cowork, and other machines in your fleet.

```console
$ agent-history doctor --coverage
claude-code ~/.claude/projects
    1174 files on disk, 1086 indexed, 2686 gone from disk (index-only)
    newest source 2026-10-14 12:58 · newest indexed 2026-10-14 11:59 · ok
...
suspect transcripts: 1 empty, 5 truncated (last line not JSON), 6 short (< 2.0 KiB)
  EMPTY     /mnt/c/Users/me/.claude/projects/C--Users-me/29446839-….jsonl  0 B  2026-10-13 20:41
```

It exits 1 when a source lags or a transcript is empty or truncated (short
stubs alone do not fail it), so it can run from cron. `--json` prints the full
report, including every suspect file.

## What was true when: the time axis

Retrieval returns what was said, including things later changed: a host you
moved off, a default you replaced, a decision you reversed. Both statements
are in the index, both are genuinely relevant, and the older one can win on
relevance outright. Four opt-in tools reason over time without summarising
anything or running a model.

**A time window.** `--since` and `--until` keep only chunks inside a range;
`--as-of` is `--until` by its purpose — *ask the past as it stood then*.
Values are an age (`12h`, `30d`, `2w`, `6mo`, `1y`), `today` / `yesterday`,
a date (`2026-10-01`, local time; as an upper bound it includes the whole
day), or an ISO time. An undated chunk is outside every bounded window.

```console
$ agent-history search "default model" --since 2026-10-01
$ agent-history search "where does winch-bot run" --as-of 2026-07-01
```

**A recency half-life.** `--recency 30d` multiplies each hit's relevance by
0.5^(age / 30 days), so "current state of X" leans to recent statements while
a much stronger old hit can still win. It is off unless asked for — default
ranking is unchanged — and each reweighted hit shows its factor
(`score=0.0123 rec×0.42`). Hybrid scores sit in a narrow band (a hit both
arms rank first scores only about twice one that a single arm ranks first),
so one half-life of age outweighs most relevance gaps: start generous
(`90d`–`180d`) and let [`eval`](#measuring-search-quality-eval) show whether a
shorter one helps on your questions.

**Possibly superseded.** Among one search's hits, an older hit is flagged
when a newer hit from a different conversation states a different value for
the same operational identifier and no longer mentions the old one:

```text
#3  score=0.0161  2026-08-01T10:14  claude-code/assistant  ~/fleet
    file: ...
    ⚠ possibly superseded: login host 45.76.1.5 -> 100.103.244.8 in a newer statement (#1, 2026-10-11)
```

It understands `ssh`/`scp`/`rsync` targets (same user, new host), IPv4
addresses and `~/.ssh` key files (which must share a distinctive nearby word,
such as the box's name), and `name 1.2.3` versions. Two hits from one session
are never compared — one conversation legitimately names several hosts — and
loopback, netmask and similar addresses are ignored. It is advisory: nothing
is hidden or reordered, and the same warning reaches injected context, JSON
(`superseded_hint`), the viewer and MCP. `AGENT_HISTORY_SUPERSEDE_HINTS=0`
turns it off.

**Curated facts.** A fact is an explicit statement of the current value of
one thing — the "moved on <date>, use X now" lines a hand-kept memory file
accumulates, made queryable:

```console
$ agent-history fact set lvtd-fleet-1 ssh linuxuser@45.76.1.5 --since 2026-08-01
$ agent-history fact set lvtd-fleet-1 ssh linuxuser@100.103.244.8 --since 2026-10-11
$ agent-history fact get lvtd-fleet-1
lvtd-fleet-1 · ssh: linuxuser@100.103.244.8  (since 2026-10-11; was linuxuser@45.76.1.5 before)
$ agent-history fact get lvtd-fleet-1 --as-of 2026-09-01
lvtd-fleet-1 · ssh: linuxuser@45.76.1.5  (since 2026-08-01; until 2026-10-11)
$ agent-history fact get lvtd-fleet-1 --history
lvtd-fleet-1 · ssh
    2026-08-01T00:00  linuxuser@45.76.1.5  (pc)  fact-…
  * 2026-10-11T00:00  linuxuser@100.103.244.8  (pc)  fact-…
$ agent-history fact retire winch-bot host       # no longer true, kept as history
$ agent-history fact forget lvtd-fleet-1 --yes   # delete its rows on this machine
```

- **History, not overwrite.** Every `set` adds a row; the value in force is
  the row with the latest `--since` at or before the moment asked about, so
  back-dating a value never replaces a later one. Subjects and predicates
  compare without case or spacing.
- **First in search and injection.** A query that names a fact's subject
  (all its words, or half of them plus a word of the predicate) gets the
  current fact as hit #1, labelled `fact`, above the usual k transcript
  hits; `--as-of` returns the fact as it was then, and `--agent fact` lists
  facts alone. Context injection, stricter (the whole subject), puts current
  facts ahead of excerpts and marks an excerpt that predates one. A fact can
  be `--scope`d to a project directory (`--scope .` is stored as an absolute
  path): injection then uses it only inside that tree, strict mode injects
  only scoped facts, and search shows it with its scope (with `--cwd`, when
  the scope contains that substring). Injected facts are capped at 300
  characters each and count toward injection's size limit.
- **Beside the index, never in it.** Facts live in `facts.db`, written only
  by `fact set`/`retire` or the `history_fact` MCP tool. Nothing reads
  transcripts to propose facts (a possible later, opt-in phase of #40).
- **Raw here, redacted when shipped.** This machine keeps a fact as written
  (redacted too when `AGENT_HISTORY_REDACT` or strict mode is on, as for the
  index). With `AGENT_HISTORY_HUB_INGEST` set, a
  fact's redacted copy is queued for the hub (`inbox/<tenant>/<machine>/facts/`,
  beside session notes) and sent at once, or on the next `fact ship` or
  hook run if the hub is unreachable; the hub redacts again. `fact get` and
  `fact list` merge this machine's facts with the hub's when
  `AGENT_HISTORY_REMOTE` is set (`--local` / `--hub` pin one side).

All four apply over the hub too: the window and half-life travel to the hub
as absolute UTC instants (so the hub's time zone does not matter), and are
only sent when used, so an older hub still answers every other search.

## Context injection (on by default)

A UserPromptSubmit hook embeds each prompt you type (locally, via Ollama),
retrieves past-session chunks relevant to it, and injects them as context —
so the agent already knows what you decided three weeks ago without being
told. **No LLM is involved anywhere**: the prompt is the retrieval query, and
the chunks arrive verbatim. Tools like claude-mem buy this experience with
LLM compression passes; retrieval alone turns out to be enough, at zero token
cost and with the original wording intact.

Because it's local, free, and precision-gated, it's **on by default**. Each
time it fires you see a notice listing exactly which memories were pulled —
one line per chunk with its date, source, project, and a snippet:

```text
📚 agent-history recalled 3 memories  ·  /history:inject to toggle
  · 2026-07-08 claude-code/assistant xiu — Fixed and deployed — restart the app…
  · 2026-08-06 claude-code/assistant agent-history — README rewrite for the archive…
  · 2026-02-25 codex/user openclaw — ssh into opsbot, work on openclaw…
```

That's shown only to you, never added to the model's context, so you can
watch recall happen while the agent works without it stopping to narrate.
Toggle with **`/history:inject`** in Claude Code, or `agent-history inject
off` / `on` / `toggle` (and `AGENT_HISTORY_INJECT=0` force-disables).

Unsolicited context has to clear a much higher bar than search results you
asked for, so this is precision-first:

- An absolute distance cutoff rejects weak matches
  (`AGENT_HISTORY_INJECT_MAX_DIST`, default 17.0 — recalibrate if you change
  embedding model).
- At most 3 chunks / ~2 KB per prompt; near-duplicates collapse.
- Your current session's own chunks are excluded — the agent has that context.
- **Directory-aware, two ways.** First, cheaply: retrieval prefers your
  current directory — same-project chunks pass the normal cutoff, other-
  project chunks must clear a tighter "near-exact" bar
  (`AGENT_HISTORY_INJECT_CROSS_DIST`, default 14.0), so obviously-unrelated
  matches are dropped before injection while genuinely strong cross-project
  ones still come through. Second, as a backstop: the injected header names
  your current project and tells the agent to disregard any excerpt from
  elsewhere. The embedder can't know relevance, so this pairs a simple
  distance heuristic with the model's judgement. (Deliberate `search` stays
  fully cross-project; only automatic injection is scoped.)
- **Curated facts first.** A current [fact](#what-was-true-when-the-time-axis)
  whose whole subject the prompt names is injected ahead of the excerpts, as
  the value in force, and an excerpt that a newer statement or a current fact
  appears to replace carries a `⚠ possibly superseded` / `predates the current
  fact` line — the agent sees the warning, not a bare stale value. Facts need
  no embedder, so they still arrive when Ollama is down.
- **Optional age limits.** `AGENT_HISTORY_INJECT_MAX_AGE=365d` never injects
  an older excerpt; `AGENT_HISTORY_INJECT_RECENCY=90d` orders the candidates
  that pass the cutoff by relevance x recency. Both are off by default.
- Trivial prompts ("ok", "go ahead") and slash commands are skipped.
- When Ollama is down, nothing is injected. Every failure path is a silent
  no-op — the hook can never break a prompt.

Injected recall is never itself re-indexed — the indexer refuses any record
that begins with the injection markers — so recalled memories can't compound
into a memories-about-memories loop.

## Indexing documentation trees

Transcripts answer "what was said"; a docs tree answers "what was written
down". Name your documentation roots and both land in the same index:

```console
$ agent-history docs add ~/ops-library     # persisted; hooks pick it up
$ agent-history index
$ agent-history search "gateway restart runbook" --agent docs
```

`docs add` stores the root in the data directory, which is sturdier than an
environment variable — hook processes don't reliably inherit shell exports,
Windows especially. `docs list` and `docs remove` manage the set;
`$AGENT_HISTORY_DOCS_DIRS` (os.pathsep-separated) still works and is merged
in.

This is opt-in only — nothing is auto-detected, because guessing at
documentation locations would mean reading directories you never offered.
Markdown and `.txt` files are indexed recursively; vendored and hidden
directories (`node_modules`, `.git`, …) are pruned; symbolic links may not
escape the configured root; files over 2 MB are treated as logs and skipped.

**Edited docs are refreshed, not accumulated.** Docs are the one *mutable*
source the store reads, so they get the one delete path it has: when a
re-read document no longer contains some previously indexed text, those
chunks are removed — from the vector index and the full-text index too. The
rule is deliberately conservative: only a document actually re-read this run
is eligible, text still produced by *anything* this run survives, and a doc
deleted from disk keeps its chunks. Transcripts are append-only in reality
and stay append-only here.

Together with the Hermes and OpenClaw adapters, this lets one agent-history
install replace a hand-rolled transcript-plus-docs indexer — the
Ollama-embeddings-over-SQLite pattern many ops setups grow — with hybrid
search over all of it.

## Backfilling claude.ai chats from a data export

claude.ai chats (web, desktop, mobile) leave nothing on disk to index. Their
one full-fidelity copy is the data export: Settings → Privacy → **Export
data**, then unzip what arrives by email. Import it explicitly:

```console
$ agent-history import claude-ai ~/Downloads/claude-export --dry-run   # count only
$ agent-history import claude-ai ~/Downloads/claude-export
$ agent-history search "shed footings" --agent claude-ai
```

`--dry-run` parses and counts without touching the index or Ollama. The real
import goes through the normal indexing path — chunked, content-hashed,
embedded — under the same lock as `index`.

**Re-importing a newer export adds only what is new.** Every chunk is keyed
by the hash of its text, so messages already indexed are skipped wherever
they came from. A message edited in place adds one chunk for its new text;
a regenerated reply identical to one already stored adds nothing. The
conversation's name and claude.ai's summary are a separate `title` record,
so renaming a chat never re-imports its messages.

What gets indexed, all under agent `claude-ai`:

| export part | record role | location (`cwd`) |
|---|---|---|
| chat message text (text and voice-note blocks) | `user` / `assistant` | `claude.ai` |
| conversation name + summary | `title` | `claude.ai` |
| text extracted from pasted/uploaded files (first 100k chars each) | `attachment` | as its chat |
| project knowledge files | `doc` | `claude.ai/project/<name>` |
| project description + custom instructions | `instructions` | `claude.ai/project/<name>` |
| account memory, project memories, memory files | `memory` | `claude.ai` or its project |
| Claude Design chats | `user` / `assistant` | `claude.ai/design/<project>` |

`session_id` is the conversation's uuid, so `agent-history show <uuid>`
prints the whole chat. Skipped on purpose: thinking blocks and tool calls and
results (the same line the Claude Code adapter draws), uploaded images,
`frames` (rendered artifact versions and their assets), `artifact_data`,
`feedback`, login metadata and Claude Code cloud routine metadata. The export
doesn't currently say which project a chat belongs to; if it ever does
(`project_uuid`), chats are filed under the project.

Imported chunks count as history the index alone holds: no adapter re-reads
the export, so `reindex` refuses to wipe them without `--force`, exactly as
for deleted transcripts. `agent-history purge --agent claude-ai --yes`
removes the whole import. Because their `cwd` is never a real directory,
claude.ai chunks only reach context injection through the strict
cross-project cutoff, and never under `strict`.

The 180 MB `conversations.json` of a real three-year export parses in about
5 seconds with ~100 MB peak memory: it is streamed one conversation at a
time rather than loaded whole (`json.load` alone peaks above 500 MB).

## Your index outlives the transcripts

**Claude Code deletes your old transcripts by default.** The
[`cleanupPeriodDays`](https://code.claude.com/docs/en/settings) setting
defaults to **30 days**, and the sweep runs at startup:

> "Claude Code deletes session files and other application data older than
> this period at startup."

There is no archive directory. Once a transcript is swept, **your index is
the only surviving copy of that conversation**.

This is why the tool is worth more than it first appears. On the machine it
was built for, **67% of a mature index — 25,727 chunks from 1,616 files —
came from transcripts that no longer existed.** The single best answer to
"why does SessionEnd not always fire" lived in a session whose file had been
swept months earlier.

The mechanism is measurable. Comparing every indexed transcript against what
was still on disk:

| | files | median age of last message |
|---|---|---|
| still on disk | 250 | 7 days |
| **deleted** | **1,616** | **51 days** |

**Not one deleted transcript was younger than 30 days.** That hard floor is
the retention policy, not chance. Unless you have raised `cleanupPeriodDays`,
the same is happening to you right now.

Three commands follow from this, in order of how often you should use them:

- **`index`** — incremental, additive, the default. All the hooks ever run.
- **`reembed`** — switching embedding models **without losing anything**. The
  chunk text lives in the index, so a model change never needs the source
  files: every stored chunk is re-embedded with the new model, staged
  alongside the old vectors (searches keep working throughout), and swapped
  in atomically at the end. Interrupted by a crash or an Ollama outage, it
  resumes where it stopped.
- **`reindex`** — wipe and rebuild from what is on disk *today*, permanently
  discarding history whose sources are gone. It now **refuses to run** when
  orphaned chunks exist, tells you how many conversations you'd lose, and
  requires `--force` to proceed. There is almost never a reason to: model
  changes have `reembed`.

## Backups and integrity checks

Because the index is often the only copy, **every store is backed up before
anything that could lose rows**, automatically:

- the **first write by a new version** of agent-history (an upgrade that
  migrates the schema, or simply writes differently);
- every command that **deletes or rewrites rows**: `reindex`, `reembed`,
  `purge`, `scrub`, `relabel`, `notes migrate-records`, `fact forget`.

A backup copies every SQLite store in the data home (`index.db`,
`notes.db`, `facts.db`, `ship.db`, …) with SQLite's backup API, so it is a
consistent moment even while a hook or the hub's indexer is writing, plus
the token file and any batches still waiting to ship. Its `manifest.json`
records each file's size, SHA-256, `PRAGMA integrity_check` result and the
row count of every table. It is written under a temporary name and renamed
into place only when complete.

**If the backup cannot be made, the operation does not run** — no free
space means no purge, and a new version does not touch an index it could not
copy first. After the operation, the live data is checked; if that fails,
the message names the backup to restore.

```console
$ agent-history backup                         # one now (kept until you delete it)
$ agent-history backup list
$ agent-history backup check                   # live data vs the latest backup
$ agent-history backup check --no-loss         # ...and fail if any table shrank
$ agent-history backup verify latest           # the backup itself is intact
$ agent-history backup restore <name> --yes    # backs up what it replaces first
$ agent-history backup create --all-tenants    # every tenant on a hub
```

`check` looks for corruption (`PRAGMA integrity_check`, and FTS5's own
check of the full-text index), for the index's three views of the chunks
(rows, full-text entries, vectors) disagreeing, and for any table with fewer
rows than the backup recorded. Fewer rows is reported, and fatal with
`--no-loss` — the check to run after an upgrade, when nothing should have
been removed.

Backups live in `<data home>/backups/` (`AGENT_HISTORY_BACKUP_DIR` puts them
on another disk). The newest five automatic ones are kept
(`AGENT_HISTORY_BACKUP_KEEP`); ones you make by hand stay until you delete
them. A backup is the same disk as the index unless you move it: it guards
against a bad command or a bad upgrade, not a dead drive — copy
`backups/` off the machine for that.

## Deleting on purpose: `purge`

`reindex --force` is a sledgehammer; `purge` is a scalpel — permanent,
targeted deletion for when you *want* specific history gone (a client
offboarding, a contract-end destruction obligation, a project you no longer
want recalled):

```console
$ agent-history purge --cwd ~/clients/acme     # preview: counts, deletes nothing
$ agent-history purge --cwd ~/clients/acme --yes   # actually delete
$ agent-history purge --session <id>           # one conversation
$ agent-history purge --agent codex            # one source
$ agent-history purge --all --yes              # total destruction
```

`--cwd`, `--session`, and `--agent` are AND-combined; without `--yes` it only
prints how many chunks *would* go. On a multi-tenant hub, `--tenant NAME`
points any of them at that tenant's index file and no other (`purge --tenant
kim --all --yes` erases a tenant's transcript index). Session notes, facts
and the `.processed/` batches are separate stores; facts have `fact forget`. Deletion removes them from the vector and
full-text indexes too. One caveat from content-hash dedup: identical text is
stored once, attributed to its first-indexed source — so a surgical purge
matches that stored attribution, and `--all` is the only guaranteed-complete
wipe.

**Backups keep what `purge` removes.** Like every deleting command, it backs
up first and then names that backup. To erase the data everywhere, delete
that backup and any older ones (`agent-history backup delete <name> --yes`),
or run the purge with `AGENT_HISTORY_AUTO_BACKUP=0`.

## Regulated work: `strict` mode

Context injection's convenience is a liability when one machine holds more
than one client's history: a chunk from project A can ride into a prompt for
project B on semantic similarity alone, and every agent here sends prompts to
a commercial cloud LLM. For regulated data (CUI and the like), turn on the
compliance profile:

```console
$ agent-history strict on        # or AGENT_HISTORY_STRICT=1
```

In strict mode injection **defaults off**, and when you do enable it
(`inject on`) recall is **scoped to the current project** — a chunk from
another directory can never be injected. This closes the cross-project
egress path.

Strict mode narrows *behaviour*; it does not de-scope the *data*. The index
still holds verbatim text at rest (see [Privacy and security](#privacy-and-security)),
so encryption-at-rest, access control, and a destruction plan
([`purge`](#deleting-on-purpose-purge)) remain your responsibility. For
genuinely regulated deployments, index on in-boundary hardware — never pull a
client's data onto an out-of-boundary workstation.

## Automatic indexing

The plugin installs three hooks:

- **SessionEnd** — index when a session finishes
- **PreCompact** — index before context is compacted, so nothing is lost
- **UserPromptSubmit** — powers
  [context injection](#context-injection-on-by-default) (on by default; it
  retrieves, it never indexes)

One honest caveat: `SessionEnd` only fires on a *clean* exit. Kill the
terminal and that session is not indexed until something else triggers a run.
Gaps are expected, not a bug. For a safety net, add a cron entry that indexes
only when a session is active and something actually changed:

```cron
0 * * * * pgrep -x claude >/dev/null && agent-history trigger cron --if-changed
```

On Windows, `install.ps1` offers to register the equivalent Scheduled Task.

`--if-changed` compares your sources against a stamp file and exits before
waking Ollama when there is nothing to do, so idle hours cost nothing. For
docs trees it consults the adapter's own pruned file set, so a touched
`node_modules` readme or an oversized log can never wake the indexer.

### Near-real-time (hooks + `sync`)

Add `agent-history-notify` as a `Stop` and `SubagentStop` hook (the plugin's
`hooks.json` does). It returns in ~50 ms; a detached `agent-history sync`
then indexes and ships only the appended turns, usually within a second, at
most one pass per 15 s (`AGENT_HISTORY_SYNC_INTERVAL`) with a trailing pass so
the last turn is never skipped. Syncers never overlap (`.sync.lock`; index and
ship keep their own locks), the hub being down just leaves batches in the
outbox, and redaction still happens before anything is written to it. Keep a
cron safety net for sessions killed without a hook:

```cron
*/5 * * * * (set -a; . $HOME/.config/agent-history/env; set +a; agent-history sync --sweep --once) >> $HOME/.local/share/agent-history/sync.log 2>&1
```

Shipped records are keyed by session id + timestamp + role + text, not by file
path, so moving a transcript between `~/.claude/projects/<dir>/` folders
re-sends and re-indexes nothing. After upgrading, run
`agent-history ship --migrate-keys` once *before* moving files.

## Hub (central history)

One index on a server that every machine feeds and every session can search —
for agents that live on servers, for a workstation that is asleep, or for a
box that should hold the index so laptops need no Ollama. Design and
rationale: [#25](https://github.com/d3layd/agent-history/issues/25).

```
 pc        ─┐                                       ┌─▶ agent-history search (merged with local)
 laptop    ─┼─ ship: redacted Records, rsync/ssh ─▶ HUB ─┼─▶ agent-history show
 server    ─┘    (upload-only key)        inbox → index  └─ read-only key, serve-ssh
```

**Machines ship Records, not transcripts.** `agent-history ship` runs the
normal adapters, redacts every record, and uploads JSONL batches (about 4% of
the raw transcript bytes). The hub embeds with its own Ollama, so `meta.model`
stays consistent and shippers need no embedder.

```console
$ export AGENT_HISTORY_HUB_INGEST=ahub@hub.example.invalid:   # rsync destination
$ export AGENT_HISTORY_HUB_KEY=~/.ssh/agent-history-ingest    # optional identity
$ agent-history ship --dry-run              # counts only, writes and sends nothing
$ agent-history ship                        # new records since last time
$ agent-history ship --backfill-from-index  # also chunks already in your index,
                                            # incl. transcripts that no longer exist
```

Run it from a timer (`deploy/machine/`: a systemd user unit every 2
minutes). With `AGENT_HISTORY_HUB_INGEST` set, the SessionEnd/PreCompact
hooks ship too, right after indexing. Shipping is idempotent and crash-safe:
new records are written to `outbox/` as immutable batches (at most 2000 lines),
uploaded with `rsync --remove-source-files`, and a record counts as shipped
only once its batch has left the outbox. A failed upload leaves the batches in
place for the next run; overlapping runs no-op under a lock.

**The hub** sets `AGENT_HISTORY_HUB=1` and `AGENT_HISTORY_INBOX=<dir>`. In hub
mode only the inbox is indexed (never the hub's own transcripts) and
redaction is forced on, whatever else is configured, as a second line of
defence in case a shipper misbehaves. The inbox holds
`<machine>/*.jsonl`; the machine name is the *directory*, never a field inside
a file. Once a run has committed, consumed batches move to
`.processed/<machine>/`, so a crash just re-reads them (content-hash dedup
makes that free); prune `.processed` on your own schedule. Chunks gain a
`machine` column (migrated in place, no re-embed); old rows count as the local
machine. Identical text is stored once, attributed to whichever machine's copy
was indexed first (the existing dedup rule), so `--machine` can miss a chunk
another machine said word for word.

**Searching the hub.** Set `AGENT_HISTORY_REMOTE=ssh://reader@hub.example.invalid[:port]`
(and optionally `AGENT_HISTORY_REMOTE_KEY`):

- With a local index, `search` queries both and merges them by reciprocal-rank
  fusion; the hub is asked to leave out this machine's own chunks, so nothing
  repeats. Results from other machines show `@machine`.
- Without a local index, `search` runs on the hub and prints its answer.
- `--local` / `--hub` force one side. If the hub is unreachable, `search`
  prints one warning line to stderr and still returns the local results.
- `show` looks locally first, then asks the hub, so any hit can be opened.

**Tenancy.** A hub can hold several people's or clients' history, and one
tenant must never be able to read another's. Separation is structural: **one
index file per tenant**, not a tenant column. The layout is
`inbox/<tenant>/<machine>/` for uploads and `tenants/<tenant>/` (a data home)
for each index, with one templated indexer per tenant. Ingest keys are pinned
to a single `<tenant>/<machine>` directory, so shippers know nothing of
tenants, and a machine name only has to be unique within its tenant. A search
key's `serve-ssh --tenants a,b` decides which index files it may open: a
request for any other tenant is refused (it is not filtered, the file is never
opened), `show` resolves a session only inside the allowed list, and a key
with several tenants gets their results merged and labelled. Tenant names are
`[a-z0-9][a-z0-9-]{0,40}`; anything else, or a directory that resolves
outside the tenants root, is rejected. The list comes only from the forced
command in `authorized_keys`, never from the client's request.

**Security model**

- **Redacted before it leaves.** `ship` ignores the local `AGENT_HISTORY_REDACT`
  setting: your local index may keep credentials on purpose, but nothing
  shipped does. The hub redacts again regardless. (Redaction is conservative
  pattern matching, a strong reduction and not a guarantee. For `--backfill-from-index`
  and `scrub`, stored chunks are first stitched back into whole records, so a secret
  cut by a chunk boundary is still redacted whole. A chunk that cannot be stitched
  is redacted alone, and a long base64-like run touching its edge is replaced with
  `[REDACTED:fragment]` as a safety net.)
- **Two kinds of key, both pinned.** Ingest keys are `command="rrsync -wo
  <dir>",restrict`: they can only write into one directory and cannot read
  anything back. Search keys are `command="agent-history serve-ssh
  --tenants ...",restrict`: `serve-ssh` splits `$SSH_ORIGINAL_COMMAND` with
  `shlex`, accepts only `search` and `show` (validated by the normal argument
  parser), runs them in-process and never starts a shell. `index`, `purge`,
  `scrub`, anything else, or an empty request exits non-zero. Shell
  metacharacters in a query are just query text.
- **Nothing listens on the network** beyond sshd. Ollama stays on loopback,
  and so does the optional MCP server (below), which a router on the same
  host reaches. The usual rules still apply: `AGENT_HISTORY_HOME` is `0700`/`0600`, the
  hub's disk should be encrypted (the index has no encryption of its own),
  and `.processed/` holds the redacted batches as received.
- **Tenant isolation is a software boundary.** All tenants in the examples run
  as one unix user (`ahub`), so a bug in sshd restrictions or in `serve-ssh`
  would be the only thing between a client key and another tenant's files.
  For a client you do not fully trust, give each tenant its own unix user and
  directory modes (`0700`), which makes the boundary the operating system's.

Examples, with no secrets and no real hosts, are in [`deploy/`](deploy/):
templated systemd units for the hub's indexer (`agent-history-hub-index@<tenant>`
`.service` / `.path` / `.timer`), the hub's MCP server
(`agent-history-mcp.service`), a user unit and timer for shipping, and
`authorized_keys.example`.

### `claude -p` jobs: `ship-jobs` and the final-ship hook

A user that runs headless `claude -p` jobs (one directory per job under
`AGENT_HISTORY_JOBS_DIR`, default `/var/lib/claude-jobs/jobs/<job_id>/` with
`job.json` and the stream-json `transcript.jsonl`) ships with
`agent-history ship-jobs` instead of `ship`. It sends the job's Claude Code
session files and `transcript.jsonl` through the same upload-only key, with
every record filed under `session_id = <job id>`, a `cwd` that contains the
job id, a `job` object on each line (job id, owning Duet channel, Studio task
key), batch files named `<job_id>-….jsonl`, and a header record whose text
names all of them. A parent agent reads a job back with the hub's existing
tools, no hub change needed:

```text
history_recall(query="cj_20261101T120000Z_a1b2c3", mode="text", cwd="cj_20261101T120000Z_a1b2c3")
history_show(session="cj_20261101T120000Z_a1b2c3", limit=50)
```

Every record also carries the real Claude Code session uuid
(`claude_session_id`, plus a `claude` link alias), so
`history_recall("<uuid>", mode="text")` finds the job, and on a hub with this
version `history_show(session="<uuid>")` resolves to it too.

`agent-history ship-jobs --final <job_id>` (wrapped as
`deploy/claude-jobs/ship-final`) is the runner's last step before a job is
marked done: it waits for the ship lock, adds a seal record, and exits 0 only
once rsync has confirmed every batch of the job (1 unconfirmed, 2 bad
input/config, 3 lock busy). Indexing is confirmed separately, on the runner's
side and never as the job user, with
`agent-history confirm-indexed --job <job_id> [--json] [--receipt FILE]`
(0 indexed, 1 not yet or partial, 2 config or credential error). It reads
the hub through the existing read-only `serve-ssh` key
(`AGENT_HISTORY_REMOTE`), or with `--local` the local index on the hub itself. Install steps: [`deploy/claude-jobs/`](deploy/claude-jobs/README.md);
design: [`docs/design/claude-jobs-shipper.md`](docs/design/claude-jobs-shipper.md).

### Boat VMs: `boat-export` before teardown

A fleet job that runs Claude Code on Boat VMs keeps them stopped (paused)
until the work is verified, then runs
`agent-history boat-export --job <job_id> <vm_id>...`. For each VM it resumes
a stopped VM, pulls `~/.claude/projects` with `boat scp`, checks the copy
against the VM's own sha256 list, stops the VM again (also on failure), and
ships the job with `ship-jobs --final`. Exit 0 means the hub confirmed every
record arrived **and** the runner's `confirm-indexed` result
(`<jobs>/<job_id>/index-confirmed.json`) says the VM's session is indexed, so
the VM is safe to delete. Exit 4 means arrived, awaiting index confirmation.
The tool never deletes a VM itself (`boat-export --check … && boat delete …`
is the caller's step). Each VM reads back as
its own conversation, `history_show(session="<job_id>/vm/<vm_id>")`.
`--check` re-confirms an earlier export from its receipt without calling
Boat. Design: [`docs/design/boat-vm-transcripts.md`](docs/design/boat-vm-transcripts.md).

### Session notes

Next to raw transcripts, agent-history keeps **session notes**: short
summaries that point back to their source, in a separate store (`notes.db`,
never the transcript index). Each note names exactly one source: a claude.ai
conversation URL, a Duet channel id plus the message id that starts the
span, or a Claude Code session id plus machine (plus the job id for
`claude -p`). A note without one is refused. Notes are append-only (a
correction is a new note with `corrects`), redacted like transcripts, and
searchable on their own:

```text
agent-history notes add --title … --summary … --session <id> [--job cj_…]
agent-history notes sweep                              # sessions idle >= 5 min (model summary)
agent-history notes scrub <note_id> --pattern - --reason "…"  # admin: remove a leaked secret
agent-history notes search "what was decided"
history_note(...)                                      # MCP, write scope
history_recall(query, notes="only" | "include")        # default "exclude"
```

The sweep's summary comes from `claude -p --model haiku` with the machine's
own Claude login (never `ANTHROPIC_API_KEY`), on redacted text; if that call
fails the note falls back to first prompt + last message and is marked
`fallback`. Each note carries the `history_show` call that opens its source
span. On a
hub, notes arrive in `inbox/<tenant>/<machine>/notes/` and are indexed by
the same unit. Install: [`deploy/notes/`](deploy/notes/README.md); design:
[`docs/design/session-notes.md`](docs/design/session-notes.md).

#### history_record writes notes

`history_record` writes notes too (they are summaries, not transcripts): same
tool, same arguments, plus optional `duet_channel_id` / `duet_message_id` or
`session_id` / `session_machine` / `job_id` for a source link. Reads keep
working: the default `history_recall` and `surface="claude-ai-chat"` still
return records in their old shape, and `history_show` on a record's
`claude-ai-chat-…` session returns the chat's record trail. On a hub,
`agent-history notes migrate-records` (dry run unless `--yes`) moves existing
records out of the transcript index; the indexer diverts any old-format
record line to notes. Design: [`docs/design/history-record-notes.md`](docs/design/history-record-notes.md).

### MCP server: claude.ai and other MCP clients

`agent-history mcp` serves the hub's history as MCP tools, so Claude on
claude.ai (web, desktop, mobile) can search it and add to it through an MCP
router ([#28](https://github.com/d3layd/agent-history/issues/28)). It needs
the optional extra: `uv tool install 'agent-history[mcp]'`.

| Tool | Scope | What it does |
|---|---|---|
| `history_recall` | read | `query`, `k` (default 8, max 20), optional `agent` / `surface`, `cwd`, `machine`, `mode` (`hybrid`/`text`/`vector`), `sort`. Ranked hits: timestamp, agent, machine, project, session, role, score, a 700-character excerpt. Same `search_rows` as the CLI. |
| `history_show` | read | `session` (from a hit), optional `around` (timestamp) and `limit` (default 20, max 50). The conversation, oldest first, 1500 characters per message. Same `rows_for` as `show`. |
| `history_record` | write | `title`, `summary`, `decisions[]`, `findings[]`, `links[]`, `chat_url`, `project`. Saves a chat record and returns its id. |
| `history_fact` | write | `subject`, `predicate`, `value` (or `retire: true`), optional `since`, `source`, `scope`. Records a [fact](#what-was-true-when-the-time-axis), redacted, into the tenant's facts inbox; stored on the hub's next indexing run. |
| `history_fact_get` | read | Optional `subject`, `predicate`, `as_of`, `history`. The value in force (with what it replaced), every fact in force, or the full timeline. |

`history_recall` also takes `since`, `until` (as of) and `recency` (a
half-life), the [narrowing options](#narrowing-a-search) (`role`,
`exclude_agent`, `subagents`, `min_chars`, `per_session`; `agent` may be a
comma list), puts matching facts first (`role: fact`, with a `fact` object),
and gives a hit that a newer hit appears to replace a `superseded_hint` and a
one-line `warning`.

**Records take the shipped path.** `history_record` builds one Record
(agent `claude-ai-chat`, role `summary`, timestamp now, `cwd` = project),
redacts it with the same `redact` pass `ship` uses, and writes it as an
immutable batch into `<inbox>/<tenant>/<machine>/`, where `<machine>` comes
from the token (e.g. `claude-ai`). The tenant's indexer picks it up like any
shipped batch (and redacts again in hub mode), so it is embedded, searchable
and attributed like everything else. Records from one chat share a session
(`claude-ai-chat-<chat uuid>` when `chat_url` is a claude.ai chat URL), so
`history_show` on it returns the chat's whole record trail. It is
**append-only**: there is no update or delete tool, and the write path only
ever creates new files (`O_EXCL`, renamed into place). Undoing a record is an
operator's job (`purge` on the hub).

**Tokens.** Every request carries `Authorization: Bearer <token>`. A token has
a scope (`read`: recall and show; `write`: record) and names exactly **one
tenant** index; the tenant never comes from the request. Prefer one scope per
token. Only SHA-256 hashes are stored, in `$AGENT_HISTORY_HOME/mcp-tokens.json`
(or `AGENT_HISTORY_MCP_TOKENS`), which must be `0600` and owned by the server's
user; comparison is constant-time, and adding or revoking a token takes effect
without a restart. Tokens are never accepted in argv and never logged.

```console
$ agent-history mcp token add router-read  --scope read  --tenant kim
ahmcp_...                                   # printed once; give it to the router
$ agent-history mcp token add router-write --scope write --tenant kim --machine claude-ai
$ agent-history mcp token list              # names, scopes, tenants, key age; no secrets
$ agent-history mcp token rotate router-read  # new secret, same name/scope/tenant
$ agent-history mcp token revoke router-read
$ agent-history mcp serve                   # http://127.0.0.1:8765/mcp
$ agent-history mcp serve --stdio           # local testing; token from AGENT_HISTORY_MCP_TOKEN
```

**Rotation.** `token rotate` replaces a token's secret in place and prints
the new one once; the old secret stops working as soon as the file is
written (a running server re-reads it). `token list` shows each token's
`created` and `rotated` dates and its `age_days` since the last of them, so a
key that was pasted somewhere it should not have been — or has simply been
around too long — is visible. Tokens created before these dates were kept
show no age: rotate them to start the clock. Update the router's env file
with the new value right after rotating.

For a single-tenant setup the tokens can come from the environment instead:
`AGENT_HISTORY_MCP_READ_TOKEN` / `AGENT_HISTORY_MCP_WRITE_TOKEN` (32+
characters), with `AGENT_HISTORY_MCP_TENANT` and `AGENT_HISTORY_MCP_MACHINE`.

**Transport and exposure.** Streamable HTTP, stateless with JSON responses
(one request, one answer, nothing to keep alive), on **127.0.0.1:8765** by
default. A non-loopback `--host` is refused unless you pass
`--allow-non-loopback`, which belongs only behind an authenticating
TLS-terminating proxy. Requests without a valid token get `401` before they
reach MCP; `GET /healthz` answers `ok` and nothing else. The tool list a
client sees is filtered to its token's scope, and every call is checked
again. Calls are rate-limited per token (60 reads and 20 writes a minute).
Results never include a hit's origin (the file path on the machine that
shipped it); `project` is the session's working directory, as `search` shows.

**Mounting it in [mcp-router](https://github.com/d3layd/mcp-router).** The
router connects to each upstream once, with fixed headers, and decides per
client what it may call. Run the server on the router's host (loopback), as
`deploy/hub/agent-history-mcp.service`, and mount it **twice**, once per
token, so the router's grants are per server and no single upstream entry can
both read and write:

```yaml
# router.yaml
servers:
  - id: history                     # tools: history__history_recall, history__history_show
    transport: http
    url: http://127.0.0.1:8765/mcp
    headers:
      Authorization: Bearer ${AGENT_HISTORY_READ_TOKEN}
  - id: history-rec                 # tool: history-rec__history_record
    transport: http
    url: http://127.0.0.1:8765/mcp
    headers:
      Authorization: Bearer ${AGENT_HISTORY_WRITE_TOKEN}
    operations:
      history_record: create        # "record" is not a verb the router knows

roles:
  history-user:
    - server: history
      allow: [read]
    - server: history-rec
      allow: [create]

clients:
  - id: claude-ai
    roles: [operator, history-user]   # add to the client's existing roles
```

The same two entries carry the fact tools: `history_fact_get` (read) on
`history`, and `history_fact` (write) on `history-rec`, which needs its own
`operations` line (`history_fact: create`) and a router refresh to appear.

Put the two tokens in the router's environment file (`/etc/mcp-router/env`,
`0600`), never in `router.yaml`, and restart the router (a `servers` change
needs one). `history_recall` and `history_show` are classified `read` by
their `readOnlyHint`. A client that should only search gets just the
`history` rule. Records arrive under the write token's `machine`, which says
which client wrote them but not which router client called; if that matters,
give each such client its own write token and upstream entry.

A profile instruction that makes claude.ai use the tools at the right moments
is drafted in [`docs/claude-ai-profile-instruction.md`](docs/claude-ai-profile-instruction.md).

## Session identity

A source's session id is not a good identity. One conversation can have
several (a local Claude Code uuid, a remote-control `session_…` / `cse_…`
pair, a teleported copy, an export id), and synthetic ids collide: every
project's memory file used to be `memory:MEMORY`, and two machines on a hub
can report the same id.

So every session gets a **uid** of the tool's own: a UUIDv5 over
`agent`, `machine` and the source id (plus the file path for memory and doc
files). It is a pure function of the record, so a rebuild, another machine and
the hub all arrive at the same uid, and once a chunk carries one it never
changes. The raw id stays as metadata and as an alias.

Other ids are linked to the uid only on hard evidence. Today that is Claude
Code's remote-control bridge: the `bridge-session` line in a transcript, or
`bridgeSessionId` in `~/.claude/sessions/*.json`, links `cse_<x>` and
`session_<x>`. When the evidence shows two sessions are one conversation, the
earliest stays canonical and the other's ids become aliases of it.

You never have to type a uid. `show`, `purge --session` and anything else
that takes a session accept the uid, the raw id (what `claude --resume`
takes) or any alias:

```bash
agent-history show 0b6a5a8e-1111-4222-8333-944455556666   # raw Claude Code id
agent-history show session_01AbCdEfGhJkLmNpQrStUvWx       # its remote-control id
```

`search --json` and `show --json` report `session_uid` next to `session_id`.
Records shipped to a hub carry the uid and their aliases, but the hub only
trusts what it can authenticate: it derives the uid from the machine that
sent the record, which is the record's inbox directory (the one that
machine's ingest key is pinned to in `authorized_keys`). A shipped uid is
kept only when it matches that derivation, and any other is ignored, so no
machine can file history under another's conversation. **Name each machine's
inbox directory exactly as that machine's `AGENT_HISTORY_MACHINE` (or short
hostname).** If they differ, the hub still indexes everything, but its uids
for that machine will not match the machine's own local uids. Records from
older shippers, which carry no uid, are derived the same way. An existing index
is upgraded in place on first open (columns and tables added, uids backfilled
in one pass, nothing re-embedded or deleted).

### Linking a fresh-context session to its parent

A session started by Claude desktop's `start_session` tool begins with empty
context, so its transcript has no `forkedFrom` and nothing ties it to the
session that started it. To link it, make the **first line of the first user
message** (the brief) exactly:

```
Parent session id: <session id>
```

The id is the parent's 36-character lowercase UUID (what `claude --resume`
takes). The prefix is case-sensitive, with one space after the colon and
nothing else on the line; trailing whitespace, CRLF and a leading BOM are
tolerated. Example:

```
Parent session id: 0b6a5a8e-1111-4222-8333-944455556666
Research the billing bug and report back.
```

The link is stored like a fork (`fork_of` on the child, `forks` on the
parent), and is applied later if the parent is indexed after the child. Only
that one line counts: a later message, a line further down, a quote in an
assistant reply or tool output, a malformed id, the session's own id, and
subagent files are all ignored. A real `forkedFrom` link always wins. The
line stays in the indexed text as usual (redacted like any other text).
**The fleet's briefs for `start_session` should begin with this line.**

### Labelling old subagent chunks: `relabel`

Chunks indexed before subagent labels existed have no `agent_id` or
`sidechain` value. `agent-history relabel` previews what it would fill and
changes nothing; `agent-history relabel --apply` writes. It derives the id and
the sidechain flag from the `.../subagents/agent-<id>.jsonl` origin, and the
agent type from the transcript when that file is still under a transcript root
and belongs to this machine. Nothing is re-embedded or shipped. On a hub, run
it there: `agent_type` stays empty for hub rows. The preview still opens the
index (runs the additive migration and takes the indexing lock).

Undo: it only fills `agent_id`, `agent_type` and `sidechain` where they were
empty, so restore a SQLite backup, or set those three columns to NULL for the
affected rows. Take a SQLite backup first on any shared index.

## How indexing works

```
sources ──▶ adapter ──▶ filter ──▶ chunk ──▶ embed ──▶ sqlite-vec + FTS5
 (JSONL/SQLite/md)  normalise  drop noise  ~1500ch   Ollama    + SHA-256 dedup
```

**1. Adapters normalise.** Each agent stores transcripts differently — some
as append-only JSONL, some in SQLite. An adapter turns whichever it is into a
common `Record` (agent, origin, session, cwd, timestamp, role, text). Nothing
downstream knows which agent it came from.

**2. Filtering does most of the work.** The overwhelming majority of a
transcript is tool traffic — file reads, diffs, command output — which is
bulky and semantically useless to search. Measured across a real 293 MB
corpus:

| stage | count |
|---|---|
| raw JSONL lines | 87,627 |
| user/assistant messages | 63,341 |
| dropped: tool calls and results only | 54,506 |
| dropped: harness noise (`<system-reminder>`, heartbeats, …) | 321 |
| dropped: under 20 characters | 109 |
| **kept as records** | **9,195** |

That is **11.0 MB of actual conversation out of 293 MB on disk — 3.8%**. The
index is small because 96% of what agents write down is not conversation.

**3. Chunking.** Messages longer than 1500 characters are split with 200
characters of overlap, so a passage spanning a boundary is still findable.
Runs of 300+ base64-ish characters are replaced with `[blob]` first —
embedding an encoded payload costs time and matches nothing.

**4. Embedding and storage.** Each chunk goes to Ollama and lands in SQLite:
a `chunks` table for text and metadata, a `vec_chunks` virtual table
([`sqlite-vec`](https://github.com/asg017/sqlite-vec)) for the vectors, an
`fts_chunks` table (FTS5) for full-text, and `meta` recording which model
built the index.

**5. Dedup makes re-runs cheap.** Every chunk is keyed by the SHA-256 of its
text. Re-indexing skips anything already embedded, so the expensive step only
ever runs on genuinely new content — identically for file-backed and
database-backed adapters, with no per-source watermark bookkeeping.

Claude Code project memory files (`~/.claude*/projects/*/memory/*.md`) and
OpenClaw workspace markdown are indexed alongside transcripts: hand-written,
durable, and often the single most useful thing a search surfaces.

**Every Claude Code profile is scanned.** A second account run with
`CLAUDE_CONFIG_DIR=~/.claude-org` keeps its own `projects/` tree; the Claude
Code adapter reads `~/.claude/projects`, every `~/.claude-<name>/projects`,
and `$CLAUDE_CONFIG_DIR/projects` when it points elsewhere, on the Linux side
and in the WSL Windows profile, with no setting needed. The same directory
reached two ways is read once. Search results say which profile a hit came
from: a `profile` field in `--json` output (`claude`, `claude-org`, ...), and
a tag such as `claude-code[claude-org]/user` in text output for any profile
other than the default. The profile is read from the stored file path, so
chunks indexed before this change carry it too.

## What it costs

Measured on the corpus above — 276 transcript files, 293 MB, 12,819 chunks:

| | |
|---|---|
| First full index | **13 minutes** (~16 chunks/s, CPU-bound on Ollama) |
| Incremental run, nothing new | **0.4 s** — no Ollama call at all |
| Incremental run, one new session | **~27 s** for 399 chunks |
| Search, warm | **~0.2 s** |
| Search, first after boot | ~2.9 s (Ollama loading the model) |
| Index on disk | **60 MB** — 39 MB vectors, 9 MB text, 4.6 KB/chunk |
| Hook overhead at session end | **5 ms** (it detaches immediately) |
| Plugin context cost | ~37 tokens always-on |

The vectors dominate the footprint: 768 float32 values is 3 KB per chunk no
matter how short the text.

## Measuring search quality: `eval`

"It finds things" is an impression; `eval` makes it a number for *your*
index. Write down questions whose answers you know are in your history, run
them, and you get, per mode, **hit@1** (first result right), **recall@k** (a
right result in the top k) and **MRR** (mean 1/rank of the first right
result) — the measures to watch when you change the model, the chunking, or
`--recency`.

```console
$ cat queries.jsonl
{"query": "why did we split the auth middleware", "expect": "session refresh"}
{"query": "os error 11001 on the windows box", "expect": {"session": "4048cd58-..."}}
{"query": "hub upgrade backup", "expect": [{"text": "pre-319d60d"}], "since": "2026-11-01"}
$ agent-history eval queries.jsonl --mode all --recency 30d
eval: 3 queries, k=10, index 38,412 chunks
config            hit@1  recall@10     MRR
hybrid            0.667      1.000   0.778
vector            0.333      0.667   0.444
text              0.667      1.000   0.833
hybrid+rec30d     ...
```

`expect` is a substring of the right hit's text, an object of `text` /
`session` / `agent` / `file` that must all match, or a list of either (any
one counts). A query may carry `cwd`, `agent`, `since` and `until`.
`--details` prints each query's rank per configuration; `--json` the whole
report. The [narrowing options](#narrowing-a-search) (`--subagents`,
`--per-session`, `--role`, `--min-chars`, `--exclude-agent`) apply to every
configuration, so `eval q.jsonl` and `eval q.jsonl --subagents exclude
--per-session 2` side by side show whether a filter earns a place as a
default. Queries run against this machine's index and go nowhere else; on a
hub, run it as the hub user inside a tenant's data home.

## Architecture

The whole tool is about 2,900 lines. The dependency graph is acyclic and
shallow — `config` and `textproc` import nothing internal, everything else
depends downward, and `cli` is the only module aware of all the others.

```
        ┌──────────────────────────────────────────────────────────┐
 ENTRY  │  cli.py (~380)  index │ reindex │ reembed │ search │ show│
        │  serve │ docs │ inject │ strict │ purge │ sources │ doctor│
        └──────┬────────────────────────────┬──────────────────────┘
               │                            │
        ┌──────▼─────────┐  ┌───────────────▼───────────────────────┐
 FLOWS  │ index.py (~250)│  │ search.py (130)   hybrid RRF          │
        │  collect→dedup │  │ show.py (90)      conversation view   │
        │  →refresh docs │  │ viewer.py (252)   loopback web UI     │
        │  →embed→store  │  │ inject.py (191)   prompt-time recall  │
        │  reembed: swap │  │ trigger.py (191)  hook-driven indexing│
        └───┬────────┬───┘  └────┬────────────────────┬─────────────┘
            │        │           │                    │
    ┌───────▼───┐    │           │                    │
 IN │ adapters/ │    │           │                    │
    │ 6 sources │    │           │                    │
    │ Record ×N │    │           │                    │
    └───────────┘    │           │                    │
                ┌────▼───────────▼────────────┐ ┌─────▼───────────┐
 OUT            │      store.py (~520)        │ │  embed.py (102) │
                │ schema, dedup, kNN, BM25,   │ │  Ollama client  │
                │ RRF, the docs delete path   │ └─────┬───────────┘
                └──────────────┬──────────────┘       │
                               │                      │
                     SQLite + sqlite-vec + FTS5   HTTP :11434
                               │                      │
        ┌──────────────────────▼──────────────────────▼───────────┐
 BASE   │  config.py (~340) — every path and env var, imports     │
        │  nothing.  textproc.py (32) — chunk + scrub, pure.      │
        └─────────────────────────────────────────────────────────┘
```

**Adapters** are the extensibility seam. One answers three questions —
`available()`, `roots()`, `records()` — and yields `Record(agent, origin,
session_id, cwd, timestamp, role, text)`. Nothing downstream knows which
agent produced a record, which is why `origin` is a string: a file path for
the JSONL agents, `db:<path>#<table>` for the SQLite-backed ones. Two
optional capabilities extend the seam: `watch_files()` lets an adapter own
its change-detection file set, and `mutable = True` opts a source into
stale-chunk refresh — docs is the only adapter that sets either. The
SQLite-backed adapters open their sources read-only and select named columns
only: agent databases keep OAuth tokens and system prompts in neighbouring
tables and columns, and `SELECT *` is how those would end up in a search
index.

**`config.py`** is the single source of truth for location. No other module
contains a hardcoded path. It also owns the security posture — `0700`
creation, the loopback policy, WSL profile disambiguation.

**`embed.py`** is the only code that touches the network.

### Storage

```sql
chunks(id, agent, origin, session_id, cwd, timestamp, role, text,
       content_hash UNIQUE, machine, session_uid,
       record_id, chunk_ordinal, char_start, char_end)  -- provenance
vec_chunks USING vec0(embedding float[768])   -- rowid ⟷ chunks.id
fts_chunks USING fts5(text, content='chunks') -- BM25, external content
meta(key, value)                              -- model, dimension
sessions(uid PRIMARY KEY, agent, machine, source_session_id, origin,
         first_seen, last_seen, meta)         -- one row per conversation
session_aliases(kind, value, session_uid,
                PRIMARY KEY(kind, value))     -- every other id it has
```

Three decisions carry most of the weight:

- **`content_hash UNIQUE` is the entire incremental strategy.** No
  watermarks, no mtime tracking, no per-source bookkeeping — and it behaves
  identically for append-only JSONL and for SQLite sources.
- **`meta` makes the dimension dynamic** rather than a hardcoded 768, and
  doubles as the model-mismatch detector.
- **`vec_chunks` joins by rowid**, keeping the vector table a pure vector
  store.

### Who does what

A common misreading is that Ollama performs the search. It does not — it
never sees the index. Ollama converts text to vectors, and that is all: once
per chunk at index time, once per query at search time. The
nearest-neighbour search runs inside SQLite, in-process, with no network
involved — and so does the BM25 full-text half of a hybrid query, via FTS5.
That is why `--mode text` keeps working when Ollama is down, and why hybrid
search degrades to it rather than failing.

| | |
|---|---|
| Ollama — text → 768 floats | 94 ms |
| sqlite-vec — kNN over 12,819 vectors | **27 ms** |

`sqlite-vec` is a real vector index, not a BLOB column scanned in Python:
`vec_chunks` is a virtual table with its own packed storage format, and
`MATCH ? AND k = 8` is a kNN operator implemented in C. The accurate
description is an embedded vector database living inside a SQLite file — the
same way SQLite is a real database that happens to be a file rather than a
server.

**The limitation that follows from this:** `sqlite-vec` does *exhaustive*
kNN. There is no HNSW or IVF approximate index, so search cost is linear in
corpus size. At 12,819 vectors that is 27 ms and irrelevant. At a few hundred
thousand it becomes noticeable; at millions you would want a different store.
For a personal transcript index the ceiling is remote, and in exchange there
is no daemon, no server, and no separate process to keep alive.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `AGENT_HISTORY_HOME` | `$XDG_DATA_HOME/agent-history` | Where the index lives |
| `AGENT_HISTORY_MODEL` | `nomic-embed-text` | Ollama embedding model |
| `OLLAMA_URL` | `http://localhost:11434` | Ollama endpoint |
| `AGENT_HISTORY_ALLOW_REMOTE` | – | Set to `1` to permit a non-loopback `OLLAMA_URL` |
| `AGENT_HISTORY_PROJECTS` | every `~/.claude*/projects` + `$CLAUDE_CONFIG_DIR/projects` | Claude Code transcript root; when set, replaces profile discovery on this side (the WSL Windows side is still scanned) |
| `AGENT_HISTORY_CODEX` | `~/.codex/sessions` | Codex rollout root |
| `AGENT_HISTORY_OPENCODE_DB` | `~/.local/share/opencode/opencode.db` | OpenCode database |
| `AGENT_HISTORY_HERMES_DIRS` | `~/.hermes*` | Hermes profile homes, os.pathsep-separated |
| `AGENT_HISTORY_OPENCLAW_DIRS` | `~/.openclaw` | OpenClaw state roots, os.pathsep-separated |
| `AGENT_HISTORY_DOCS_DIRS` | – | Docs trees (prefer `agent-history docs add`) |
| `AGENT_HISTORY_EXTRA_DIRS` | – | Extra transcript roots, os.pathsep-separated |
| `AGENT_HISTORY_NO_WSL` | – | Set to `1` to skip Windows-side transcripts |
| `AGENT_HISTORY_INJECT` | – | `1`/`0` forces injection on/off (else on by default, off under strict) |
| `AGENT_HISTORY_INJECT_MAX_DIST` | `17.0` | Injection cutoff for same-directory chunks (L2 distance) |
| `AGENT_HISTORY_INJECT_CROSS_DIST` | `14.0` | Tighter cutoff for other-directory chunks (near-exact only) |
| `AGENT_HISTORY_INJECT_MAX_AGE` | – | Never inject an excerpt older than this span, e.g. `365d` |
| `AGENT_HISTORY_INJECT_RECENCY` | – | Order injection candidates by relevance x recency with this half-life, e.g. `90d` |
| `AGENT_HISTORY_SUPERSEDE_HINTS` | on | `0` turns off the "possibly superseded" hints everywhere |
| `AGENT_HISTORY_AUTO_BACKUP` | on | `0` skips the automatic backups (see [Backups](#backups-and-integrity-checks)); the operation then runs without one |
| `AGENT_HISTORY_BACKUP_DIR` | `$AGENT_HISTORY_HOME/backups` | Where backups go; each data home (tenant) gets its own folder there |
| `AGENT_HISTORY_BACKUP_KEEP` | `5` | Automatic backups kept per data home; hand-made ones are never pruned |
| `AGENT_HISTORY_STRICT` | – | `1` enables the compliance profile (injection off + hard project-scoped) |
| `AGENT_HISTORY_REDACT` | off (on under strict) | `on` replaces secrets with typed markers at ingest; `off` forces verbatim even under strict. Never applies to `ship` (always redacts) or a hub (always on) |
| `AGENT_HISTORY_MACHINE` | short hostname | This machine's name in a shared index |
| `AGENT_HISTORY_HUB_INGEST` | – | rsync destination for `ship`, e.g. `ahub@host:` |
| `AGENT_HISTORY_HUB_KEY` | – | ssh identity file for uploads |
| `AGENT_HISTORY_REMOTE` | – | `ssh://user@host[:port]` hub to search; merges with local results |
| `AGENT_HISTORY_REMOTE_KEY` | – | ssh identity file for hub searches |
| `AGENT_HISTORY_HUB` | – | `1` makes this install a hub: inbox only, redaction forced |
| `AGENT_HISTORY_INBOX` | – | Directory of `<machine>/*.jsonl` batches (hub) |
| `AGENT_HISTORY_TENANTS` | `~/tenants` | Root of per-tenant data homes (multi-tenant hub, `serve-ssh --tenants`) |
| `AGENT_HISTORY_MCP_TOKENS` | `$AGENT_HISTORY_HOME/mcp-tokens.json` | Hashed bearer tokens for `agent-history mcp` (must be `0600`) |
| `AGENT_HISTORY_MCP_READ_TOKEN` / `_WRITE_TOKEN` | – | Single-tenant alternative to the token file (32+ characters) |
| `AGENT_HISTORY_MCP_TENANT` / `_MACHINE` | – | Tenant and record provenance for the environment tokens |
| `AGENT_HISTORY_MCP_TOKEN` | – | The caller's token for `agent-history mcp --stdio` |

Switching `AGENT_HISTORY_MODEL` calls for **`agent-history reembed`** —
vectors from different models are not comparable, and the tool detects the
mismatch and tells you rather than silently returning nonsense. `reembed`
rebuilds the vectors from the text already in the index, so nothing is lost.

## Platform support

| | status |
|---|---|
| **Linux** | fully supported, CI on Python 3.10–3.13 |
| **macOS** | supported on **Python 3.10**; 3.11–3.13 lack SQLite extension support (see Requirements) |
| **Windows** | supported, CI on Python 3.10–3.13 — with one caveat below |

**On Windows the index is not owner-only.** POSIX mode bits do not exist
there, so where Linux and macOS create `index.db` as `0600` inside a `0700`
directory, Windows falls back to whatever the parent directory's ACL grants.
Given the index contains credentials that appeared in your transcripts, put
`AGENT_HISTORY_HOME` somewhere already protected, or restrict it yourself:

```powershell
icacls "$env:LOCALAPPDATA\agent-history" /inheritance:r /grant:r "$env:USERNAME:(OI)(CI)F"
```

Windows uses `%LOCALAPPDATA%\agent-history` by default rather than
`~/.local/share`, and os.pathsep-separated variables use `;` there rather
than `:` — a colon would split drive letters.

Install and sync with the PowerShell equivalents:

```powershell
.\scripts\install.ps1        # checks Ollama, installs, offers a Scheduled Task
.\scripts\sync-local.ps1 -Check
```

### WSL

When running inside WSL, transcripts written by Claude Code or Codex on the
Windows host are picked up automatically, so one index covers both sides
(for Claude Code, every `.claude*/projects` profile in that Windows account). WSL
cannot tell which Windows account is yours, so a profile is selected only
when exactly one under `/mnt/c/Users` has that agent's transcripts; if
several do, none is chosen and you are told to name one via
`AGENT_HISTORY_EXTRA_DIRS`. Set `AGENT_HISTORY_NO_WSL=1` to opt out entirely.

## Privacy and security

**The index is a credential store.** It holds the full text of every indexed
message, and conversations with a coding agent routinely contain secrets you
pasted or that a tool printed. Auditing a real 12,819-chunk index built from
ordinary daily use turned up live database URIs with passwords, API keys, and
32 distinct `password=` assignments. Assume yours is the same.

The posture that follows:

- **Secret redaction is available, and off by default locally.** On one
  machine the index is often the only place a credential can still be found,
  and recalling it is a feature, so secrets are indexed verbatim unless you
  ask otherwise. With `AGENT_HISTORY_REDACT=on` (implied by `strict` mode),
  recognised secrets are replaced at ingest, before chunking, hashing or
  embedding, with a typed marker such as `[REDACTED:github_token]`, so a hit
  still shows *that* something was there. Covered: private-key blocks,
  Anthropic / OpenAI / GitHub / AWS / Slack / Stripe / Google keys, JWTs,
  `Bearer` tokens, the password in `scheme://user:password@host` URLs, and
  `password=` / `token:` / `api_key=`-style assignments. Matching is
  deliberately conservative (git SHAs, UUIDs, hashes, env-var references and
  prose about passwords are kept), so treat it as a strong reduction, not a
  guarantee. Anything that leaves the machine is redacted regardless of this
  setting: `ship` always redacts, and a [hub](#hub-central-history) enforces
  it again. `doctor` shows the setting
  and the last run's per-class counts.
- `index.db` deserves the same handling as `~/.ssh`. It is created `0600`
  inside a `0700` directory — deliberately, since the default umask would
  otherwise make it world-readable on a shared machine. Never commit it;
  think twice before a synced or cloud-mounted `AGENT_HISTORY_HOME`; there is
  no encryption at rest — full-disk encryption is the answer.
- **Facts follow the same rule.** `facts.db` (`0600`) keeps values as you
  wrote them — often a host or a credential — unless local redaction is on,
  and what is shipped to a hub is redacted, twice. `fact forget` deletes the
  rows (SQLite `secure_delete`, so they do not linger in free pages) and any
  copy still queued for the hub; a copy the hub already holds is deleted
  there.
- **Backups hold everything the stores hold**, at `0700`/`0600` like the
  stores. `purge`, `scrub` and `fact forget` back up first, so what they
  remove survives in that backup (and older ones) until you delete it with
  `agent-history backup delete <name> --yes`; set
  `AGENT_HISTORY_AUTO_BACKUP=0` when the point is erasure.
- **Encryption at rest is the disk's job.** Neither `index.db`, `notes.db`
  nor `facts.db` is encrypted by the tool. On a laptop that means full-disk
  encryption; on a hub holding other people's history it means an encrypted
  volume (LUKS, or the provider's encrypted block storage) for the tenants
  root and the inbox, plus a backup that is itself encrypted. Purging a
  tenant (`purge --tenant`) does not reach backups or `.processed/` batches.
- **Nothing is sent off your machine by default.** A non-loopback
  `OLLAMA_URL` is refused unless you set `AGENT_HISTORY_ALLOW_REMOTE=1`,
  because pointing it elsewhere would stream your entire history there.
- **The viewer binds strictly to 127.0.0.1** and has no flag to change it.
- **SQLite sources are read read-only, named columns only** — the same
  databases hold OAuth tokens and system prompts that must never reach the
  index.
- **Docs trees are opt-in, pruned, and contained** — symbolic links inside a
  configured root may not reach files outside it.
- **Shared machines:** on WSL, a Windows profile is auto-detected only when
  exactly one has transcripts; if several do, none is indexed — pulling a
  colleague's conversations into your index would be worse than indexing
  nothing.

### Cleaning an existing index: `scrub`

Turning redaction on only protects chunks written from then on. When you are
ready to drop stored credentials (move them into a password manager first),
`scrub` finds secrets already stored and rewrites those chunks in place — new text, new
content hash, full-text row swapped, vector re-embedded — keeping ids and
provenance. It reaches orphaned chunks too, whose transcripts are gone and
which could never be re-derived:

```console
$ agent-history scrub          # preview: per-class counts, changes nothing, no Ollama
$ agent-history scrub --yes    # rewrite (re-embeds only the affected chunks)
```

Chunk boundaries are computed on the original text and redaction is applied
within them, so a later `index` produces exactly the scrubbed text and
converges instead of duplicating. A chunk whose redacted text is already
stored is a duplicate and is deleted. Older backups (`index.*.backup.db`)
are not touched — delete or scrub them yourself. On the author's 92,913-chunk
index, the first `scrub` found 615 affected chunks and finished in about a
minute.

## What it is — and isn't

The "agents forget everything" space splits into two philosophies. Tools like
claude-mem observe sessions, compress them with LLM calls into structured
memories, and inject summaries. agent-history makes the opposite trade: **the
original words, kept verbatim, at zero ongoing cost** — no summarization
pass, no token spend, no lossy compression between you and what was actually
said. [Context injection](#context-injection-on-by-default) shows the trade holds
even for the "agent already knows" experience: retrieval alone gets there.

Deliberately out of scope: AI summarization and knowledge graphs. If you want
compressed, curated memory, run a tool that does that — the two approaches
compose fine, and this one guarantees the source material survives.

**The cost of verbatim: stale statements come back too.** Retrieval returns
what was said, including decisions later reversed, and nothing in a
transcript says "this was overturned". A summarising tool can flag that, at
the price of tokens and the original wording; this one cannot in general.
What it does instead is narrower and honest about it: a [time
window](#what-was-true-when-the-time-axis) and recency half-life you choose,
heuristic *possibly superseded* hints for the identifiers that drift most
(hosts, addresses, keys, versions), and curated facts for whatever you decide
should have one current value. A reversed *decision* in prose is still yours
to recognise.

## Roadmap

The original roadmap — adapters for every agent found on this machine — is
complete. What remains is driven by use, and tracked in the
[issues](https://github.com/d3layd/agent-history/issues):

- [x] A **`history_recall` MCP tool**, with `history_show` and an
  append-only `history_record`, served by `agent-history mcp` behind an MCP
  router ([MCP server](#mcp-server-claudeai-and-other-mcp-clients), #28). A
  complement to injection, not a replacement: injection stays the ambient,
  zero-turn default; the tool is the deliberate, inspectable path.
- [x] **Time-aware recall** (#40 phases 1–2, #24): `--since` / `--until` /
  `--as-of`, an opt-in recency half-life, curated valid-time facts, and
  "possibly superseded" hints for drifting identifiers
  ([the time axis](#what-was-true-when-the-time-axis)). Still open from #40:
  an opt-in pass that *proposes* facts from transcripts for review.
- [x] **A number for search quality**: `agent-history eval` scores hit@1,
  recall@k and MRR over your own queries
  ([Measuring search quality](#measuring-search-quality-eval)).
- [ ] **Self-calibrating injection cutoff** so a model swap can't silently
  mis-tune it (#13).
- [ ] **CMMC/CUI hardening** — a strict-mode secret scrub and the rest of the
  gap list toward a regulated deployment (#16).
- [x] A **hub** deployment: many machines ship redacted records to one
  index, per tenant, that is searched over ssh ([Hub](#hub-central-history),
  #25), and served over MCP to claude.ai through a router. Still open from
  #25: remote-aware injection.
- [ ] **Multi-machine** merge / snapshot / restore over encrypted object
  storage (#9).
- [ ] An approximate vector index if a corpus outgrows exhaustive kNN
  (hundreds of thousands of chunks — remote for a personal index).
- [ ] Further agents as they appear — one adapter module each.

Adding an agent means writing a single module in
`src/agent_history/adapters/` that yields `Record`s. The indexer and search
layer don't change.

## Development

```console
$ uv sync --group dev
$ uv run pytest                 # no Ollama required
```

The embedder is stubbed in tests, so the suite is fast and hermetic — it
never touches a real index and never needs a model. CI runs it on Python
3.10–3.13 across Ubuntu, macOS and Windows, which is what keeps the platform
claims honest.

Keeping an installation current:

```console
$ ./scripts/sync-local.sh --check   # report drift, change nothing
$ ./scripts/sync-local.sh           # pull, reinstall what changed, run doctor
```

The checkout, the `uv tool` CLI, and the plugin drift independently and
nothing reconciles them on its own — an out-of-date CLI is easy to miss
because it keeps working.

## Known limitations

- Deleting a transcript leaves its chunks in the index — usually the point
  (see [above](#your-index-outlives-the-transcripts)), but it means `--cwd`
  filters can surface paths that no longer exist.
- The first index of a large corpus is slow — see
  [What it costs](#what-it-costs). It is a one-time cost.
- Search quality is bounded by the embedding model. `nomic-embed-text` is a
  good default; `mxbai-embed-large` is slower and somewhat better. Changing
  models is a `reembed` away and loses nothing; `eval` tells you whether it
  helped on your own questions.
- Facts match a query by its words (no embedder), so a fact only leads a
  search that names its subject. Supersession hints are heuristics over a
  handful of identifier kinds, compared only among the hits of one search.
- A time window narrows the nearest-neighbour candidates after the vector
  scan (up to 4,096 of them); on a very large index, an `--as-of` far in the
  past can return fewer than k hits even though older matches exist.

## Contributing

`main` is protected: changes go through a pull request and CI has to be green
before it can merge.

```console
$ git checkout -b my-change
$ uv run pytest
$ git push -u origin my-change
$ gh pr create
```

Merging a version bump to `main` tags it and publishes a GitHub release
automatically, using that version's `CHANGELOG.md` entry as the notes. Merges
that don't touch the version are silent. Keep `pyproject.toml`, `plugin.json`
and `__init__.py` in agreement — CI fails if they drift.

## Contact

Issues and pull requests are welcome on GitHub. For anything else:
[github@dennislayden.com](mailto:github@dennislayden.com).

## License

MIT
