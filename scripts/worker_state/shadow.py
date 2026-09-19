"""Compare the retired last-record classifier with the new snapshot. Shadow only."""
from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

WATCH_CANDIDATES = (
    Path(__file__).resolve().parents[1] / "qoder-goal-watch.py",
    Path("/usr/local/bin/spectre-qoder-goal-watch"),
)


def load_old_classifier():
    for path in WATCH_CANDIDATES:
        if not path.is_file():
            continue
        spec = importlib.util.spec_from_file_location("qoder_goal_watch_shadow", path)
        if spec is None or spec.loader is None:
            continue
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    return None


def occupancy_from_snapshot(snapshot: dict[str, Any]) -> str:
    state = str((snapshot.get("goal") or {}).get("state") or "UNKNOWN")
    if state in {"RUNNING", "ACCEPTED", "INJECTED", "WAITING"}:
        return "active"
    if state == "PARKED":
        return "parked"
    if state in {"IDLE", "COMPLETED", "FAILED"}:
        return "idle"
    return "unknown"


def old_dispatch_allowed(pos: dict[str, Any] | None) -> bool:
    state = str((pos or {}).get("state") or "unknown")
    if state == "active":
        return False
    if state == "parked" and (pos or {}).get("reason") == "plan_gate":
        return False
    return True


def compare(old_pos: dict[str, Any] | None, snapshot: dict[str, Any]) -> dict[str, Any]:
    old_state = str((old_pos or {}).get("state") or "unknown")
    new_occ = occupancy_from_snapshot(snapshot)
    park_reason = (snapshot.get("goal") or {}).get("park_reason")
    policy = snapshot.get("policy") or {}
    kinds: list[str] = []
    if old_state != new_occ:
        if old_state == "idle" and new_occ == "active":
            kinds.append("old_false_idle")
        elif old_state == "active" and new_occ == "idle":
            kinds.append("dangerous_false_idle")
        else:
            kinds.append("occupancy_mismatch")
    old_park = (old_pos or {}).get("reason") if old_state == "parked" else None
    if old_state == "parked" or new_occ == "parked":
        if old_park != park_reason:
            kinds.append("park_reason_mismatch")
    if old_dispatch_allowed(old_pos) != bool(policy.get("can_dispatch_goal")):
        kinds.append("dispatch_goal_mismatch")
    old_resume = old_dispatch_allowed(old_pos)
    if old_state == "parked" and old_park == "plan_gate":
        old_resume = False
    if old_resume != bool(policy.get("can_resume")) and new_occ == "parked":
        kinds.append("resume_mismatch")
    return {
        "old_state": old_state,
        "old_reason": (old_pos or {}).get("reason"),
        "new_occupancy": new_occ,
        "new_goal_state": (snapshot.get("goal") or {}).get("state"),
        "park_reason": park_reason,
        "policy": policy,
        "kinds": kinds,
        "dangerous": "dangerous_false_idle" in kinds,
    }
