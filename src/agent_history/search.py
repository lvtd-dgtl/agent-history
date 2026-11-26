"""Search over the index.

Three modes:

- hybrid (default): vector similarity and BM25 full-text, merged with
  reciprocal rank fusion. Embeddings find "what did we decide about auth";
  full-text finds exact identifiers ("os error 11001", a function name) that
  embed poorly. Fusion means each covers the other's blind spot.
- vector: embeddings only — the pre-0.6 behaviour.
- text: full-text only. Needs no Ollama, so it also serves as the fallback
  when the embedding endpoint is down.

With `AGENT_HISTORY_REMOTE` set, results can also come from a hub: merged with
the local ones when both exist, or hub-only when there is no local index.

The time axis (all opt-in; default ranking is unchanged):

- a window (``--since`` / ``--until`` / ``--as-of``) keeps only chunks
  inside it, so ``--as-of <date>`` asks the past as it stood then;
- ``--recency <half-life>`` multiplies each hit's relevance by
  0.5^(age / half-life), so "current state of X" leans to recent statements;
- curated facts (``facts``) the query names come first, as the value in force
  at the end of the window;
- hits that a newer hit appears to replace carry a ``superseded_hint``
  (``supersede``).
"""
from __future__ import annotations

import json
import sys
from collections import Counter

import time

from . import config, embed, facts, remote, store, supersede, timeaxis
from .adapters.claude_code import AGENT as CLAUDE_AGENT, profile_of

PREVIEW_CHARS = 600
JSON_PREVIEW_CHARS = 500

MODES = ("hybrid", "vector", "text")
SORTS = ("relevance", "newest")

# Scores within a rounding bucket are treated as tied, and ties go to the
# newer chunk — so superseded information only outranks its correction when
# relevance genuinely separates them. Deliberately mild: no decay curves.
SCORE_TIE_DECIMALS = 4      # RRF scores (~0.016-0.033)
DISTANCE_TIE_DECIMALS = 1   # L2 distances (~14-20)


def search_rows(
    query: str,
    *,
    k: int = 8,
    cwd: str | None = None,
    agent: str | None = None,
    mode: str = "hybrid",
    sort: str = "relevance",
    machine: str | None = None,
    exclude_machine: list[str] | None = None,
    window: timeaxis.Window | None = None,
    recency: float | None = None,
    include_facts: bool = True,
    roles: list[str] | None = None,
    exclude_agent: list[str] | None = None,
    subagents: str = "include",
    min_chars: int = 0,
    per_session: int | None = None,
) -> list[dict]:
    """Run one search and return result dicts — shared by the CLI and viewer.

    ``window`` narrows to a time range; ``recency`` is a half-life in seconds
    (None: off). Matching curated facts lead the list unless
    ``include_facts`` is False; ``agent="fact"`` returns facts alone.
    ``agent`` may be a comma-separated list; ``exclude_agent``, ``roles``,
    ``subagents`` and ``min_chars`` narrow further, and ``per_session`` keeps
    at most that many hits from any one conversation.
    """
    window = window or timeaxis.Window()
    filters = make_filters(roles=roles, exclude_agent=exclude_agent,
                           subagents=subagents, min_chars=min_chars)
    if per_session is not None and per_session < 1:
        raise ValueError("per_session must be at least 1")
    agents = store.agent_set(agent)
    # Curated facts are exempt from --min-chars: a fact is short by design
    # ("hub ssh: dennis@10.0.0.5"), and the floor is there to drop one-line
    # prompts, not the answer itself.
    wants_facts = (include_facts and (not agents or facts.AGENT in agents)
                   and facts.AGENT not in filters.exclude_agents
                   and (not filters.roles or facts.ROLE in filters.roles)
                   and filters.subagents != "only")
    fact_hits = (_fact_hits(query, k=k, cwd=cwd, machine=machine,
                            exclude_machine=exclude_machine, window=window,
                            sort=sort) if wants_facts else [])
    if agents == {facts.AGENT}:
        return fact_hits[:k]
    if fact_hits and not config.db_path().exists():
        # Facts live beside the index, not in it; before the first
        # `agent-history index` they still answer. With no matching fact the
        # store's "no index" error below says what to do.
        print("note: no transcript index yet; showing curated facts only",
              file=sys.stderr)
        return fact_hits[:k]
    # "a,b" becomes a clean list; "," or " " names no agent at all.
    agent = ",".join(sorted(agents - {facts.AGENT})) or None

    db = store.open_for_read(check_model=(mode != "text"))
    narrowing = {"cwd": cwd, "agent": agent, "machine": machine,
                 "exclude_machine": exclude_machine, "window": window,
                 "filters": filters}
    fetch = _fetcher(db, query, mode, narrowing)
    reweigh = recency is not None and sort == "relevance"
    anchor = window.until if window.until is not None else time.time()
    # A recent hit must be fetched before the half-life can lift it.
    pool = max(k * 4, 40) if reweigh else k

    while True:
        rows = fetch(pool)
        ranked = _order([_as_dict(row) for row in rows], "relevance")
        if reweigh:
            ranked = apply_recency(ranked, recency, anchor)
        if not per_session:
            break
        ranked = cap_per_session(ranked, per_session)
        # The cap is chosen in relevance order and refilled from deeper
        # candidates only when it removed something, so a cap that never
        # binds returns exactly the uncapped list.
        if len(ranked) >= k or len(rows) < pool or pool >= _POOL_MAX:
            break
        pool = min(pool * 4, _POOL_MAX)

    results = ranked[:k]
    if sort != "relevance":
        results = _order(results, sort)
    # Facts ride above the k transcript hits rather than taking their places.
    return fact_hits + results


