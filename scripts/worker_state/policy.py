"""Policy flags derived from the resolved axes. Consumers read only these."""
from __future__ import annotations

from .types import FAIL_CLOSED_POLICY


def policy_for(
    *,
    goal_state: str,
    park_reason: str | None,
    stalled: bool,
    waiting: bool,
    completion_open: bool,
) -> dict[str, bool]:
    """Occupancy + park_reason. Transport/pane targeting stays in the bridge."""
    policy = dict(FAIL_CLOSED_POLICY)
    if goal_state in {"IDLE", "COMPLETED", "FAILED"}:
        policy["can_dispatch_goal"] = True
    if goal_state == "PARKED" and park_reason == "goal_budget":
        policy["can_resume"] = True
        policy["grokbot_may_advance"] = True
    if goal_state == "COMPLETED" and completion_open:
        policy["grokbot_may_advance"] = True
    if goal_state in {
        "INJECTED",
        "UNCONFIRMED",
        "ACCEPTED",
        "RUNNING",
        "WAITING",
        "PARKED",
    }:
        policy["continuity_eligible"] = True
    if stalled and goal_state == "RUNNING" and not waiting:
        policy["continuity_recovery_allowed"] = True
    if waiting or goal_state == "WAITING":
        policy["continuity_recovery_allowed"] = False
    if goal_state == "PARKED" and park_reason == "plan_gate":
        policy["can_dispatch_goal"] = False
        policy["can_resume"] = False
        policy["grokbot_may_advance"] = False
        policy["continuity_recovery_allowed"] = False
    return policy


def fail_closed_snapshot(worker: str, error: str, server_time: str) -> dict:
    from .types import RESOLVER_VERSION, SCHEMA_VERSION

    return {
        "schema_version": SCHEMA_VERSION,
        "resolver_version": RESOLVER_VERSION,
        "snapshot_version": 0,
        "worker": worker,
        "goal": {
            "goal_id": None,
            "turn_id": None,
            "dispatch_id": None,
            "attempt_id": None,
            "state": "UNKNOWN",
            "park_reason": None,
        },
        "transport": {"kind": None, "state": "UNKNOWN"},
        "observation": {"state": "UNKNOWN"},
        "execution": {
            "progress_seq": 0,
            "current_operation": None,
            "stalled": False,
            "stall_reason": None,
        },
        "policy": dict(FAIL_CLOSED_POLICY),
        "evidence": {"kind": None, "source": None, "journal_seq": 0},
        "reason": f"state api unavailable: {error}",
        "server_time": server_time,
        "debug": {"unavailable": True},
        "ok": False,
    }
