"""Backups of a data home, and integrity checks of the live data.

The index is often the only copy of a conversation: agents delete old
transcripts, and a shipper's files leave the machine once the hub has them.
So every store is backed up before anything that could lose rows:

- the first write by a newer version (a schema upgrade, or code that writes
  differently), detected by the version stamp in the data home;
- every command that deletes or rewrites rows: reindex, reembed, purge,
  scrub, relabel, ``notes migrate-records``, ``fact forget``.

A backup is a directory under ``<data home>/backups/`` (or a folder of its
own under ``$AGENT_HISTORY_BACKUP_DIR``) holding a consistent copy of each
SQLite store, made with SQLite's backup API, plus the token file and the
batches not yet shipped. Its ``manifest.json`` records each file's size,
SHA-256, ``PRAGMA integrity_check`` result and per-table row counts. It is
written under a temporary name and renamed into place only when complete,
so a backup that exists is a whole one. A store whose copy fails the check
is kept anyway and flagged: a damaged copy is better than none, and one bad
store must not stop the others being saved.

While a store is copied, other processes can read it but their writes wait;
every writer waits up to ``store.BUSY_TIMEOUT`` for that rather than failing.

``check`` compares the live stores with a backup: corruption, the index's
three views of the chunks (rows, full-text, vectors) disagreeing, and any
table that has fewer rows than the backup recorded.

If a backup cannot be made (no space, unreadable store) the operation that
asked for it does not run: losing the safety net is never silent. Set
``AGENT_HISTORY_AUTO_BACKUP=0`` to run without one, for example to erase data
on purpose; backups keep what a later purge, scrub or ``fact forget``
removes until they are deleted.
"""
from __future__ import annotations

import contextlib
import datetime
import hashlib
import json
import os
import re
import shutil
import socket
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

from . import __version__, config

MANIFEST = "manifest.json"
STAMP = ".written-by"
PARTIAL = ".partial-"
IN_USE = ".in-use"          # in a backup an operation still depends on
IN_USE_HOURS = 24           # after which a pin left by a crash no longer counts
DEFAULT_KEEP = 5            # backups made before an operation, per data home
UPGRADE_KEEP = 2            # backups made before a new version's first write
SPACE_MARGIN = 64 * 2**20   # free space to leave beyond the copy itself
LOCK_TIMEOUT = 600          # seconds to wait for another backup
TOKEN_FILE = "mcp-tokens.json"
OUTBOXES = ("outbox", "facts-outbox", "notes-outbox")
_SQLITE_HEADER = b"SQLite format 3\x00"
_FAILURES = (OSError, sqlite3.Error, shutil.Error)


class BackupError(RuntimeError):
    """A backup could not be made, read or restored."""


class IntegrityError(RuntimeError):
    """The live data failed its check after an operation."""


# ------------------------------------------------------------------- settings

def auto_enabled() -> bool:
    value = (config._env("AGENT_HISTORY_AUTO_BACKUP", "") or "").strip().lower()
    return value not in ("0", "false", "no", "off")


def keep() -> int:
    raw = (config._env("AGENT_HISTORY_BACKUP_KEEP", "") or "").strip()
    try:
        return max(1, int(raw)) if raw else DEFAULT_KEEP
    except ValueError:
        return DEFAULT_KEEP


def root() -> Path:
    """Where this data home's backups go. Under $AGENT_HISTORY_BACKUP_DIR
    (another disk, say) each data home gets a folder named for its full
    path, so two homes — a hub and its tenants — never share one."""
    raw = (config._env("AGENT_HISTORY_BACKUP_DIR", "") or "").strip()
    home = config.data_home()
    if raw:
        tag = hashlib.sha256(str(home.expanduser().resolve()).encode()).hexdigest()[:10]
        return Path(raw).expanduser() / f"{home.name}-{tag}"
    return home / "backups"


# ---------------------------------------------------------------- what to copy

