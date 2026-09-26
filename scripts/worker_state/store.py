"""SQLite WAL store. Evidence append, snapshot, claims share one transaction."""
from __future__ import annotations

import copy
import json
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Any

from .policy import fail_closed_snapshot
from .resolver import apply_now_effects, resolve
from .types import (
    CLAIM_RECONCILE_SEC,
    Event,
    RESOLVER_VERSION,
    SCHEMA_VERSION,
    authority_for,
    iso_from,
)

SCHEMA_FILE = Path(__file__).resolve().parent / "schema.sql"
SCHEMA_VERSION_SQL = 1


class StoreError(Exception):
    def __init__(self, code: str, message: str, http: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http = http


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._published: dict[str, dict[str, Any]] = {}
        self._conn = sqlite3.connect(
            str(self.path),
            check_same_thread=False,
            isolation_level=None,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._migrate()
        rows = self._conn.execute(
            "SELECT worker_id, snapshot_json FROM worker_snapshots WHERE resolver_version = ?",
            (RESOLVER_VERSION,),
        ).fetchall()
        self._published = {row["worker_id"]: json.loads(row["snapshot_json"]) for row in rows}

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _migrate(self) -> None:
        with self._lock:
            cur = self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
            )
            if cur.fetchone() is None:
                if self.path.exists() and self.path.stat().st_size > 0:
                    backup = self.path.with_suffix(self.path.suffix + ".bak-pre-v1")
                    if not backup.exists():
                        backup.write_bytes(self.path.read_bytes())
                sql = SCHEMA_FILE.read_text(encoding="utf-8")
                self._conn.executescript(sql)
                self._conn.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (SCHEMA_VERSION_SQL, iso_from(_now_fallback())),
                )
                return
            row = self._conn.execute(
                "SELECT MAX(version) AS v FROM schema_migrations"
            ).fetchone()
            current = int(row["v"] or 0) if row else 0
            if current < SCHEMA_VERSION_SQL:
                raise StoreError("schema", f"unknown migration path from {current}", 500)

    def pragmas(self) -> dict[str, str]:
        with self._lock:
            mode = self._conn.execute("PRAGMA journal_mode").fetchone()[0]
            sync = self._conn.execute("PRAGMA synchronous").fetchone()[0]
            fk = self._conn.execute("PRAGMA foreign_keys").fetchone()[0]
            return {
                "journal_mode": str(mode).upper(),
                "synchronous": str(sync),
                "foreign_keys": str(fk),
            }

    def ingest(self, raw: dict[str, Any], now: float) -> dict[str, Any]:
        event_id = str(raw.get("event_id") or "").strip()
        worker = str(raw.get("worker") or raw.get("worker_id") or "").strip()
        kind = str(raw.get("kind") or "").strip()
        source = str(raw.get("source") or "").strip()
        if not event_id:
            raise StoreError("event_id", "event_id is required")
        if not worker:
            raise StoreError("worker", "worker is required")
        if not kind:
            raise StoreError("kind", "kind is required")
        if not source:
            raise StoreError("source", "source is required")
        payload = raw.get("payload") if isinstance(raw.get("payload"), dict) else {}
        authority = raw.get("authority")
        if authority is None:
            authority = authority_for(source, kind)
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                existing = self._conn.execute(
                    "SELECT journal_seq FROM evidence_journal WHERE event_id = ?",
                    (event_id,),
                ).fetchone()
                if existing is None:
                    if kind.startswith("assignment."):
                        self._validate_assignment(raw, payload, worker, now)
                    self._conn.execute(
                        """
                        INSERT INTO evidence_journal (
                          event_id, source_timestamp, received_at, worker_id,
                          goal_id, turn_id, dispatch_id, attempt_id,
                          kind, source, authority, payload_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            event_id,
                            raw.get("source_timestamp"),
                            iso_from(now),
                            worker,
                            raw.get("goal_id"),
                            raw.get("turn_id"),
                            raw.get("dispatch_id"),
                            raw.get("attempt_id"),
                            kind,
                            source,
                            int(authority),
                            json.dumps(payload),
                        ),
                    )
                    if kind in {"goal.completed", "goal.failed"}:
                        self._claim_completion(raw, worker, now)
                snapshot = self._rebuild_locked(worker, now)
                self._commit(worker)
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            return apply_now_effects(copy.deepcopy(snapshot), now)

    def snapshot(self, worker: str, now: float, *, rebuild: bool = True) -> dict[str, Any]:
        if rebuild:
            with self._lock:
                self._conn.execute("BEGIN IMMEDIATE")
                try:
                    self._rebuild_locked(worker, now)
                    self._commit(worker)
                except Exception:
                    self._conn.execute("ROLLBACK")
                    raise
                folded = copy.deepcopy(self._published[worker])
            return apply_now_effects(folded, now)
        published = self._published.get(worker)
        if published is None:
            return fail_closed_snapshot(worker, "snapshot not published", iso_from(now))
        return apply_now_effects(copy.deepcopy(published), now)

    def replay(self, worker: str, now: float) -> dict[str, Any]:
        return self.snapshot(worker, now, rebuild=True)

    def events_for(self, worker: str) -> list[Event]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM evidence_journal
                WHERE worker_id = ?
                ORDER BY journal_seq ASC
                """,
                (worker,),
            ).fetchall()
        return [_row_to_event(row) for row in rows]

    def claim(
        self,
        worker: str,
        action: str,
        expected_snapshot_version: int,
        idempotency_key: str,
        now: float,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if action not in {"dispatch_goal", "resume", "advance"}:
            raise StoreError("action", f"unknown action {action!r}")
        if not idempotency_key:
            raise StoreError("idempotency_key", "idempotency_key is required")
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                snapshot = self._rebuild_locked(worker, now)
                reused = self._conn.execute(
                    "SELECT * FROM action_claims WHERE idempotency_key = ?",
                    (idempotency_key,),
                ).fetchone()
                if reused:
                    self._commit(worker)
                    return _claim_to_dict(reused, reused=True)
                if int(snapshot["snapshot_version"]) != int(expected_snapshot_version):
                    raise StoreError(
                        "snapshot_version",
                        "snapshot_version mismatch",
                        409,
                    )
                policy = snapshot.get("policy") or {}
                needed = {
                    "dispatch_goal": "can_dispatch_goal",
                    "resume": "can_resume",
                    "advance": "grokbot_may_advance",
                }[action]
                if not policy.get(needed):
                    raise StoreError("policy", f"{needed} is false", 409)
                inflight = self._conn.execute(
                    """
                    SELECT action_id FROM action_claims
                    WHERE worker_id = ? AND state = 'claimed'
                    """,
                    (worker,),
                ).fetchone()
                if inflight:
                    raise StoreError("conflict", "worker has an in-flight action", 409)
                identity = _mint_identity(snapshot, action)
                action_id = str(uuid.uuid4())
                self._conn.execute(
                    """
                    INSERT INTO action_claims (
                      action_id, worker_id, action, idempotency_key,
                      expected_snapshot_version, snapshot_version_at_claim,
                      state, delivery_state, goal_id, turn_id, dispatch_id,
                      attempt_id, claimed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'claimed', 'pending', ?, ?, ?, ?, ?)
                    """,
                    (
                        action_id,
                        worker,
                        action,
                        idempotency_key,
                        int(expected_snapshot_version),
                        int(snapshot["snapshot_version"]),
                        identity["goal_id"],
                        snapshot["goal"].get("turn_id"),
                        identity["dispatch_id"],
                        identity["attempt_id"],
                        iso_from(now),
                    ),
                )
                self._insert_event_locked(
                    {
                        "event_id": f"claim-{action_id}",
                        "worker": worker,
                        "kind": "action.claimed",
                        "source": "api",
                        "source_timestamp": iso_from(now),
                        "goal_id": identity["goal_id"],
                        "dispatch_id": identity["dispatch_id"],
                        "attempt_id": identity["attempt_id"],
                        "payload": {
                            "action": action,
                            "action_id": action_id,
                            **({k: extra[k] for k in extra if k in {"target", "terminal", "pin"}} if extra else {}),
                        },
                    },
                    now,
                )
                self._rebuild_locked(worker, now)
                row = self._conn.execute(
                    "SELECT * FROM action_claims WHERE action_id = ?",
                    (action_id,),
                ).fetchone()
                self._commit(worker)
                return _claim_to_dict(row, reused=False)
            except StoreError:
                self._conn.execute("ROLLBACK")
                raise
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def action_result(
        self, action_id: str, ok: bool, now: float, error: str | None = None
    ) -> dict[str, Any]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT * FROM action_claims WHERE action_id = ?",
                    (action_id,),
                ).fetchone()
                if row is None:
                    raise StoreError("action_id", "unknown action_id", 404)
                worker = row["worker_id"]
                if row["state"] != "claimed":
                    self._commit(worker)
                    return _claim_to_dict(row, reused=True)
                worker = row["worker_id"]
                result = {"ok": bool(ok), "error": error}
                claimed_payload = self._claimed_payload_locked(action_id)
                if ok:
                    state, delivery = "succeeded", "written"
                    kind = (
                        "advance.completed"
                        if row["action"] == "advance"
                        else "terminal_write.succeeded"
                    )
                else:
                    state, delivery = "failed", "failed"
                    kind = (
                        "advance.completed"
                        if row["action"] == "advance"
                        else "terminal_write.failed"
                    )
                self._conn.execute(
                    """
                    UPDATE action_claims
                    SET state = ?, delivery_state = ?, result_at = ?, result_json = ?
                    WHERE action_id = ?
                    """,
                    (state, delivery, iso_from(now), json.dumps(result), action_id),
                )
                self._insert_event_locked(
                    {
                        "event_id": f"result-{action_id}",
                        "worker": worker,
                        "kind": kind,
                        "source": "api",
                        "source_timestamp": iso_from(now),
                        "goal_id": row["goal_id"],
                        "turn_id": row["turn_id"],
                        "dispatch_id": row["dispatch_id"],
                        "attempt_id": row["attempt_id"],
                        "payload": {
                            "action": row["action"],
                            "action_id": action_id,
                            "ok": ok,
                            "error": error,
                            "goal_id": row["goal_id"],
                            "dispatch_id": row["dispatch_id"],
                            "attempt_id": row["attempt_id"],
                            **{
                                k: claimed_payload[k]
                                for k in ("target", "terminal", "pin")
                                if k in claimed_payload
                            },
                        },
                    },
                    now,
                )
                snapshot = self._rebuild_locked(worker, now)
                row = self._conn.execute(
                    "SELECT * FROM action_claims WHERE action_id = ?",
                    (action_id,),
                ).fetchone()
                self._commit(worker)
                out = _claim_to_dict(row, reused=False)
                out["snapshot"] = snapshot
                return out
            except StoreError:
                self._conn.execute("ROLLBACK")
                raise
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def reconcile(self, worker: str, now: float) -> dict[str, Any]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                rows = self._conn.execute(
                    """
                    SELECT * FROM action_claims
                    WHERE worker_id = ? AND state = 'claimed'
                    """,
                    (worker,),
                ).fetchall()
                for row in rows:
                    claimed_at = row["claimed_at"]
                    from .types import parse_ts

                    ts = parse_ts(claimed_at)
                    if ts is None or now - ts < CLAIM_RECONCILE_SEC:
                        continue
                    if row["action"] == "advance":
                        self._conn.execute(
                            """
                            UPDATE action_claims
                            SET state = 'unknown', delivery_state = 'unknown',
                                result_at = ?, result_json = ?
                            WHERE action_id = ?
                            """,
                            (
                                iso_from(now),
                                json.dumps({"ok": None, "error": "reconciled; advance not injected"}),
                                row["action_id"],
                            ),
                        )
                        continue
                    claimed_payload = self._claimed_payload_locked(row["action_id"])
                    inject_kind = "terminal_write.succeeded"
                    if row["action"] == "dispatch_goal" and not claimed_payload.get("target"):
                        inject_kind = "terminal_write.failed"
                    self._conn.execute(
                        """
                        UPDATE action_claims
                        SET state = 'unknown', delivery_state = 'unknown',
                            result_at = ?, result_json = ?
                        WHERE action_id = ?
                        """,
                        (
                            iso_from(now),
                            json.dumps({"ok": None, "error": "reconciled; delivery unknown"}),
                            row["action_id"],
                        ),
                    )
                    self._insert_event_locked(
                        {
                            "event_id": f"reconcile-{row['action_id']}",
                            "worker": worker,
                            "kind": "delivery.unknown",
                            "source": "api",
                            "source_timestamp": iso_from(now),
                            "goal_id": row["goal_id"],
                            "dispatch_id": row["dispatch_id"],
                            "attempt_id": row["attempt_id"],
                            "payload": {
                                "action": row["action"],
                                "action_id": row["action_id"],
                            },
                        },
                        now,
                    )
                    # Ambiguous write: do not retry. Surface UNCONFIRMED via inject evidence.
                    self._insert_event_locked(
                        {
                            "event_id": f"reconcile-inject-{row['action_id']}",
                            "worker": worker,
                            "kind": inject_kind,
                            "source": "api",
                            "source_timestamp": iso_from(now),
                            "goal_id": row["goal_id"],
                            "dispatch_id": row["dispatch_id"],
                            "attempt_id": row["attempt_id"],
                            "payload": {
                                "action": row["action"],
                                "action_id": row["action_id"],
                                "reconciled": True,
                                "goal_id": row["goal_id"],
                                "dispatch_id": row["dispatch_id"],
                                "attempt_id": row["attempt_id"],
                                **{
                                    k: claimed_payload[k]
                                    for k in ("target", "terminal", "pin")
                                    if k in claimed_payload
                                },
                            },
                        },
                        now,
                    )
                snapshot = self._rebuild_locked(worker, now)
                self._commit(worker)
                return snapshot
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def record_shadow(
        self, worker: str, kind: str, old: dict, new: dict, now: float
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO shadow_divergence (worker_id, observed_at, old_json, new_json, kind)
                VALUES (?, ?, ?, ?, ?)
                """,
                (worker, iso_from(now), json.dumps(old), json.dumps(new), kind),
            )

    def shadow_rows(self, worker: str | None = None, limit: int = 50) -> list[dict]:
        with self._lock:
            if worker:
                rows = self._conn.execute(
                    """
                    SELECT * FROM shadow_divergence
                    WHERE worker_id = ?
                    ORDER BY id DESC LIMIT ?
                    """,
                    (worker, limit),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM shadow_divergence ORDER BY id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [dict(row) for row in rows]

    def health(self, now: float) -> dict[str, Any]:
        with self._lock:
            n = self._conn.execute(
                "SELECT COUNT(*) AS n FROM evidence_journal"
            ).fetchone()["n"]
            workers = self._conn.execute(
                "SELECT COUNT(*) AS n FROM worker_snapshots"
            ).fetchone()["n"]
        pragmas = self.pragmas()
        return {
            "ok": True,
            "schema_version": SCHEMA_VERSION,
            "resolver_version": RESOLVER_VERSION,
            "journal_events": int(n),
            "workers": int(workers),
            "pragmas": pragmas,
            "server_time": iso_from(now),
        }

    def _claim_completion(self, raw: dict, worker: str, now: float) -> None:
        goal_id = str(raw.get("goal_id") or "")
        turn_id = str(raw.get("turn_id") or "")
        attempt = raw.get("attempt_id")
        if attempt is None:
            attempt = 0
        ident = str((raw.get("payload") or {}).get("completion_identity") or turn_id or "complete")
        if not goal_id:
            goal_id = f"g-unknown-{worker}"
        if not turn_id:
            turn_id = "unknown"
        self._conn.execute(
            """
            INSERT OR IGNORE INTO completion_claims (
              worker_id, goal_id, turn_id, attempt_id, completion_identity,
              claimed_at, journal_seq
            ) VALUES (?, ?, ?, ?, ?, ?, 0)
            """,
            (worker, goal_id, turn_id, int(attempt), ident, iso_from(now)),
        )

    def _completion_open(self, worker: str, snapshot: dict) -> bool:
        goal = snapshot.get("goal") or {}
        if goal.get("state") not in {"COMPLETED", "FAILED"}:
            return False
        row = self._conn.execute(
            """
            SELECT 1 FROM completion_claims
            WHERE worker_id = ? AND goal_id = ? AND turn_id = ? AND attempt_id = ?
            """,
            (
                worker,
                str(goal.get("goal_id") or f"g-unknown-{worker}"),
                str(goal.get("turn_id") or "unknown"),
                int(goal.get("attempt_id") or 0),
            ),
        ).fetchone()
        claimed = self._conn.execute(
            """
            SELECT 1 FROM action_claims
            WHERE worker_id = ? AND action = 'advance' AND state IN ('succeeded', 'claimed')
              AND goal_id = ? AND COALESCE(attempt_id, 0) = ?
            """,
            (worker, goal.get("goal_id"), int(goal.get("attempt_id") or 0)),
        ).fetchone()
        return row is not None and claimed is None

    def _rebuild_locked(self, worker: str, now: float) -> dict[str, Any]:
        rows = self._conn.execute(
            """
            SELECT * FROM evidence_journal
            WHERE worker_id = ?
            ORDER BY journal_seq ASC
            """,
            (worker,),
        ).fetchall()
        events = [_row_to_event(row) for row in rows]
        # completion_open needs a first pass for COMPLETED, then policy.
        draft = resolve(events, now, worker, completion_open=False, apply_time=False)
        open_flag = self._completion_open(worker, draft)
        snapshot = resolve(events, now, worker, completion_open=open_flag, apply_time=False)
        last_seq = events[-1].journal_seq if events else 0
        snapshot["snapshot_version"] = last_seq
        self._conn.execute(
            """
            INSERT INTO worker_snapshots (
              worker_id, snapshot_version, snapshot_json, journal_seq,
              resolver_version, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(worker_id) DO UPDATE SET
              snapshot_version = excluded.snapshot_version,
              snapshot_json = excluded.snapshot_json,
              journal_seq = excluded.journal_seq,
              resolver_version = excluded.resolver_version,
              updated_at = excluded.updated_at
            """,
            (
                worker,
                snapshot["snapshot_version"],
                json.dumps(snapshot),
                last_seq,
                RESOLVER_VERSION,
                iso_from(now),
            ),
        )
        self._conn.execute(
            """
            INSERT INTO decision_audit (
              worker_id, journal_seq, snapshot_version, snapshot_json,
              reason, resolver_version, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                worker,
                last_seq,
                snapshot["snapshot_version"],
                json.dumps(snapshot),
                snapshot.get("reason") or "",
                RESOLVER_VERSION,
                iso_from(now),
            ),
        )
        return snapshot

    def _commit(self, worker: str) -> None:
        row = self._conn.execute(
            "SELECT snapshot_json FROM worker_snapshots WHERE worker_id = ?", (worker,)
        ).fetchone()
        published = json.loads(row["snapshot_json"]) if row else None
        self._conn.execute("COMMIT")
        if published is not None:
            self._published = {**self._published, worker: published}

    def _validate_assignment(self, raw: dict, payload: dict, worker: str, now: float) -> None:
        if raw.get("source") != "api" or worker != "minecraft":
            raise StoreError("assignment", "assignment requires the minecraft API owner", 409)
        row = self._conn.execute(
            "SELECT * FROM action_claims WHERE action_id = ?", (payload.get("action_id"),)
        ).fetchone()
        if row is None or row["worker_id"] != worker or row["action"] != "advance" or row["state"] != "succeeded":
            raise StoreError("assignment", "assignment requires a succeeded advance", 409)
        snapshot = self._rebuild_locked(worker, now)
        goal = snapshot["goal"]
        if (goal.get("goal_id") != row["goal_id"] or goal.get("attempt_id") != row["attempt_id"]
                or raw.get("goal_id") != row["goal_id"] or raw.get("attempt_id") != row["attempt_id"]):
            raise StoreError("assignment", "stale assignment identity", 409)
        request_id = payload.get("request_id")
        if not isinstance(request_id, str) or not request_id or len(request_id) > 96:
            raise StoreError("assignment", "request_id required")
        if raw["kind"] == "assignment.started":
            if goal["state"] not in {"COMPLETED", "FAILED"}:
                raise StoreError("assignment", "worker is occupied", 409)
        elif raw["kind"] in {"assignment.finished", "assignment.failed"}:
            if goal["state"] != "ASSIGNING" or goal.get("assignment_id") != request_id:
                raise StoreError("assignment", "assignment is not active", 409)
            if raw["kind"] == "assignment.failed":
                self._conn.execute("UPDATE action_claims SET state = 'failed' WHERE action_id = ?",
                                   (row["action_id"],))
        else:
            raise StoreError("assignment", "unknown assignment event")

    def _claimed_payload_locked(self, action_id: str) -> dict[str, Any]:
        row = self._conn.execute(
            "SELECT payload_json FROM evidence_journal WHERE event_id = ?",
            (f"claim-{action_id}",),
        ).fetchone()
        if not row:
            return {}
        try:
            data = json.loads(row["payload_json"] or "{}")
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    def _insert_event_locked(self, raw: dict[str, Any], now: float) -> None:
        payload = raw.get("payload") if isinstance(raw.get("payload"), dict) else {}
        kind = raw["kind"]
        source = raw["source"]
        self._conn.execute(
            """
            INSERT INTO evidence_journal (
              event_id, source_timestamp, received_at, worker_id,
              goal_id, turn_id, dispatch_id, attempt_id,
              kind, source, authority, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                raw["event_id"],
                raw.get("source_timestamp"),
                iso_from(now),
                raw["worker"],
                raw.get("goal_id"),
                raw.get("turn_id"),
                raw.get("dispatch_id"),
                raw.get("attempt_id"),
                kind,
                source,
                int(raw.get("authority") or authority_for(source, kind)),
                json.dumps(payload),
            ),
        )


def _row_to_event(row: sqlite3.Row) -> Event:
    payload = {}
    try:
        payload = json.loads(row["payload_json"] or "{}")
    except json.JSONDecodeError:
        payload = {}
    return Event(
        journal_seq=int(row["journal_seq"]),
        event_id=row["event_id"],
        worker_id=row["worker_id"],
        kind=row["kind"],
        source=row["source"],
        authority=int(row["authority"]),
        source_timestamp=row["source_timestamp"],
        received_at=row["received_at"],
        goal_id=row["goal_id"],
        turn_id=row["turn_id"],
        dispatch_id=row["dispatch_id"],
        attempt_id=row["attempt_id"],
        payload=payload if isinstance(payload, dict) else {},
        ignored=bool(row["ignored"]),
        ignore_reason=row["ignore_reason"],
    )


def _claim_to_dict(row: sqlite3.Row, reused: bool) -> dict[str, Any]:
    return {
        "action_id": row["action_id"],
        "worker": row["worker_id"],
        "action": row["action"],
        "idempotency_key": row["idempotency_key"],
        "state": row["state"],
        "delivery_state": row["delivery_state"],
        "goal_id": row["goal_id"],
        "turn_id": row["turn_id"],
        "dispatch_id": row["dispatch_id"],
        "attempt_id": row["attempt_id"],
        "snapshot_version": row["snapshot_version_at_claim"],
        "reused": reused,
    }


def _mint_identity(snapshot: dict, action: str) -> dict[str, Any]:
    goal = snapshot.get("goal") or {}
    if action == "resume":
        goal_id = goal.get("goal_id") or f"g-{uuid.uuid4().hex[:16]}"
        attempt = int(goal.get("attempt_id") or 0) + 1
    elif action == "advance":
        goal_id = goal.get("goal_id")
        attempt = goal.get("attempt_id")
    else:
        goal_id = f"g-{uuid.uuid4().hex[:16]}"
        attempt = 1
    return {
        "goal_id": goal_id,
        "dispatch_id": f"d-{uuid.uuid4().hex[:16]}",
        "attempt_id": attempt,
    }


def _now_fallback() -> float:
    from time import time

    return time()
