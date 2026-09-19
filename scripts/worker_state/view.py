"""Project a resolver snapshot into the occupancy view consumers used to probe."""
from __future__ import annotations

from typing import Any


def occupancy(goal_state: str | None) -> str:
    state = str(goal_state or "UNKNOWN")
    if state in {"RUNNING", "ACCEPTED", "INJECTED", "WAITING", "UNCONFIRMED"}:
        return "active"
    if state == "PARKED":
        return "parked"
    if state in {"IDLE", "COMPLETED", "FAILED"}:
        return "idle"
    return "unknown"


def snapshot_to_pos(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Bridge-shaped position derived only from snapshot axes + policy."""
    goal = snapshot.get("goal") if isinstance(snapshot.get("goal"), dict) else {}
    policy = snapshot.get("policy") if isinstance(snapshot.get("policy"), dict) else {}
    evidence = snapshot.get("evidence") if isinstance(snapshot.get("evidence"), dict) else {}
    state = occupancy(goal.get("state"))
    park = goal.get("park_reason")
    reason = park
    if state == "idle" and goal.get("state") == "COMPLETED":
        reason = "goal_complete"
    elif state == "idle" and goal.get("state") == "FAILED":
        reason = "failed"
    elif reason is None:
        reason = str(goal.get("state") or evidence.get("kind") or state)
    return {
        "state": state,
        "reason": reason,
        "turn_id": goal.get("turn_id") or "",
        "ts": snapshot.get("server_time") or "",
        "goal_state": goal.get("state"),
        "park_reason": park,
        "attempt_id": goal.get("attempt_id"),
        "goal_id": goal.get("goal_id"),
        "policy": policy,
        "snapshot_version": snapshot.get("snapshot_version"),
        "stalled": bool((snapshot.get("execution") or {}).get("stalled")),
    }