def stores() -> list[Path]:
    """Every SQLite database directly in the data home."""
    home = config.data_home()
    if not home.is_dir():
        return []
    found = []
    for path in sorted(home.glob("*.db")):
        try:
            with open(path, "rb") as fh:
                if fh.read(16) == _SQLITE_HEADER:
                    found.append(path)
        except OSError:
            continue
    return found


def _connect(path: Path, *, readonly: bool = False) -> sqlite3.Connection:
    """A connection with sqlite-vec loaded when it can be, so the virtual
    tables the stores use are understood. Read-only opens never create a
    missing file and never change an existing one."""
    from . import store

    if readonly:
        db = sqlite3.connect(f"file:{quote(str(path))}?mode=ro", uri=True,
                             timeout=store.BUSY_TIMEOUT)
    else:
        db = sqlite3.connect(path, timeout=store.BUSY_TIMEOUT)
    try:
        import sqlite_vec

        db.enable_load_extension(True)
        sqlite_vec.load(db)
        db.enable_load_extension(False)
    except Exception:  # noqa: BLE001 - without the extension, vec tables are skipped
        pass
    return db


# ------------------------------------------------------------------- describe

def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _tables(db: sqlite3.Connection) -> dict[str, int]:
    """Row count of every table that holds data of its own. Virtual tables'
    shadow tables are left out: their row counts follow internal layout
    (full-text segments merge, vectors are stored in blocks), not content."""
    rows = db.execute("SELECT name, coalesce(sql, '') FROM sqlite_master "
                      "WHERE type = 'table' ORDER BY name").fetchall()
    virtual = [name for name, sql in rows if sql.upper().startswith("CREATE VIRTUAL")]
    names = [name for name, sql in rows
             if not sql.upper().startswith("CREATE VIRTUAL")
             and not name.startswith("sqlite_")
             and not any(name.startswith(v + "_") for v in virtual)]
    return {name: db.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0]
            for name in names}


def _integrity(db: sqlite3.Connection, quick: bool = False) -> str:
    rows = db.execute("PRAGMA quick_check" if quick else "PRAGMA integrity_check").fetchall()
    text = "; ".join(str(r[0]) for r in rows[:5])
    return "ok" if text == "ok" else text


def _views(db: sqlite3.Connection) -> dict:
    """The index holds each chunk three times: the row, its full-text entry
    and its vector. Counted in one read transaction, so a writer committing
    in between cannot make them look different."""
    views: dict = {}
    names = {row[0] for row in db.execute("SELECT name FROM sqlite_master")}
    if "chunks" not in names:
        return views
    db.execute("BEGIN")
    try:
        views["chunks"] = db.execute("SELECT count(*) FROM chunks").fetchone()[0]
        if "fts_chunks_docsize" in names:
            views["fulltext"] = db.execute(
                "SELECT count(*) FROM fts_chunks_docsize").fetchone()[0]
        if "vec_chunks" in names:
            try:
                views["vectors"] = db.execute("SELECT count(*) FROM vec_chunks").fetchone()[0]
            except sqlite3.OperationalError:
                views["vectors"] = None  # sqlite-vec not loadable here
        if "vec_chunks_new" in names:
            views["reembed_in_progress"] = True
    finally:
        db.rollback()
    return views


def _view_problems(name: str, views: dict) -> list[tuple[str, str]]:
    out = []
    chunks = views.get("chunks")
    if chunks is None:
        return out
    if views.get("fulltext") is not None and views["fulltext"] != chunks:
        out.append((f"{name}:fulltext",
                    f"{name}: {chunks} chunks but {views['fulltext']} full-text entries"))
    if views.get("vectors") is not None and views["vectors"] != chunks:
        out.append((f"{name}:vectors",
                    f"{name}: {chunks} chunks but {views['vectors']} vectors"))
    return out


def describe(path: Path, *, quick: bool = False, readonly: bool = True) -> dict:
    """Size, integrity, row counts and, for the index, its three views.
    A backup's copy is opened read-only so checking it never changes it."""
    db = _connect(path, readonly=readonly)
    try:
        info = {"bytes": path.stat().st_size, "integrity": _integrity(db, quick),
                "tables": _tables(db)}
        views = _views(db)
        if views:
            info["views"] = views
        return info
    finally:
        db.close()


