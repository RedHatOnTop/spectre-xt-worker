#!/usr/bin/env python3
"""Copy ZCode CLI sessions for one workspace directory between machines."""
from __future__ import annotations

import argparse
import sqlite3
import uuid
from pathlib import Path

TABLES = (
    "session",
    "message",
    "part",
    "todo",
    "session_entry",
    "session_input",
)

ALIASES = {
    "session": "sessions",
    "message": "messages",
    "part": "parts",
    "todo": "todos",
    "session_entry": "session_entries",
    "session_input": "session_inputs",
}


def _aliased(counts: dict[str, int]) -> dict[str, int]:
    out = dict(counts)
    for table, alias in ALIASES.items():
        if table in counts:
            out[alias] = counts[table]
    out["sessions"] = counts.get("session", 0)
    return out


def _connect(path: Path, *, rw: bool) -> sqlite3.Connection:
    if rw:
        conn = sqlite3.connect(path)
    else:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


def snapshot(src: Path, dest: Path) -> None:
    """Consistent copy of a live ZCode db including WAL."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    source = sqlite3.connect(src)
    target = sqlite3.connect(dest)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()


def matching_session_ids(conn: sqlite3.Connection, directory: str) -> list[str]:
    directory = str(Path(directory))
    rows = conn.execute(
        """
        SELECT id FROM session
        WHERE directory = ? OR directory LIKE ?
        ORDER BY time_updated DESC
        """,
        (directory, directory.rstrip("/") + "/%"),
    ).fetchall()
    return [r["id"] for r in rows]


def export_sessions(src_db: Path, directory: str, bundle: Path) -> dict[str, int]:
    snap = bundle.with_suffix(bundle.suffix + ".snap")
    snapshot(src_db, snap)
    src = _connect(snap, rw=False)
    if bundle.exists():
        bundle.unlink()
    out = sqlite3.connect(bundle)
    counts: dict[str, int] = {t: 0 for t in TABLES}
    try:
        ids = matching_session_ids(src, directory)
        if not ids:
            out.close()
            src.close()
            snap.unlink(missing_ok=True)
            return {**counts, "sessions": 0}

        for table in TABLES:
            if not _table_exists(src, table):
                continue
            create = src.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            if not create or not create[0]:
                continue
            out.executescript(create[0])
            cols = _columns(src, table)
            col_sql = ", ".join(cols)
            placeholders = ", ".join("?" for _ in cols)
            q = ",".join("?" for _ in ids)
            if table == "session":
                rows = src.execute(
                    f"SELECT {col_sql} FROM session WHERE id IN ({q})", ids
                ).fetchall()
            else:
                rows = src.execute(
                    f"SELECT {col_sql} FROM {table} WHERE session_id IN ({q})", ids
                ).fetchall()
            out.executemany(
                f"INSERT INTO {table} ({col_sql}) VALUES ({placeholders})",
                [tuple(r[c] for c in cols) for r in rows],
            )
            counts[table] = len(rows)
        out.commit()
    finally:
        out.close()
        src.close()
        snap.unlink(missing_ok=True)
    return _aliased(counts)


def _fresh_id(old_id: str, taken: set[str]) -> str:
    """Generate a new session id derived from old_id, unique vs taken."""
    candidate = f"{old_id[:8]}-replay-{uuid.uuid4().hex[:12]}"
    while candidate in taken:
        candidate = f"{old_id[:8]}-replay-{uuid.uuid4().hex[:12]}"
    return candidate


def import_sessions(
    bundle: Path, dest_db: Path, *, as_new: bool = False
) -> dict[str, int]:
    """Import a bundle. With as_new=True (the safe default for pulling
    sessions onto a machine that may already hold them), every imported
    session gets a fresh id so nothing already on this machine is ever
    overwritten — the incoming history appears as new sessions."""
    if not dest_db.exists():
        raise FileNotFoundError(
            f"destination ZCode db missing: {dest_db} (open ZCode once on this machine first)"
        )
    src = _connect(bundle, rw=False)
    dest = _connect(dest_db, rw=True)
    dest.execute("PRAGMA foreign_keys=OFF")
    counts: dict[str, int] = {}
    try:
        id_map: dict[str, str] = {}
        if as_new and _table_exists(src, "session"):
            taken = {r[0] for r in dest.execute("SELECT id FROM session")}
            for row in src.execute("SELECT DISTINCT id FROM session").fetchall():
                old = row[0]
                id_map[old] = _fresh_id(old, taken | set(id_map.values()))
            # Message ids live in their own namespace but parts reference
            # them; remap them too so cross-references stay consistent.
            if _table_exists(src, "message"):
                for row in src.execute("SELECT DISTINCT id FROM message").fetchall():
                    old = row[0]
                    id_map.setdefault(old, f"{old}-r{uuid.uuid4().hex[:8]}")

        for table in TABLES:
            if not _table_exists(src, table) or not _table_exists(dest, table):
                continue
            src_cols = _columns(src, table)
            dest_cols = _columns(dest, table)
            cols = [c for c in src_cols if c in dest_cols]
            if not cols:
                continue
            col_sql = ", ".join(cols)
            placeholders = ", ".join("?" for _ in cols)
            rows = src.execute(f"SELECT {col_sql} FROM {table}").fetchall()
            transformed: list[tuple] = []
            for r in rows:
                record = [r[c] for c in cols]
                if id_map:
                    for ref_col in ("id", "session_id", "message_id"):
                        if ref_col in cols:
                            idx = cols.index(ref_col)
                            value = record[idx]
                            if isinstance(value, str):
                                record[idx] = id_map.get(value, value)
                transformed.append(tuple(record))
            dest.executemany(
                f"INSERT OR REPLACE INTO {table} ({col_sql}) VALUES ({placeholders})",
                transformed,
            )
            counts[table] = len(transformed)
        dest.commit()
    finally:
        dest.close()
        src.close()
    return _aliased(counts)


def main() -> int:
    parser = argparse.ArgumentParser(prog="warp-zcode")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_ex = sub.add_parser("export")
    p_ex.add_argument("--db", type=Path, required=True)
    p_ex.add_argument("--directory", required=True)
    p_ex.add_argument("--out", type=Path, required=True)

    p_im = sub.add_parser("import")
    p_im.add_argument("--db", type=Path, required=True)
    p_im.add_argument("--from", dest="bundle", type=Path, required=True)
    p_im.add_argument(
        "--as-new",
        action="store_true",
        help="assign fresh ids so nothing existing is overwritten",
    )

    args = parser.parse_args()
    if args.cmd == "export":
        summary = export_sessions(args.db, args.directory, args.out)
        print(summary)
        return 0 if summary["sessions"] else 1
    summary = import_sessions(args.bundle, args.db, as_new=args.as_new)
    print(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
