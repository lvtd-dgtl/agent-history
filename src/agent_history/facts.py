"""Curated facts with valid time — the "current value" layer.

Transcripts record what was said, including things later changed, and the
index keeps them verbatim on purpose. A fact is the other half: an explicit,
curated statement of what is true *now* about one thing — ``lvtd-fleet-1 ·
ssh: linuxuser@45.76.1.5`` — that knows when it became true and what it
replaced. It is what a hand-kept memory file's "moved on <date>, use X now"
lines are, made queryable.

- **Explicit.** Facts are written by a person or an agent (``agent-history
  fact set``, the ``history_fact`` MCP tool). Nothing here reads transcripts,
  summarises, or runs a model.
- **History, not overwrite.** Each ``set`` adds a row; the current value of a
  ``(subject, predicate)`` is the row with the latest ``valid_from`` at or
  before the moment asked about. ``--as-of`` walks back along the same rows,
  and ``retire`` adds a row that says "no longer true".
- **Beside the index, never in it.** Facts live in ``facts.db``; the
  transcript corpus is untouched. Search and context injection put a matching
  current fact ahead of transcript hits, labelled as a fact.
- **Raw here, redacted when shipped.** Values are often credentials or hosts,
  so this machine keeps them as written, while the copy sent to a hub is
  redacted (and redacted again by the hub). ``fact forget`` deletes rows.

Facts reach a hub as ``inbox/<tenant>/<machine>/facts/*.jsonl``, beside session
notes and below the directory the transcript reader looks at.
"""
from __future__ import annotations

import datetime
import json
import os
import re
import sqlite3
import sys
import time
import urllib.parse
import uuid
from collections import Counter
from pathlib import Path

from . import config, redact, timeaxis

FORMAT = 1
FACTS_DIR = "facts"
PROCESSED = ".processed"
FACT_ID = re.compile(r"^fact-[0-9a-f]{20}$")
_MACHINE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

MAX_SUBJECT = 200
MAX_PREDICATE = 120
MAX_VALUE = 2000
MAX_FIELD = 500

AGENT = ROLE = "fact"  # how a fact is labelled among search results
SEARCH_LIMIT = 3      # facts put ahead of transcript hits in one search
INJECT_LIMIT = 3

_TERM = re.compile(r"[a-z0-9]+")
_STOP = frozenset(
    "a an and are as at be but by can could did do does for from had has have "
    "how i if in into is it its me my no not of on or our so than that the "
    "their them then there these they this to too us was we were what when "
    "where which who why will with would you your".split())


class FactError(ValueError):
    """Bad input for a fact."""


# --------------------------------------------------------------- the record

def key(text: str) -> str:
    """How subjects and predicates are compared: case and spacing ignored."""
    return " ".join((text or "").lower().split())


def _clean(value, name: str, limit: int, *, required: bool = True) -> str | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise FactError(f"{name} is required")
        return None
    if not isinstance(value, str):
        raise FactError(f"{name} must be text")
    value = value.strip()
    if len(value) > limit:
        raise FactError(f"{name} is longer than {limit} characters")
    if any(ord(c) < 32 and c not in "\t" for c in value) and name != "value":
        raise FactError(f"{name} must be one line")
    return value


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def build(subject: str, predicate: str, value: str | None, *,
          since: str | None = None, source: str | None = None,
          scope: str | None = None, author: str = "cli",
          now: float | None = None) -> dict:
    """A new fact row. ``value`` None retires the (subject, predicate)."""
    subject = _clean(subject, "subject", MAX_SUBJECT)
    predicate = _clean(predicate, "predicate", MAX_PREDICATE)
    if value is not None:
        value = _clean(value, "value", MAX_VALUE)
    try:
        start = (timeaxis.parse_bound(since, end=False, now=now) if since
                 else (time.time() if now is None else now))
    except timeaxis.TimeError as exc:
        raise FactError(str(exc)) from None
    return {
        "format": FORMAT,
        "fact_id": "fact-" + uuid.uuid4().hex[:20],
        "subject": subject,
        "predicate": predicate,
        "value": value,
        "valid_from": timeaxis.iso(start),
        "created_at": _now_iso(),
        "source": _clean(source, "source", MAX_FIELD, required=False),
        "scope": _clean(scope, "scope", MAX_FIELD, required=False),
        "author": _clean(author, "author", MAX_FIELD, required=False) or "cli",
    }


