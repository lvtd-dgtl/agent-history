"""`agent-history eval` — measure retrieval against queries with known answers.

Search quality is otherwise a matter of impression. This scores the same
``search_rows`` every other surface uses against a file of questions whose
answers you know are in the index, and reports three numbers per mode:

- **hit@1**: the share of queries whose first result is a right one;
- **recall@k**: the share with a right result anywhere in the top k;
- **MRR**: the mean of 1/rank of the first right result (0 when missing).

The file is JSONL, one query per line; blank lines and ``#`` lines are skipped::

    {"query": "why did we split the auth middleware", "expect": "session refresh"}
    {"query": "fleet ssh login", "expect": {"session": "4048cd58-..."}}
    {"query": "hub upgrade backup", "expect": [{"text": "pre-319d60d"},
                                              {"session": "a1b2"}],
     "cwd": "agent-history", "since": "2026-10-01"}

``expect`` is a string (a case-insensitive substring of the hit's text), an
object, or a list of either (any one may match). An object's fields must all
match: ``text`` (substring), ``session`` (the session id or uid), ``agent``,
``file`` (the origin path or its tail). Optional per-query ``cwd``, ``agent``,
``since``, ``until`` narrow the search exactly as the CLI flags do.

Nothing is sent anywhere: the queries run against this machine's index.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from . import embed, search, store, timeaxis


class EvalError(ValueError):
    """The queries file cannot be used."""


def load(path: str | Path) -> list[dict]:
    """Queries from a JSONL file, validated."""
    try:
        lines = Path(path).expanduser().read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise EvalError(f"cannot read {path}: {exc}") from None
    out = []
    for number, raw in enumerate(lines, 1):
        raw = raw.strip()
        if not raw or raw.startswith("#"):
            continue
        try:
            item = json.loads(raw)
        except ValueError as exc:
            raise EvalError(f"line {number}: not JSON ({exc})") from None
        if not isinstance(item, dict) or not isinstance(item.get("query"), str) \
                or not item["query"].strip():
            raise EvalError(f"line {number}: needs a non-empty \"query\"")
        expect = item.get("expect")
        wanted = expect if isinstance(expect, list) else [expect]
        if not wanted or not all(_valid_expectation(e) for e in wanted):
            raise EvalError(f"line {number}: \"expect\" must be a string, an object "
                            "with text/session/agent/file, or a list of those")
        try:
            window = timeaxis.window(item.get("since"), item.get("until"))
        except timeaxis.TimeError as exc:
            raise EvalError(f"line {number}: {exc}") from None
        out.append({"line": number, "query": item["query"].strip(),
                    "expect": [e if isinstance(e, dict) else {"text": e} for e in wanted],
                    "cwd": item.get("cwd") or None, "agent": item.get("agent") or None,
                    "window": window})
    if not out:
        raise EvalError(f"{path}: no queries")
    return out


_FIELDS = ("text", "session", "agent", "file")


def _valid_expectation(value) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    return (isinstance(value, dict) and value
            and all(k in _FIELDS and isinstance(v, str) and v.strip()
                    for k, v in value.items()))


def matches(hit: dict, expectation: dict) -> bool:
    """Whether one result satisfies every field of one expectation."""
    for field, wanted in expectation.items():
        if field == "text":
            if wanted.lower() not in (hit.get("text") or "").lower():
                return False
        elif field == "session":
            if wanted not in (hit.get("session_id"), hit.get("session_uid")):
                return False
        elif field == "agent":
            if hit.get("agent") != wanted:
                return False
        elif field == "file":
            origin = hit.get("file") or ""
            if origin != wanted and not origin.endswith(wanted):
                return False
    return True


def first_rank(hits: list[dict], expect: list[dict]) -> int | None:
    """1-based rank of the first hit matching any expectation, or None."""
    for rank, hit in enumerate(hits, 1):
        if any(matches(hit, e) for e in expect):
            return rank
    return None


def score(ranks: list[int | None], k: int) -> dict:
    n = len(ranks)
    found = [r for r in ranks if r is not None and r <= k]
    return {
        "queries": n,
        "hit_at_1": round(sum(1 for r in found if r == 1) / n, 4) if n else 0.0,
        "recall_at_k": round(len(found) / n, 4) if n else 0.0,
        "mrr": round(sum(1.0 / r for r in found) / n, 4) if n else 0.0,
    }


def evaluate(queries: list[dict], *, k: int, mode: str,
             recency: float | None = None) -> dict:
    """Run every query in one configuration and score it."""
    ranks: list[int | None] = []
    errors = 0
    for q in queries:
        try:
            hits = search.search_rows(q["query"], k=k, cwd=q["cwd"], agent=q["agent"],
                                      mode=mode, window=q["window"], recency=recency)
        except embed.EmbedError:
            errors += 1
            ranks.append(None)
            continue
        ranks.append(first_rank(hits, q["expect"]))
    return {"mode": mode, "recency": recency, **score(ranks, k),
            "errors": errors, "ranks": ranks}


def run(path: str, *, k: int = 10, mode: str = "hybrid", recency: str | None = None,
        as_json: bool = False, details: bool = False) -> int:
    try:
        queries = load(path)
        half_life = timeaxis.parse_span(recency) if recency else None
    except (EvalError, timeaxis.TimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if k < 1:
        print("error: -k must be at least 1", file=sys.stderr)
        return 1
    try:
        chunks = store.stats(store.open_for_read(check_model=False)).get("total")
    except store.StoreError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    modes = list(search.MODES) if mode == "all" else [mode]
    configs = [(m, None) for m in modes]
    if half_life is not None:
        configs += [(m, half_life) for m in modes]
    results = [evaluate(queries, k=k, mode=m, recency=r) for m, r in configs]

    if as_json:
        print(json.dumps({"k": k, "queries": len(queries), "chunks": chunks,
                          "results": results}, indent=2))
        return 0

    print(f"eval: {len(queries)} quer{'y' if len(queries) == 1 else 'ies'}, "
          f"k={k}, index {chunks:,} chunks" if isinstance(chunks, int)
          else f"eval: {len(queries)} queries, k={k}")
    label_width = max(len(_label(r, recency)) for r in results)
    print(f"{'config':<{label_width}}  {'hit@1':>6}  {f'recall@{k}':>9}  {'MRR':>6}")
    for r in results:
        note = f"  ({r['errors']} embed error(s))" if r["errors"] else ""
        print(f"{_label(r, recency):<{label_width}}  {r['hit_at_1']:>6.3f}  "
              f"{r['recall_at_k']:>9.3f}  {r['mrr']:>6.3f}{note}")
    if details:
        print()
        for i, q in enumerate(queries):
            cells = "  ".join(f"{_label(r, recency)}={r['ranks'][i] or '-'}"
                              for r in results)
            print(f"line {q['line']:>3}  {cells}  {q['query'][:70]}")
    return 0


def _label(result: dict, recency: str | None) -> str:
    return result["mode"] + (f"+rec{recency}" if result["recency"] is not None else "")