# --------------------------------------------------------------------- create

def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9.]+", "-", text.lower()).strip("-")[:48] or "backup"


@contextlib.contextmanager
def _lock():
    from .trigger import _exclusive

    try:
        config.ensure_data_home()
        held = _exclusive(config.data_home() / ".backup.lock", timeout=LOCK_TIMEOUT)
        acquired = held.__enter__()
    except _FAILURES as exc:
        raise BackupError(f"cannot take the backup lock: {exc}") from exc
    try:
        if not acquired:
            raise BackupError("another backup of this data home is still running")
        yield
    finally:
        held.__exit__(None, None, None)


def _private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if not config.WINDOWS:
        path.chmod(0o700)


def _copy_db(src: Path, dst: Path) -> None:
    """A consistent copy through SQLite's backup API, in one step so the copy
    is a single moment of the source. The source is opened read-only: a
    store that vanished raises instead of being copied as an empty one."""
    from . import store

    source = sqlite3.connect(f"file:{quote(str(src))}?mode=ro", uri=True,
                             timeout=store.BUSY_TIMEOUT)
    try:
        target = sqlite3.connect(dst, timeout=store.BUSY_TIMEOUT)
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()
    config.secure_file(dst)


def _copy_tree(src: Path, dst: Path, *, only_missing: bool = False) -> int:
    """Copy a queue directory file by file. Files can be uploaded and removed
    while this runs; those, and partial (hidden or ``.part``) files, are
    skipped. Returns how many files were copied."""
    copied = 0
    for folder, _dirs, files in os.walk(src):
        base = Path(folder)
        target = dst / base.relative_to(src)
        _private_dir(target)
        for name in files:
            if name.startswith(".") or name.endswith(".part"):
                continue
            dest = target / name
            if only_missing and dest.exists():
                continue
            try:
                shutil.copy2(base / name, dest)
            except FileNotFoundError:
                continue
            config.secure_file(dest)
            copied += 1
    return copied


def snapshot(reason: str, *, kind: str = "operation",
             label: str | None = None) -> Path | None:
    """Back up every store in the data home; return the backup's directory,
    or None when there is nothing to back up. ``kind`` is ``operation``,
    ``upgrade`` or ``manual`` (never pruned). Raises BackupError."""
    with _lock():
        return _snapshot(reason, kind=kind, label=label)


def _snapshot(reason: str, *, kind: str, label: str | None) -> Path | None:
    try:
        return _snapshot_unlocked(reason, kind=kind, label=label)
    except BackupError:
        raise
    except _FAILURES as exc:
        raise BackupError(f"{type(exc).__name__}: {exc}") from exc


