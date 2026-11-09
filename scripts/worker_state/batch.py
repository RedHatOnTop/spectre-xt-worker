"""Persist passive poller evidence with one journal replay per worker batch."""
from __future__ import annotations

from typing import Any

from .store import Store, StoreError


def ingest_batch(store: Store, worker: str, events: list[dict[str, Any]], now: float) -> dict:
    for event in events:
        if not isinstance(event, dict):
            raise StoreError("event", "poller event must be an object")
        if event.get("worker") != worker:
            raise StoreError("worker", "poller event worker mismatch")
        if not all(isinstance(event.get(key), str) and event[key].strip()
                   for key in ("event_id", "kind", "source")):
            raise StoreError("event", "poller event identity is missing")
        if event["kind"].startswith("assignment."):
            raise StoreError("assignment", "assignment events require the action API")
    if not events:
        return {"added": 0}
    with store._lock:
        store._conn.execute("BEGIN IMMEDIATE")
        try:
            added = 0
            for event in events:
                existing = store._conn.execute(
                    "SELECT 1 FROM evidence_journal WHERE event_id = ?",
                    (event["event_id"],),
                ).fetchone()
                if existing:
                    continue
                store._insert_event_locked(event, now)
                if event["kind"] in {"goal.completed", "goal.failed"}:
                    store._claim_completion(event, worker, now)
                added += 1
            if added:
                store._rebuild_locked(worker, now)
                store._commit(worker)
            else:
                store._conn.execute("COMMIT")
        except Exception:
            store._conn.execute("ROLLBACK")
            raise
    return {"added": added}