def parse(line: str) -> dict | None:
    """A fact from one JSONL line, or None for anything malformed."""
    try:
        data = json.loads(line)
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        return None
    fact_id = data.get("fact_id")
    if not isinstance(fact_id, str) or not FACT_ID.match(fact_id):
        return None
    try:
        fact = {
            "format": FORMAT,
            "fact_id": fact_id,
            "subject": _clean(data.get("subject"), "subject", MAX_SUBJECT),
            "predicate": _clean(data.get("predicate"), "predicate", MAX_PREDICATE),
            "value": (None if data.get("value") is None
                      else _clean(data.get("value"), "value", MAX_VALUE)),
            "valid_from": _clean(data.get("valid_from"), "valid_from", 64),
            "created_at": _clean(data.get("created_at"), "created_at", 64),
            "source": _clean(data.get("source"), "source", MAX_FIELD, required=False),
            "scope": _clean(data.get("scope"), "scope", MAX_FIELD, required=False),
            "author": _clean(data.get("author"), "author", MAX_FIELD, required=False),
        }
    except FactError:
        return None
    if timeaxis.instant(fact["valid_from"]) == float("-inf"):
        return None
    return fact


def redacted(fact: dict, counter: Counter | None = None) -> dict:
    """The copy that may leave this machine."""
    counter = counter if counter is not None else Counter()
    out = dict(fact)
    for name in ("subject", "predicate", "value", "source", "scope", "author"):
        if isinstance(out.get(name), str):
            out[name], found = redact.redact(out[name])
            counter.update(found)
    return out


# --------------------------------------------------------------------- store

_SCHEMA = """
CREATE TABLE IF NOT EXISTS facts (
    id            INTEGER PRIMARY KEY,
    fact_id       TEXT NOT NULL UNIQUE,
    subject       TEXT NOT NULL,
    predicate     TEXT NOT NULL,
    subject_key   TEXT NOT NULL,
    predicate_key TEXT NOT NULL,
    value         TEXT,
    valid_from    TEXT NOT NULL,
    valid_epoch   REAL NOT NULL,
    created_at    TEXT NOT NULL,
    machine       TEXT NOT NULL,
    author        TEXT,
    source        TEXT,
    scope         TEXT
);
CREATE INDEX IF NOT EXISTS idx_facts_key ON facts(subject_key, predicate_key);
"""


def db_path() -> Path:
    return config.data_home() / "facts.db"


def open_store(write: bool) -> sqlite3.Connection | None:
    """The facts database; None when reading and there is none yet."""
    path = db_path()
    if not write and not path.exists():
        return None
    if write:
        config.ensure_data_home()
    created = not path.exists()
    db = sqlite3.connect(path) if write else sqlite3.connect(
        f"file:{urllib.parse.quote(str(path))}?mode=ro", uri=True)
    if write:
        db.executescript(_SCHEMA)
        if created:
            config.secure_file(path)
    return db


def insert(db: sqlite3.Connection, fact: dict, machine: str) -> bool:
    """Add one fact; False when that fact id is already stored."""
    cursor = db.execute(
        "INSERT OR IGNORE INTO facts (fact_id, subject, predicate, subject_key, "
        "predicate_key, value, valid_from, valid_epoch, created_at, machine, "
        "author, source, scope) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (fact["fact_id"], fact["subject"], fact["predicate"], key(fact["subject"]),
         key(fact["predicate"]), fact["value"], fact["valid_from"],
         timeaxis.instant(fact["valid_from"]), fact["created_at"], machine,
         fact.get("author"), fact.get("source"), fact.get("scope")))
    return cursor.rowcount > 0


_COLUMNS = ("fact_id, subject, predicate, value, valid_from, created_at, machine, "
            "author, source, scope")


def _row(row: tuple) -> dict:
    names = [c.strip() for c in _COLUMNS.split(",")]
    return dict(zip(names, row))