# How deep a per-session cap may look for more conversations: sqlite-vec's
# limit on one nearest-neighbour query.
_POOL_MAX = 4096


def _fetcher(db, query: str, mode: str, narrowing: dict):
    """A function returning the ``n`` best rows for this query and mode.

    The query is embedded once, here, so fetching deeper costs only the
    index lookup. Hybrid falls back to full text, with one note, when the
    embedding endpoint is down.
    """
    if mode == "text":
        return lambda n: [(*r, None, None) for r in store.text_query(db, query, n, **narrowing)]
    if mode == "vector" or not store.fts_enabled(db):
        # An index last written by an older version has no FTS table yet;
        # it appears on the next `agent-history index`.
        vector = embed.pack(embed.embed(query, timeout=60))
        return lambda n: [(*r, None) for r in store.query(db, vector, n, **narrowing)]
    try:
        vector = embed.pack(embed.embed(query, timeout=60))
    except embed.EmbedError as exc:
        print(f"note: embedding unavailable ({exc}); "
              "full-text results only", file=sys.stderr)
        return lambda n: [(*r, None, None) for r in store.text_query(db, query, n, **narrowing)]
    return lambda n: store.hybrid_query(db, vector, query, n, **narrowing)


def make_filters(*, roles: list[str] | None = None,
                 exclude_agent: list[str] | None = None,
                 subagents: str = "include", min_chars: int = 0) -> store.Filters:
    """store.Filters from option values; list options also take commas."""
    def names(values):
        return frozenset(v.strip() for value in values or () for v in value.split(",")
                         if v.strip())
    if subagents not in store.SUBAGENT_MODES:
        raise ValueError(f"subagents must be one of {', '.join(store.SUBAGENT_MODES)}")
    return store.Filters(exclude_agents=names(exclude_agent), roles=names(roles),
                         subagents=subagents, min_chars=max(0, int(min_chars or 0)))


def _session_key(result: dict) -> tuple:
    return (result.get("tenant") or "", result.get("machine") or "",
            result.get("session_uid") or result.get("session_id") or result.get("file") or "")


def cap_per_session(results: list[dict], n: int) -> list[dict]:
    """Keep at most ``n`` hits per conversation, in order; facts are exempt.
    One long session otherwise takes most of a result list."""
    if n < 1:
        raise ValueError("per_session must be at least 1")
    seen: Counter = Counter()
    out = []
    for result in results:
        if result.get("role") != facts.ROLE:
            key = _session_key(result)
            if seen[key] >= n:
                continue
            seen[key] += 1
        out.append(result)
    return out


def _fact_hits(query: str, *, k: int, cwd: str | None, machine: str | None,
               exclude_machine: list[str] | None, window: timeaxis.Window,
               sort: str) -> list[dict]:
    """Curated facts the query names, in force at the end of the window.

    Facts are global unless scoped; a ``--cwd`` narrowing keeps global facts
    and facts scoped under a matching directory. Only relevance searches get
    them first — a "newest" listing is about the conversation timeline.
    """
    if sort != "relevance":
        return []
    try:
        pool = facts.rows()
    except Exception:  # noqa: BLE001 - a broken facts file must not break search
        return []
    if not pool:
        return []
    local = config.machine_id()
    if machine:
        pool = [f for f in pool if (f.get("machine") or local) == machine]
    if exclude_machine:
        skip = set(exclude_machine)
        pool = [f for f in pool if (f.get("machine") or local) not in skip]
    if cwd:
        # --cwd is a substring of a directory, as for chunks: global facts
        # stay, scoped ones must have it in their scope.
        pool = [f for f in pool if not f.get("scope") or cwd in f["scope"]]
    found = facts.matching(query, as_of=window.until, facts=pool,
                           limit=min(k, facts.SEARCH_LIMIT), any_scope=True)
    return [facts.as_hit(fact) for _score, fact in found]