def _snapshot_unlocked(reason: str, *, kind: str, label: str | None) -> Path | None:
    files = stores()
    if not files:
        return None
    base = root()
    _private_dir(base)
    size = sum(p.stat().st_size for p in files)
    size += sum(Path(str(p) + "-wal").stat().st_size for p in files
                if Path(str(p) + "-wal").exists())
    free = shutil.disk_usage(base).free
    if free < size + SPACE_MARGIN:
        raise BackupError(
            f"not enough free space in {base} for a backup: {size / 2**20:.0f} MB "
            f"needed, {free / 2**20:.0f} MB free. Free some space or set "
            "AGENT_HISTORY_BACKUP_DIR to another disk.")

    when = datetime.datetime.now(datetime.timezone.utc)
    name = f"{when.strftime('%Y%m%d-%H%M%S')}-{_slug(label or reason)}"
    final, n = base / name, 1
    while final.exists():
        n += 1
        final = base / f"{name}-{n}"
    work = base / f"{PARTIAL}{final.name}"
    if work.exists():
        shutil.rmtree(work)
    _private_dir(work)

    entries: dict[str, dict] = {}
    damaged = []
    try:
        for path in files:
            copy = work / path.name
            _copy_db(path, copy)
            try:
                info = describe(copy)
            except sqlite3.DatabaseError as exc:
                info = {"bytes": copy.stat().st_size, "integrity": str(exc), "tables": {}}
            if info["integrity"] != "ok":
                damaged.append(f"{path.name} ({info['integrity']})")
            entries[path.name] = {**info, "sha256": _sha256(copy)}
        extras = []
        from .mcp_auth import tokens_path

        token = tokens_path()  # $AGENT_HISTORY_MCP_TOKENS, else the data home's
        if token.is_file():
            shutil.copy2(token, work / TOKEN_FILE)
            config.secure_file(work / TOKEN_FILE)
            extras.append(TOKEN_FILE)
        for box in OUTBOXES:
            source = config.data_home() / box
            if source.is_dir():
                _copy_tree(source, work / box)
                extras.append(box)
        manifest = {
            "format": 1, "name": final.name, "reason": reason, "label": label,
            "kind": kind, "manual": kind == "manual",
            "created": when.isoformat(timespec="microseconds"),
            "version": __version__, "host": socket.gethostname(),
            "data_home": str(config.data_home()), "files": entries, "extras": extras,
            "damaged": damaged,
            "token_path": str(token) if TOKEN_FILE in extras else None,
        }
        manifest_path = work / MANIFEST
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        config.secure_file(manifest_path)
        os.replace(work, final)
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise
    if damaged:
        print(f"WARNING: backup {final.name}: the copy of {', '.join(damaged)} failed "
              "its integrity check — the live store is probably damaged. It was kept "
              "as it is; run `agent-history backup check`.", file=sys.stderr)
    if kind != "manual":
        prune()
    return final


# ----------------------------------------------------------------------- read