def rows(subject: str | None = None, predicate: str | None = None,
         db: sqlite3.Connection | None = None) -> list[dict]:
    """Every stored row, optionally for one subject (and predicate)."""
    own = db is None
    db = db or open_store(write=False)
    if db is None:
        return []
    try:
        where, params = [], []
        if subject:
            where.append("subject_key = ?")
            params.append(key(subject))
        if predicate:
            where.append("predicate_key = ?")
            params.append(key(predicate))
        sql = f"SELECT {_COLUMNS} FROM facts"
        if where:
            sql += " WHERE " + " AND ".join(where)
        try:
            found = db.execute(sql + " ORDER BY valid_epoch, created_at, id",
                               params).fetchall()
        except sqlite3.OperationalError:
            return []
        return [_row(r) for r in found]
    finally:
        if own:
            db.close()


# -------------------------------------------------------------- valid time

def _order_key(fact: dict) -> tuple:
    return (timeaxis.instant(fact.get("valid_from")), fact.get("created_at") or "",
            fact.get("fact_id") or "")


def chains(facts: list[dict]) -> dict[tuple[str, str], list[dict]]:
    """Rows grouped by (subject, predicate), oldest first, duplicates of one
    fact id (a local copy and the hub's) collapsed."""
    seen: dict[str, dict] = {}
    for fact in facts:
        seen.setdefault(fact["fact_id"], fact)
    grouped: dict[tuple[str, str], list[dict]] = {}
    for fact in seen.values():
        grouped.setdefault((key(fact["subject"]), key(fact["predicate"])), []).append(fact)
    for chain in grouped.values():
        chain.sort(key=_order_key)
    return grouped


def resolve(chain: list[dict], as_of: float | None = None) -> dict | None:
    """The row in force at ``as_of`` (default now), with ``previous`` (the
    value it replaced) and ``valid_to`` (when it stopped being true, if it
    has). None when nothing was true yet, or the latest row retired it."""
    as_of = time.time() if as_of is None else as_of
    position = None
    for i, fact in enumerate(chain):
        if timeaxis.instant(fact["valid_from"]) <= as_of:
            position = i
    if position is None or chain[position]["value"] is None:
        return None
    fact = dict(chain[position])
    earlier = [f for f in chain[:position] if f["value"] is not None]
    if earlier:
        fact["previous"] = {"value": earlier[-1]["value"],
                            "valid_from": earlier[-1]["valid_from"]}
    if position + 1 < len(chain):
        fact["valid_to"] = chain[position + 1]["valid_from"]
    return fact


def current(facts: list[dict], as_of: float | None = None) -> list[dict]:
    """The facts in force at ``as_of``, one per (subject, predicate)."""
    out = []
    for chain in chains(facts).values():
        fact = resolve(chain, as_of)
        if fact is not None:
            out.append(fact)
    return sorted(out, key=lambda f: (key(f["subject"]), key(f["predicate"])))


def history(facts: list[dict], as_of: float | None = None) -> list[dict]:
    """Every row, oldest first per key, each marked ``current`` when it is
    the one in force at ``as_of``."""
    out = []
    for chain in chains(facts).values():
        live = resolve(chain, as_of)
        for fact in chain:
            out.append({**fact, "current": bool(live and live["fact_id"] == fact["fact_id"])})
    return out


# ------------------------------------------------------- matching a query

def _terms(text: str) -> set[str]:
    return {t for t in _TERM.findall((text or "").lower()) if t not in _STOP}


def match_score(fact: dict, query_terms: set[str], *, strict: bool = False) -> float:
    """How well a query names a fact, from its words alone (no embedder):
    0 when it does not. All of the subject's words in the query is a match;
    in ordinary search, half of them plus a word of the predicate is too.
    ``strict`` (context injection) asks for the whole subject."""
    subject = _terms(fact["subject"])
    predicate = _terms(fact["predicate"])
    if not subject or not query_terms:
        return 0.0
    s_hit = len(subject & query_terms) / len(subject)
    p_hit = (len(predicate & query_terms) / len(predicate)) if predicate else 0.0
    if s_hit < 1.0 and (strict or s_hit < 0.5 or p_hit == 0.0):
        return 0.0
    if strict and len(subject) < 2 and p_hit == 0.0:
        return 0.0  # a one-word subject alone is too easy to hit by accident
    return s_hit + 0.5 * p_hit


def in_scope(fact: dict, cwd: str | None) -> bool:
    """A fact with a scope applies inside that directory tree only."""
    scope = (fact.get("scope") or "").rstrip("/")
    if not scope:
        return True
    here = (cwd or "").rstrip("/")
    return bool(here) and (here == scope or here.startswith(scope + "/"))