def apply_recency(results: list[dict], half_life: float, anchor: float) -> list[dict]:
    """Re-rank by relevance x 0.5^(age / half-life).

    Relevance is the fused score where there is one, else a rank-derived
    1/(60+rank) from the order the results arrived in (vector or BM25 order),
    so every mode can be reweighted. Each result keeps that ``relevance`` and
    its ``recency`` factor; ``score`` becomes their product. An undated chunk
    weighs nothing. Ties keep the newer chunk first.
    """
    weighted = []
    for rank, result in enumerate(results):
        base = result["score"] if result.get("score") is not None else 1.0 / (60 + rank)
        age = anchor - timeaxis.instant(result.get("timestamp"))
        factor = timeaxis.decay(age, half_life)
        weighted.append({**result, "relevance": base, "recency": round(factor, 4),
                         "score": base * factor})
    newest_first = sorted(weighted, key=lambda r: timeaxis.instant(r.get("timestamp")),
                          reverse=True)
    return sorted(newest_first, key=lambda r: -r["score"])


def _order(results: list[dict], sort: str) -> list[dict]:
    """Order results: newest-first, or relevance with a recency tiebreak.

    Timestamps are normalised before comparison: adapters store a mix of
    UTC-suffixed and local-naive ISO strings, and comparing those
    lexicographically can misorder by the UTC offset. Naive values are
    taken as local time (that is how they were generated); unparseable or
    empty ones sort last.
    """
    def when(r: dict) -> float:
        return _instant(r.get("timestamp") or "")

    if sort == "newest":
        return sorted(results, key=when, reverse=True)

    if not any(r.get("score") is not None or r.get("distance") is not None
               for r in results):
        return results  # pure text mode: BM25 order is the relevance order

    def rank(r: dict):
        if r.get("score") is not None:
            return -round(r["score"], SCORE_TIE_DECIMALS)
        return round(r["distance"], DISTANCE_TIE_DECIMALS)

    # Two-pass stable sort: newest-first first, then by bucketed relevance —
    # equal buckets keep the newer row in front.
    newest_first = sorted(results, key=when, reverse=True)
    return sorted(newest_first, key=rank)


def _instant(timestamp: str) -> float:
    """A comparable epoch for an ISO timestamp of any stored flavour."""
    return timeaxis.instant(timestamp)


def time_options(since: str | None = None, until: str | None = None,
                 as_of: str | None = None, recency: str | None = None,
                 ) -> tuple[timeaxis.Window, float | None]:
    """The window and half-life (seconds) for user-supplied strings.
    Raises timeaxis.TimeError with a message fit to print."""
    window = timeaxis.window(since, until, as_of)
    half_life = timeaxis.parse_span(recency) if recency else None
    return window, half_life


def run(
    query: str,
    *,
    k: int = 8,
    cwd: str | None = None,
    agent: str | None = None,
    machine: str | None = None,
    exclude_machine: list[str] | None = None,
    mode: str = "hybrid",
    sort: str = "relevance",
    full: bool = False,
    as_json: bool = False,
    where: str | None = None,
    tenant: str | None = None,
    window: timeaxis.Window | None = None,
    recency: float | None = None,
    roles: list[str] | None = None,
    exclude_agent: list[str] | None = None,
    subagents: str = "include",
    min_chars: int = 0,
    per_session: int | None = None,
) -> int:
    """``where`` is None (decide), "local", or "hub"."""
    side = _side(where, machine)
    if side is None:
        return 1
    if tenant and side == "local":
        print("error: --tenant selects an index on a hub; set "
              "AGENT_HISTORY_REMOTE", file=sys.stderr)
        return 1
    agent = ",".join(sorted(store.agent_set(agent))) or None  # "," names none
    options = dict(k=k, cwd=cwd, agent=agent, machine=machine,
                   exclude_machine=exclude_machine, mode=mode, sort=sort,
                   window=window or timeaxis.Window(), recency=recency,
                   roles=roles, exclude_agent=exclude_agent, subagents=subagents,
                   min_chars=min_chars, per_session=per_session)

    if side == "hub":
        if len(store.agent_set(agent)) < 2:
            return remote.passthrough(remote.search_args(
                query, full=full, as_json=as_json, tenant=tenant, **options))
        # An agent list is asked one agent at a time (see _hub_results), so
        # the hub's answers are merged and printed here.
        try:
            hub = _hub_results(query, full=full, tenant=tenant, **options)
        except remote.RemoteError as exc:
            print(f"error: hub search failed: {exc}", file=sys.stderr)
            return 1
        return emit(hub, full=full, as_json=as_json)

    results = search_rows(query, **options)
    if side == "both":
        results = _merge_hub(query, results, full=full, tenant=tenant, **options)
    return emit(results, full=full, as_json=as_json)