def read_manifest(path: Path) -> dict:
    try:
        return json.loads((path / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BackupError(f"{path} is not a backup (no readable {MANIFEST}: {exc})") from None


def damaged(manifest: dict) -> list[str]:
    """Stores whose copy was already damaged when the backup was made."""
    return [name for name, entry in manifest.get("files", {}).items()
            if entry.get("integrity") != "ok"]


def _kind(manifest: dict) -> str:
    return manifest.get("kind") or ("manual" if manifest.get("manual") else "operation")


def backups() -> list[tuple[Path, dict]]:
    """Complete backups of this data home, oldest first."""
    base = root()
    if not base.is_dir():
        return []
    found = []
    for path in base.iterdir():
        if not path.is_dir() or path.name.startswith("."):
            continue
        try:
            found.append((path, read_manifest(path)))
        except BackupError:
            continue
    return sorted(found, key=lambda item: (item[1].get("created") or "", item[0].name))


def resolve(name: str) -> Path:
    """A backup by directory name, path, or ``latest``."""
    if name == "latest":
        found = backups()
        if not found:
            raise BackupError(f"no backups in {root()}")
        return found[-1][0]
    path = Path(name).expanduser()
    if not path.is_absolute():
        path = root() / name
    read_manifest(path)
    return path


def prune() -> list[Path]:
    """Delete the oldest automatic backups: beyond the newest ``keep()`` made
    before operations, and beyond the newest UPGRADE_KEEP made before a new
    version's first write. Backups made by hand are never deleted here.
    Leftover partial copies older than a day go too."""
    found = backups()
    doomed = []
    for kind, limit in (("operation", keep()), ("upgrade", UPGRADE_KEEP)):
        mine = [path for path, manifest in found if _kind(manifest) == kind]
        doomed += [path for path in (mine[:-limit] if len(mine) > limit else [])
                   if not _in_use(path)]
    for path in doomed:
        shutil.rmtree(path, ignore_errors=True)
    base = root()
    if base.is_dir():
        cutoff = time.time() - 86400
        for path in base.iterdir():
            if path.name.startswith(PARTIAL) and path.stat().st_mtime < cutoff:
                shutil.rmtree(path, ignore_errors=True)
    return doomed


def _in_use(path: Path) -> bool:
    """A running operation pinned this backup (see ``guarded``)."""
    try:
        age = time.time() - (path / IN_USE).stat().st_mtime
    except OSError:
        return False
    return age < IN_USE_HOURS * 3600


def verify(path: Path) -> list[str]:
    """Problems with a backup itself: a file missing, changed since it was
    written, or failing SQLite's integrity check. Empty when intact. A copy
    that was already damaged when the backup was made (flagged in its
    manifest) is not a problem of the backup; see ``damaged``."""
    manifest = read_manifest(path)
    problems = []
    for name, entry in manifest.get("files", {}).items():
        copy = path / name
        if not copy.exists():
            problems.append(f"{name}: missing")
            continue
        if _sha256(copy) != entry.get("sha256"):
            problems.append(f"{name}: changed since the backup was made (SHA-256 differs)")
            continue
        if entry.get("integrity") != "ok":
            continue  # damaged when copied, and unchanged since
        try:
            state = describe(copy)["integrity"]
        except sqlite3.DatabaseError as exc:
            state = str(exc)
        if state != "ok":
            problems.append(f"{name}: integrity check failed ({state})")
    return problems


# ---------------------------------------------------------------------- check

@dataclass
class Report:
    against: str | None = None
    problems: list[str] = field(default_factory=list)
    codes: list[str] = field(default_factory=list)
    losses: list[str] = field(default_factory=list)
    changes: list[str] = field(default_factory=list)
    stores: dict[str, dict] = field(default_factory=dict)

    def add(self, code: str, message: str) -> None:
        self.codes.append(code)
        self.problems.append(message)

    def ok(self, no_loss: bool = False) -> bool:
        return not self.problems and not (no_loss and self.losses)

    def as_dict(self) -> dict:
        return {"against": self.against, "problems": self.problems,
                "losses": self.losses, "changes": self.changes, "stores": self.stores}


def _fulltext_check(path: Path) -> str:
    """FTS5's own check of the full-text index against the chunks."""
    db = _connect(path)
    try:
        db.execute("INSERT INTO fts_chunks(fts_chunks, rank) VALUES('integrity-check', 1)")
        db.rollback()
        return "ok"
    except sqlite3.OperationalError as exc:
        if "locked" in str(exc) or "busy" in str(exc):
            return "skipped (index busy)"
        return str(exc)
    except sqlite3.DatabaseError as exc:
        return str(exc)
    finally:
        db.close()


def check(*, against: str | Path | dict | None = "latest", quick: bool = False,
          only: tuple[str, ...] | None = None) -> Report:
    """Check the live stores (``only`` some of them, by file name), and
    compare their row counts with a backup (``latest`` by default; None for
    no comparison)."""
    report = Report()
    for path in stores():
        if only and path.name not in only:
            continue
        try:
            info = describe(path, quick=quick, readonly=False)
        except sqlite3.DatabaseError as exc:
            report.add(f"{path.name}:integrity", f"{path.name}: cannot be read ({exc})")
            continue
        report.stores[path.name] = info
        if info["integrity"] != "ok":
            report.add(f"{path.name}:integrity",
                       f"{path.name}: integrity check failed ({info['integrity']})")
        for code, message in _view_problems(path.name, info.get("views") or {}):
            report.add(code, message)
        if not quick and "fulltext" in (info.get("views") or {}):
            state = _fulltext_check(path)
            info["views"]["fulltext_check"] = state
            if state != "ok" and not state.startswith("skipped"):
                report.add(f"{path.name}:fulltext-check",
                           f"{path.name}: full-text index check failed ({state})")

    manifest = against if isinstance(against, dict) else None
    if manifest is None and against is not None:
        try:
            baseline = against if isinstance(against, Path) else resolve(str(against))
            manifest = read_manifest(baseline)
        except BackupError:
            if against != "latest":
                raise
    if manifest is not None:
        report.against = manifest.get("name")
        for name, entry in manifest.get("files", {}).items():
            if only and name not in only:
                continue
            live = report.stores.get(name)
            if live is None:
                report.losses.append(f"{name}: in the backup but missing now")
                continue
            for table, before in entry.get("tables", {}).items():
                after = live["tables"].get(table)
                if after is None:
                    report.losses.append(f"{name}: table {table} ({before} rows) is gone")
                elif after < before:
                    report.losses.append(f"{name}: {table} {before} → {after} "
                                         f"({after - before})")
                elif after > before:
                    report.changes.append(f"{name}: {table} {before} → {after} "
                                          f"(+{after - before})")
    return report


def _baseline_codes(manifest: dict) -> set[str]:
    """Problem codes a backup already had when it was made."""
    codes = set()
    for name, entry in manifest.get("files", {}).items():
        if entry.get("integrity") != "ok":
            codes.add(f"{name}:integrity")
        codes.update(code for code, _ in _view_problems(name, entry.get("views") or {}))
    return codes


def print_report(report: Report, *, no_loss: bool = False) -> None:
    for name, info in report.stores.items():
        views = info.get("views") or {}
        extra = ""
        if views.get("chunks") is not None:
            extra = (f"  chunks {views['chunks']}, full-text {views.get('fulltext', '-')}, "
                     f"vectors {views.get('vectors', '-')}")
            if views.get("fulltext_check"):
                extra += f", full-text check {views['fulltext_check']}"
        print(f"{name:18} {info['bytes'] / 2**20:8.1f} MB  integrity {info['integrity']}{extra}")
    if report.against:
        print(f"compared with backup {report.against}:")
        for line in report.changes:
            print(f"  grew   {line}")
        for line in report.losses:
            print(f"  FEWER  {line}")
        if not report.changes and not report.losses:
            print("  no table changed size")
    for line in report.problems:
        print(f"PROBLEM: {line}")
    print(f"integrity: {'ok' if report.ok(no_loss) else 'FAILED'}")


# ------------------------------------------------------------------- automatic

def _stamp_path() -> Path:
    return config.data_home() / STAMP


def written_by() -> str | None:
    try:
        return _stamp_path().read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def _version_key(text: str | None) -> tuple:
    """Comparable form of "0.20.0"; empty for a version it cannot read."""
    parts = re.findall(r"\d+", text or "")
    return tuple(int(p) for p in parts[:3]) if parts else ()


def _needs_upgrade_backup(seen: str | None) -> bool:
    """Only a version newer than every one that wrote here backs up: a
    downgrade, or an older install sharing the data home with a newer one,
    would otherwise copy everything on every switch."""
    if seen == __version__:
        return False
    mine, theirs = _version_key(__version__), _version_key(seen)
    return not theirs or not mine or mine > theirs


def guard_upgrade() -> Path | None:
    """Back up the data home the first time a newer version writes to it.

    Cheap when nothing changed (one small file read). Raises BackupError when
    a needed backup cannot be made, so a new version never changes a store
    it could not first copy."""
    if not auto_enabled() or not _needs_upgrade_backup(written_by()):
        return None
    with _lock():
        previous = written_by()
        if not _needs_upgrade_backup(previous):
            return None
        made = None
        if stores():
            made = _snapshot(f"before {__version__} (was {previous or 'unknown'})",
                             kind="upgrade", label=f"upgrade-to-{__version__}")
            print(f"backup: {made} (first write by agent-history {__version__})",
                  file=sys.stderr)
        stamp = _stamp_path()
        tmp = stamp.with_name(f"{STAMP}.{os.getpid()}.tmp")
        try:
            tmp.write_text(__version__ + "\n", encoding="utf-8")
            os.replace(tmp, stamp)
        except OSError as exc:
            raise BackupError(f"cannot write the version stamp: {exc}") from exc
        return made


@contextlib.contextmanager
def guarded(reason: str, *, touches: tuple[str, ...] = ("index.db",),
            erases: bool = False):
    """Back up before an operation that deletes or rewrites rows; check the
    stores it ``touches`` after it.

    Yields the backup's path (None with AGENT_HISTORY_AUTO_BACKUP=0 or
    nothing to back up). A failed backup raises BackupError before the
    operation starts. Afterwards, a problem the backup did not already have
    raises IntegrityError naming the backup; one it had is only reported.
    ``erases``: the operation removes data on purpose, so say where a copy
    of it remains."""
    if not auto_enabled():
        yield None
        return
    made = snapshot(reason)
    manifest = read_manifest(made) if made else None
    pin = made / IN_USE if made else None
    if made:
        print(f"backup: {made}", file=sys.stderr)
        try:
            pin.write_text(f"{os.getpid()} {reason}\n", encoding="utf-8")
        except OSError:
            pin = None
    try:
        yield made
    except BaseException as exc:
        if made:
            print(f"{reason} stopped part-way ({type(exc).__name__}: {exc}). The copy "
                  f"made just before is {made}; check with `agent-history backup "
                  f"check`, restore with `agent-history backup restore {made.name} "
                  "--yes`.", file=sys.stderr)
        raise
    finally:
        if pin is not None:
            pin.unlink(missing_ok=True)
    report = check(against=manifest, quick=True, only=touches)
    known = _baseline_codes(manifest) if manifest else set()
    new = [msg for code, msg in zip(report.codes, report.problems) if code not in known]
    old = [msg for code, msg in zip(report.codes, report.problems) if code in known]
    if new:
        raise IntegrityError(
            "the data failed its check after " + reason + ":\n  " + "\n  ".join(new)
            + (f"\nthe copy made just before is {made}; restore it with: "
               f"agent-history backup restore {made.name} --yes" if made else ""))
    for line in old:
        print(f"warning: already so before {reason}: {line}", file=sys.stderr)
    lost = "; ".join(report.losses)
    print(f"integrity: ok{f' (removed: {lost})' if lost else ''}", file=sys.stderr)
    if made and (erases or report.losses):
        print(f"note: what this removed is still in backup {made.name} (and any older "
              "backups); to erase it everywhere: agent-history backup delete "
              f"{made.name} --yes", file=sys.stderr)


# --------------------------------------------------------------------- restore

def restore(path: Path) -> Path | None:
    """Put a backup's stores back in place, after backing up what is there
    now. Queued batches missing now are put back; the token file is not
    (that would revive revoked or rotated tokens — copy it by hand if
    needed). The indexing lock and the backup lock are held throughout, so
    no run writes in between and no prune removes the backup mid-copy.
    Raises BackupError."""
    from .trigger import _exclusive

    with _exclusive(config.lock_path(), timeout=LOCK_TIMEOUT) as held:
        if not held:
            raise BackupError("an indexing run holds the lock; retry when it finishes")
        with _lock():
            problems = verify(path)
            if problems:
                raise BackupError("the backup is not intact; nothing was restored:\n  "
                                  + "\n  ".join(problems))
            manifest = read_manifest(path)
            before = (_snapshot("before restoring " + path.name, kind="manual",
                                label=f"pre-restore-{path.name}") if stores() else None)
            try:
                home = config.ensure_data_home()
                bad = damaged(manifest)
                for name in manifest.get("files", {}):
                    if name in bad:
                        # The live store may be better than a copy that was
                        # damaged when made; leave it, and say so.
                        print(f"skipped {name}: its copy was damaged when backed up; "
                              f"the live one is left as it is ({path / name} is "
                              "there for recovery by hand)", file=sys.stderr)
                        continue
                    _copy_db(path / name, home / name)
                for box in OUTBOXES:
                    if box in manifest.get("extras", []) and (path / box).is_dir():
                        _copy_tree(path / box, home / box, only_missing=True)
            except _FAILURES as exc:
                raise BackupError(
                    f"restore failed part-way ({exc}); what was there before is in "
                    f"{before}") from exc
    return before