def matching(query: str, *, as_of: float | None = None, cwd: str | None = None,
             strict: bool = False, limit: int = SEARCH_LIMIT,
             facts: list[dict] | None = None,
             scoped_only: bool = False) -> list[tuple[float, dict]]:
    """Current facts the query names, best first. ``scoped_only`` keeps only
    facts bound to ``cwd`` (strict mode's injection)."""
    terms = _terms(query)
    if not terms:
        return []
    pool = current(rows() if facts is None else facts, as_of)
    scored = []
    for fact in pool:
        if scoped_only and not fact.get("scope"):
            continue
        if fact.get("scope") and not in_scope(fact, cwd):
            continue
        score = match_score(fact, terms, strict=strict)
        if score > 0:
            scored.append((score, fact))
    scored.sort(key=lambda item: (-item[0], -timeaxis.instant(item[1]["valid_from"])))
    return scored[:limit]


def line(fact: dict) -> str:
    """``subject · predicate: value  (since …; was … before)``."""
    text = f"{fact['subject']} · {fact['predicate']}: {fact['value']}"
    since = timeaxis.local(fact.get("valid_from"), 10)
    extra = [f"since {since}"] if since else []
    if fact.get("valid_to"):
        extra.append(f"until {timeaxis.local(fact['valid_to'], 10)}")
    if fact.get("previous"):
        extra.append(f"was {fact['previous']['value']} before")
    return text + (f"  ({'; '.join(extra)})" if extra else "")


def as_hit(fact: dict) -> dict:
    """A fact in the shape of a search result."""
    return {
        "agent": AGENT, "file": f"fact:{fact['fact_id']}",
        "session_id": fact["fact_id"], "session_uid": None,
        "cwd": fact.get("scope") or "", "timestamp": fact["valid_from"],
        "role": ROLE, "text": line(fact),
        "machine": fact.get("machine") or config.machine_id(),
        "agent_id": None, "agent_type": None, "sidechain": False,
        "distance": None, "score": None, "profile": None,
        "fact": {name: fact.get(name) for name in
                 ("fact_id", "subject", "predicate", "value", "valid_from",
                  "valid_to", "previous", "source", "scope")},
    }


def reconcile(hits: list[dict]) -> list[dict]:
    """Drop fact hits another fact hit in the list replaces: same subject and
    predicate, later ``valid_from`` (a local and a hub copy of one fact, or a
    stale local value the hub has since seen changed)."""
    best: dict[tuple, dict] = {}
    for hit in hits:
        fact = hit.get("fact")
        if hit.get("role") != ROLE or not fact:
            continue
        k = (key(fact["subject"]), key(fact["predicate"]))
        if k not in best or _order_key(fact) > _order_key(best[k]["fact"]):
            best[k] = hit
    keep = {id(h) for h in best.values()}
    return [h for h in hits if h.get("role") != ROLE or id(h) in keep]


# ----------------------------------------------------------- writing facts

def outbox_dir() -> Path:
    return config.data_home() / "facts-outbox"