def emit(results: list[dict], *, full: bool, as_json: bool) -> int:
    """Print results as JSON or the human format. A tenant is labelled only
    when the results span more than one — one tenant is just "the index".
    Supersession hints are worked out here, over the final list, so a merged
    local-and-hub result is compared as one list."""
    results = supersede.annotate(results)
    if as_json:
        print(json.dumps([_trim(dict(r), full) for r in results], indent=2))
        return 0

    if not results:
        print("(no matches)")
        return 0

    label = len({r["tenant"] for r in results if r.get("tenant")}) > 1
    for position, result in enumerate(results, 1):
        _print_row(position, result, full, label_tenant=label)
    return 0


def _side(where: str | None, machine: str | None) -> str | None:
    """Which indexes to ask: "local", "hub", or "both". None means an error
    was already printed."""
    configured = config.remote_url()
    if where == "hub" and not configured:
        print("error: --hub needs AGENT_HISTORY_REMOTE=ssh://user@host",
              file=sys.stderr)
        return None
    if where == "local" or not configured:
        return "local"
    if where == "hub" or not config.db_path().exists():
        return "hub"
    # The hub also holds this machine's own history; asking it for that would
    # only return the local results a second time.
    return "local" if machine == config.machine_id() else "both"


def _merge_hub(query: str, local: list[dict], *, full: bool, k: int,
               exclude_machine: list[str] | None, sort: str,
               tenant: str | None = None, **options) -> list[dict]:
    """Interleave hub results into ``local`` by reciprocal-rank fusion.

    The hub is asked to leave out this machine's own chunks, so the two lists
    are disjoint and fusion reduces to interleaving by rank, with the usual
    recency tiebreak between equal ranks. If the hub cannot be reached the
    local results stand, with one line on stderr: search must keep working
    on a plane.
    """
    skip = [*(exclude_machine or ()), config.machine_id()]
    try:
        hub = _hub_results(query, k=k, exclude_machine=skip, sort=sort, full=full,
                           tenant=tenant, **options)
    except remote.RemoteError as exc:
        print(f"warning: hub unavailable ({exc}); showing local results only",
              file=sys.stderr)
        return local

    return fuse([local, hub], sort=sort, k=k, per_session=options.get("per_session"))


def _hub_results(query: str, *, agent: str | None, k: int, sort: str,
                 per_session: int | None = None, **options) -> list[dict]:
    """The hub's JSON answer for this search, as result dicts.

    A hub older than agent lists reads ``--agent a,b`` as one agent named
    "a,b" and matches nothing, without an error. So a list is asked one
    agent at a time, which every hub understands, and the answers are fused
    by rank. Raises remote.RemoteError.
    """
    agents = sorted(store.agent_set(agent)) or [None]
    lists = [[_with_profile({**_HUB_DEFAULTS, **row}) for row in remote.query_json(
                 remote.search_args(query, agent=name, k=k, sort=sort,
                                    per_session=per_session, as_json=True, **options))]
             for name in agents]
    if len(lists) == 1:
        return lists[0]
    return fuse(lists, sort=sort, k=k, per_session=per_session)


def _with_profile(result: dict) -> dict:
    """Fill in the Claude profile for a hub hit. Hubs that predate profile
    tracking do not send it, but the shipped origin is the original path, so
    it can be read here the same way as for a local chunk."""
    if result.get("profile") is None and result.get("agent") == CLAUDE_AGENT:
        result["profile"] = profile_of(result.get("file") or "")
    return result


