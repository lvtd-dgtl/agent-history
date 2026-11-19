"""Near-real-time sync: new transcript turns -> local index -> hub, in seconds.

``agent-history-notify`` (a Claude Code hook) spools the path of a transcript
that just changed and starts ``agent-history sync`` detached. The syncer:

- is **single-flight** (``.sync.lock``): a second syncer exits at once, and the
  running one re-checks the spool before it quits, so a notify that races its
  exit is never dropped;
- is **debounced**: the first change runs immediately; changes during the next
  ``AGENT_HISTORY_SYNC_INTERVAL`` seconds (default 15) collapse into one
  trailing pass, so a burst costs one run and the last turn is never missed;
- is **incremental**: it reads only the bytes appended to each changed
  transcript since the offset recorded in ``ship.db`` (``files`` table), then
  chunks/embeds just those records and queues them for the hub;
- never loses anything: spool entries are deleted only after a successful pass
  and only if they did not change meanwhile; shipping goes through the same
  durable outbox (a dead hub just leaves batches queued); the offset advances
  only once the records are safely queued *and* indexed (or an index run that
  covers them holds the lock). Offsets are an optimisation: every record is
  still deduplicated by hash, so a lost or stale offset costs a re-read, never
  a duplicate or a gap;
- keeps redaction intact: records go through ``ship.Batches``, which redacts
  unconditionally before anything is written to the outbox.

``sync --sweep`` stat()s every transcript and processes the ones that changed
(cheap), which is what cron runs as the safety net for sessions killed without
a hook.
"""
from __future__ import annotations

import datetime
import os
import time
from pathlib import Path

from . import config, notify, ship, textproc, trigger

DEFAULT_INTERVAL = 15.0
SHIP_LOCK_WAIT = 20      # a cron `ship` may hold it; retried next pass otherwise
INDEX_LOCK_WAIT = 3      # a full index run covers our records anyway
MAX_FAILED_PASSES = 3
LOG_CAP = 1_000_000


def interval() -> float:
    try:
        return max(0.0, float(os.environ.get("AGENT_HISTORY_SYNC_INTERVAL",
                                             DEFAULT_INTERVAL)))
    except ValueError:
        return DEFAULT_INTERVAL


def _flag(name: str, default: bool = True) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "off", "false", "no", "")


def _log(message: str) -> None:
    path = config.sync_log_path()
    try:
        if path.exists() and path.stat().st_size > LOG_CAP:
            os.replace(path, path.with_suffix(".log.1"))
        stamp = datetime.datetime.now().isoformat(timespec="seconds")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"[{stamp}] {message}\n")
        config.secure_file(path)
    except OSError:
        pass


# ------------------------------------------------------------------- spool

def _entries() -> list[Path]:
    try:
        return sorted(p for p in config.dirty_dir().iterdir()
                      if p.is_file() and not p.name.startswith("."))
    except OSError:
        return []


def _fingerprint(entry: Path) -> tuple[int, int] | None:
    try:
        st = entry.stat()
    except OSError:
        return None
    return st.st_ino, st.st_mtime_ns


def _candidates(adapter, sweep: bool, paths: list[Path]) -> list[Path]:
    """Files to look at: everything on a sweep, else the reported transcripts
    (plus their subagent transcripts), restricted to the adapter's roots."""
    roots = adapter.roots()
    out: list[Path] = []
    seen: set[Path] = set()

    def add(path: Path) -> None:
        try:
            key = path.resolve()
        except OSError:
            key = path
        if key not in seen:
            seen.add(key)
            out.append(path)

    if sweep:
        for path in adapter.transcript_files() + adapter.memory_files():
            add(path)
    for path in paths:
        for root in roots:
            try:
                rel = path.resolve().relative_to(root.resolve())
            except (ValueError, OSError):
                continue
            mapped = root / rel          # same spelling the adapter uses
            add(mapped)
            sub = mapped.with_suffix("")  # <session>/subagents/*.jsonl
            if mapped.suffix == ".jsonl" and sub.is_dir():
                for child in sub.rglob("*.jsonl"):
                    add(child)
            break
    return out


# -------------------------------------------------------------------- pass