def write_file(directory: Path, fact: dict) -> Path:
    """One immutable file per fact: a hidden part file renamed into place."""
    directory.mkdir(parents=True, exist_ok=True)
    if not config.WINDOWS:
        directory.chmod(0o700)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    name = f"{stamp}-{fact['fact_id']}.jsonl"
    final, part = directory / name, directory / f".{name}.part"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    with os.fdopen(os.open(part, flags, 0o600), "w", encoding="utf-8") as fh:
        fh.write(json.dumps(fact, ensure_ascii=False) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    if final.exists():
        part.unlink(missing_ok=True)
        raise FactError("fact id collision; retry")
    os.replace(part, final)
    return final


def save(fact: dict) -> str:
    """Keep a fact on this machine and, when a hub is configured, queue its
    redacted copy for the hub and ship the queue. Returns where it went."""
    db = open_store(write=True)
    try:
        insert(db, fact, config.machine_id())
        db.commit()
    finally:
        db.close()
    if not config.hub_ingest():
        return "stored"
    write_file(outbox_dir(), redacted(fact))
    return ("stored; queued for the hub" if ship_outbox() == 0
            else "stored; hub unreachable, queued")


def ship_outbox() -> int:
    """Upload queued facts to ``<ingest>facts/`` through the ship transport."""
    from . import ship

    directory = outbox_dir()
    files = sorted(directory.glob("*.jsonl")) if directory.is_dir() else []
    if not files:
        return 0
    dest = config.hub_ingest()
    if not dest:
        return 1
    dest = dest + ("" if dest.endswith(("/", ":")) else "/") + FACTS_DIR + "/"
    try:
        ok = ship.upload(files, dest, config.hub_key())
    except Exception as exc:  # noqa: BLE001 - a transport must not lose facts
        print(f"facts: upload failed: {exc}", file=sys.stderr)
        ok = False
    if ok:
        for f in files:
            f.unlink(missing_ok=True)
    return 0 if ok else 1


def forget(subject: str, predicate: str | None = None) -> int:
    """Permanently delete every row of a subject (and predicate) here.
    Returns how many rows went. A hub's copies are the hub's to delete."""
    db = open_store(write=False)
    if db is None:
        return 0
    db.close()
    db = open_store(write=True)
    try:
        where, params = ["subject_key = ?"], [key(subject)]
        if predicate:
            where.append("predicate_key = ?")
            params.append(key(predicate))
        cursor = db.execute(f"DELETE FROM facts WHERE {' AND '.join(where)}", params)
        db.commit()
        return cursor.rowcount
    finally:
        db.close()


# ----------------------------------------------------------------- hub side

def _fact_dirs() -> list[tuple[str, Path]]:
    root = config.inbox_dir()
    if not root or not root.is_dir():
        return []
    out = []
    for machine in sorted(root.iterdir()):
        facts_dir = machine / FACTS_DIR
        if (_MACHINE.match(machine.name) and machine.is_dir() and not machine.is_symlink()
                and facts_dir.is_dir() and not facts_dir.is_symlink()):
            out.append((machine.name, facts_dir))
    return out


def _pending() -> list[tuple[str, Path]]:
    out = []
    for machine, directory in _fact_dirs():
        out += [(machine, f) for f in sorted(directory.glob("*.jsonl"))
                if f.is_file() and not f.is_symlink() and not f.name.startswith(".")]
    return out


def inbox_pending() -> bool:
    return bool(_pending())


def index_inbox() -> int:
    """Store facts waiting in the hub inbox, redacted again, and retire each
    file once committed. The machine is the directory, never a field."""
    files = _pending()
    if not files:
        return 0
    db = open_store(write=True)
    added = skipped = 0
    try:
        for machine, path in files:
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for raw in lines:
                fact = parse(raw)
                if fact is None:
                    skipped += bool(raw.strip())
                    continue
                added += insert(db, redacted(fact), machine)
            db.commit()
            _retire(machine, path)
    finally:
        db.close()
    print(f"facts: {added} new fact(s)" + (f", {skipped} malformed line(s) skipped"
                                            if skipped else ""))
    return 0


def _retire(machine: str, path: Path) -> None:
    target = config.inbox_dir() / PROCESSED / machine / FACTS_DIR
    try:
        target.mkdir(parents=True, exist_ok=True)
        destination = target / path.name
        n = 0
        while destination.exists():
            n += 1
            destination = target / f"{path.name}.{n}"
        os.replace(path, destination)
    except OSError as exc:
        print(f"facts: could not retire {machine}/{path.name}: {exc}", file=sys.stderr)


# ---------------------------------------------------------------- commands

def _hub_rows(command: list[str]) -> list[dict] | None:
    """Fact rows from the hub, or None (with one stderr line) when it cannot
    be asked. Search must keep working on a plane; so must this."""
    from . import remote

    try:
        found = remote.query_json(command, require="fact_id")
    except remote.RemoteError as exc:
        print(f"warning: hub unavailable ({exc}); showing this machine's facts only",
              file=sys.stderr)
        return None
    keep = ("fact_id", "subject", "predicate", "value", "valid_from", "created_at",
            "machine", "author", "source", "scope", "tenant")
    out = []
    for row in found:
        if not isinstance(row.get("fact_id"), str) or not FACT_ID.match(row["fact_id"]):
            continue
        if not isinstance(row.get("subject"), str) or not isinstance(row.get("predicate"), str):
            continue
        out.append({k: row.get(k) for k in keep})
    return out


def gather(subject: str | None = None, predicate: str | None = None,
           where: str | None = None, tenant: str | None = None) -> list[dict]:
    """Rows from this machine and, when a hub is configured, from the hub
    too (``where`` pins one side, as for search; a ``tenant`` is the hub's)."""
    local = [] if where == "hub" else rows(subject, predicate)
    if where == "local" or not config.remote_url():
        return local
    command = ["fact", "get", subject] if subject else ["fact", "list"]
    if subject and predicate:
        command.append(predicate)
    if tenant:
        command += ["--tenant", tenant]
    hub = _hub_rows([*command, "--history", "--json"])
    return local + (hub or [])


def _json_row(fact: dict, *, current_flag: bool | None = None) -> dict:
    out = {k: v for k, v in fact.items() if k != "current"}
    if current_flag is not None:
        out["current"] = current_flag
    out["text"] = line(fact) if fact.get("value") is not None else (
        f"{fact['subject']} · {fact['predicate']}: (retired)")
    return out


def print_history(found: list[dict], as_of: float | None) -> None:
    for (_s, _p), chain in sorted(chains(found).items()):
        live = resolve(chain, as_of)
        print(f"{chain[0]['subject']} · {chain[0]['predicate']}")
        for fact in chain:
            mark = "*" if live and live["fact_id"] == fact["fact_id"] else " "
            value = fact["value"] if fact["value"] is not None else "(retired)"
            origin = fact.get("machine") or ""
            if fact.get("tenant"):
                origin += f" [{fact['tenant']}]"
            print(f"  {mark} {timeaxis.local(fact['valid_from'])}  {value}  "
                  f"({origin}{', ' + fact['source'] if fact.get('source') else ''})"
                  f"  {fact['fact_id']}")


def run(args) -> int:
    """`agent-history fact ...`."""
    action = args.fact_command
    try:
        if action in ("set", "retire"):
            fact = build(args.subject, args.predicate,
                         args.value if action == "set" else None,
                         since=args.since, source=args.source,
                         scope=getattr(args, "scope", None))
            where = save(fact)
            verb = "set" if action == "set" else "retired"
            print(f"{verb} {fact['subject']} · {fact['predicate']}"
                  + (f" = {fact['value']}" if fact["value"] is not None else "")
                  + f"  (valid from {timeaxis.local(fact['valid_from'])}; {where})"
                  + f"  {fact['fact_id']}")
            return 0
        if action == "ship":
            return ship_outbox()
        if action == "index":
            return index_inbox()
        if action == "forget":
            found = rows(args.subject, args.predicate)
            if not args.yes:
                print(f"would delete {len(found)} fact row(s) for {args.subject!r} on this "
                      "machine. this is permanent; re-run with --yes to do it.")
                return 0
            removed = forget(args.subject, args.predicate)
            print(f"deleted {removed} fact row(s) on this machine.")
            if config.hub_ingest() or config.remote_url():
                print("the hub keeps its own (redacted) copies; delete them there with "
                      "`agent-history fact forget` as the hub user.")
            return 0
    except FactError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    tenant = getattr(args, "tenant", None)
    if tenant and (getattr(args, "where", None) == "local" or not config.remote_url()):
        print("error: --tenant selects facts on a hub; set AGENT_HISTORY_REMOTE",
              file=sys.stderr)
        return 1
    found = gather(getattr(args, "subject", None), getattr(args, "predicate", None),
                   getattr(args, "where", None), tenant)
    return render(args, found)


def render(args, found: list[dict]) -> int:
    """Print ``fact get`` / ``fact list`` output for rows already gathered."""
    as_of = None
    if getattr(args, "as_of", None):
        try:
            as_of = timeaxis.parse_bound(args.as_of, end=True)
        except timeaxis.TimeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    if args.history:
        if args.as_json:
            print(json.dumps([_json_row(f, current_flag=f["current"])
                              for f in history(found, as_of)], indent=2,
                             ensure_ascii=False))
            return 0
        if not found:
            print("(no facts)")
            return 0
        print_history(found, as_of)
        return 0
    live = current(found, as_of)
    if args.as_json:
        print(json.dumps([_json_row(f) for f in live], indent=2, ensure_ascii=False))
        return 0
    if not live:
        print("(no facts in force)" if found else "(no facts)")
        return 0
    for fact in live:
        print(line(fact))
    return 0