def fuse(lists: list[list[dict]], *, sort: str, k: int,
         per_session: int | None = None) -> list[dict]:
    """Interleave independently ranked result lists by reciprocal-rank
    fusion: each hit scores 1/(60+rank) in its own list, equal ranks fall to
    the newer chunk, and repeats are dropped."""
    seen: set[tuple] = set()
    fused: list[dict] = []
    for results in lists:
        for rank, result in enumerate(results):
            result = {**result, "score": 1.0 / (60 + rank)}
            key = (result["file"], result["session_id"], result["timestamp"],
                   result["text"][:200], result.get("tenant"))
            if key not in seen:
                seen.add(key)
                fused.append(result)
    fused = facts.reconcile(fused)
    ordered = _order(fused, sort)
    if per_session:
        ordered = cap_per_session(ordered, per_session)
    # Curated facts lead, above the k transcript hits, as in each list alone.
    return ([r for r in ordered if r.get("role") == facts.ROLE]
            + [r for r in ordered if r.get("role") != facts.ROLE][:k])


_HUB_DEFAULTS = {"agent": "?", "file": "", "session_id": "",
                 "session_uid": None, "cwd": "",
                 "timestamp": "", "role": "?", "machine": "hub",
                 "distance": None}


def _as_dict(row: tuple) -> dict:
    (agent, origin, session_id, cwd, timestamp, role, text, machine,
     session_uid, agent_id, agent_type, sidechain, distance, score) = row
    return {
        "agent": agent,
        "file": origin,
        "session_id": session_id,
        "session_uid": session_uid,
        "cwd": cwd,
        "timestamp": timestamp,
        "role": role,
        "text": text,
        "machine": machine or config.machine_id(),
        "agent_id": agent_id,
        "agent_type": agent_type,
        "sidechain": bool(sidechain),
        "distance": distance,
        "score": score,
        # Which Claude Code config profile (account) the chunk came from;
        # derived from the stored path, so no schema change is involved.
        "profile": profile_of(origin or "") if agent == CLAUDE_AGENT else None,
    }


def _trim(result: dict, full: bool) -> dict:
    if not full:
        result["text"] = result["text"][:JSON_PREVIEW_CHARS]
    return result


def sub_label(row: dict) -> str:
    """` [sub:<type> <last 4 of id>]` for a subagent chunk, else empty."""
    if not row.get("sidechain") and not row.get("agent_id"):
        return ""
    parts = [f"sub:{row['agent_type']}" if row.get("agent_type") else "sub"]
    if row.get("agent_id"):
        parts.append(str(row["agent_id"])[-4:])
    return " [" + " ".join(parts) + "]"


def _print_row(position: int, result: dict, full: bool,
               label_tenant: bool = False) -> None:
    if result.get("role") == facts.ROLE:
        ranking = "fact"
    elif result.get("recency") is not None and result["score"] is not None:
        ranking = f"score={result['score']:.4f} rec×{result['recency']:.2f}"
    elif result["score"] is not None:
        ranking = f"score={result['score']:.4f}"
    elif result["distance"] is not None:
        ranking = f"dist={result['distance']:.2f}"
    else:
        ranking = "text-match"
    timestamp = (result["timestamp"] or "")[:16]
    where = ""
    if result.get("machine") and result["machine"] != config.machine_id():
        where = f"  @{result['machine']}"
    if label_tenant and result.get("tenant"):
        where += f"  [{result['tenant']}]"
    # Tag only non-default Claude profiles, so ~/.claude output reads as before.
    profile = result.get("profile")
    tag = f"[{profile}]" if profile and profile != "claude" else ""
    if result.get("role") == facts.ROLE:
        since = timeaxis.local(result.get("timestamp"), 10)
        print(f"#{position}  fact  since {since}  "
              f"{result.get('cwd') or 'all projects'}{where}")
    else:
        print(
            f"#{position}  {ranking}  {timestamp}  "
            f"{result['agent']}{tag}/{result['role']}{sub_label(result)}  "
            f"{result['cwd'] or '?'}{where}"
        )
    if result.get("role") == facts.ROLE and result.get("fact"):
        print(f"    fact: agent-history fact get {_quoted(result['fact']['subject'])} "
              "--history")
    else:
        print(f"    file: {result['file']}")
    if result.get("superseded_hint"):
        print(f"    \u26a0 {supersede.describe(result['superseded_hint'])}")
    text = result["text"]
    if full:
        print("    ---")
        for line in text.splitlines():
            print(f"    {line}")
    else:
        print(f"    {text[:PREVIEW_CHARS].replace(chr(10), ' ')}")
    print()


def _quoted(text: str) -> str:
    import shlex
    return shlex.quote(text)