def sync_files(files: list[Path], *, adapter=None, do_ship: bool | None = None,
               do_index: bool | None = None) -> bool:
    """Process ``files`` once. True when everything read is safely handled."""
    from .adapters.claude_code import ClaudeCodeAdapter

    adapter = adapter or ClaudeCodeAdapter()
    adapter._bridges = adapter.session_bridges()
    dest = config.hub_ingest()
    if do_ship is None:
        do_ship = bool(dest) and not config.hub_mode()
    if do_index is None:
        do_index = _flag("AGENT_HISTORY_SYNC_INDEX") and config.db_path().exists()

    config.ensure_data_home()
    state = ship.State.open(writable=True)
    try:
        work = []   # (path, size, mtime_ns, end offset, bridges)
        records = []
        for path in files:
            try:
                st = path.stat()
            except OSError:
                continue
            row = state.get_file(str(path.resolve()))
            if row and row[0] == st.st_size and row[1] == st.st_mtime_ns:
                continue
            if path.suffix == ".md":
                found, end, bridges = list(adapter.read_memory(path)), \
                    st.st_size, {}
            else:
                offset = row[2] if row else 0
                if st.st_size < offset:   # truncated or replaced: start over
                    offset = 0
                found, end, bridges = adapter.read_tail(
                    path, offset, row[3] if row else None)
            work.append((path, st.st_size, st.st_mtime_ns, end, bridges))
            records += [r for r in found if not textproc.is_injection(r.text)]
        if not work:
            return True

        shipped = new = 0
        if do_ship:
            with trigger._exclusive(config.ship_lock_path(),
                                    timeout=SHIP_LOCK_WAIT) as got:
                if not got:
                    _log("ship lock busy; will retry")
                    return False
                config.secure_file(config.ship_lock_path())
                outbox = ship._outbox()
                for stale in outbox.glob(".*.part"):
                    stale.unlink(missing_ok=True)
                state.settle(outbox)
                ship.migrate_portable(state)    # once; a no-op afterwards
                batches = ship.Batches(state, outbox, False)
                for r in records:
                    batches.add(agent=r.agent, origin=r.origin,
                                session_id=r.session_id, cwd=r.cwd,
                                timestamp=r.timestamp, role=r.role, text=r.text,
                                aliases=r.aliases, agent_id=r.agent_id,
                                agent_type=r.agent_type, sidechain=r.sidechain,
                                fork_of=r.fork_of)
                batches.flush()
                new = batches.new
                ok, shipped, left = ship.deliver(state, outbox, dest)
                if not ok or left:
                    _log(f"hub unreachable; {left} batch(es) queued in outbox")

        index_ok, chunks = True, 0
        if do_index and records:
            with trigger._exclusive(config.lock_path(),
                                    timeout=INDEX_LOCK_WAIT) as got:
                if got:
                    try:
                        from . import index
                        chunks, failures = index.run_records(records)
                        index_ok = failures == 0
                    except Exception as exc:  # noqa: BLE001
                        _log(f"index failed: {exc}")
                        index_ok = False
                # not got: a full index run holds it; its stamp-based
                # --if-changed pass picks up anything newer than its start.

        if index_ok:
            for path, size, mtime_ns, end, bridges in work:
                state.set_file(str(path.resolve()), size, mtime_ns, end, bridges)
            state.db.commit()
        _log(f"{len(work)} file(s), {len(records)} record(s): "
             f"{new} new to ship, {chunks} chunk(s) indexed"
             + ("" if index_ok else "; index incomplete, will re-read"))
        return True
    finally:
        state.close()


def _pass(entries: list[Path]) -> bool:
    from .adapters.claude_code import ClaudeCodeAdapter

    prints = {e: _fingerprint(e) for e in entries}
    sweep = False
    paths: list[Path] = []
    for entry in entries:
        try:
            text = entry.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if text == "*" or entry.name == notify.SWEEP:
            sweep = True
        elif text:
            paths.append(Path(text))
    adapter = ClaudeCodeAdapter()
    ok = sync_files(_candidates(adapter, sweep, paths), adapter=adapter)
    if ok:
        for entry, fp in prints.items():
            if _fingerprint(entry) == fp:   # changed since read: keep it
                entry.unlink(missing_ok=True)
    return ok


# ------------------------------------------------------------------ runner

def _last_start() -> float:
    try:
        return config.sync_stamp_path().stat().st_mtime
    except OSError:
        return 0.0


def _serve(once: bool, gap: float) -> int:
    failures = 0
    linger_until = time.time()
    while True:
        entries = _entries()
        if entries:
            wait = gap - (time.time() - _last_start())
            if wait > 0 and not once:
                time.sleep(wait)       # let a burst finish; one trailing pass
                entries = _entries()
            config.sync_stamp_path().touch()
            try:
                ok = _pass(entries)
            except Exception as exc:  # noqa: BLE001 - never crash a hook chain
                _log(f"pass failed: {exc}")
                ok = False
            if once:
                return 0 if ok else 1
            if not ok:
                failures += 1
                if failures >= MAX_FAILED_PASSES:
                    return 1
                time.sleep(max(gap, 1.0))
                continue
            failures = 0
            linger_until = time.time() + gap
        else:
            if once or time.time() >= linger_until:
                return 0
            time.sleep(min(0.25, max(gap / 4, 0.01)))


def run(*, sweep: bool = False, once: bool = False,
        gap: float | None = None) -> int:
    config.ensure_data_home()
    gap = interval() if gap is None else gap
    if sweep:
        notify.spool("*")
    while True:
        with trigger._exclusive(config.sync_lock_path(),
                                timeout=5 if once else 0.05) as got:
            if not got:
                return 0           # the running syncer will see our spool entry
            config.secure_file(config.sync_lock_path())
            code = _serve(once, gap)
        # A notify may have landed after the holder's last look and before it
        # released the lock; its own syncer lost the race and left. Re-check.
        if once or code != 0 or not _entries():
            return code
