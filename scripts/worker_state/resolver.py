"""Pure fold: journal events + now → snapshot. now is an input so I1 holds."""
from __future__ import annotations

from typing import Any

from .policy import policy_for
from .types import (
    AUTH_PROCESS,
    AUTH_TUI,
    AUTHORITATIVE_TERMINAL,
    HEARTBEAT_HEALTHY_SEC,
    NEW_EPOCH_KINDS,
    PROGRESS_KINDS,
    RESOLVER_VERSION,
    RUNNING_HINT_KINDS,
    SCHEMA_VERSION,
    STALL_DEFAULT_SEC,
    STALL_MODEL_SEC,
    STRUCTURED_HEALTHY_SEC,
    TERMINAL_ATTEMPT,
    UNCONFIRMED_SEC,
    WAIT_BEGIN,
    WAIT_END,
    Event,
    iso_from,
)


def resolve(
    events: list[Event],
    now: float,
    worker_id: str,
    *,
    completion_open: bool = False,
) -> dict[str, Any]:
    goal: dict[str, Any] = {
        "goal_id": None,
        "turn_id": None,
        "dispatch_id": None,
        "attempt_id": None,
        "state": "IDLE",
        "park_reason": None,
        "injected_at": None,
    }
    execution: dict[str, Any] = {
        "progress_seq": 0,
        "current_operation": None,
        "in_flight": None,
        "last_progress_at": None,
        "last_structured_at": None,
        "last_heartbeat_at": None,
        "wait_kind": None,
        "stalled": False,
        "stall_reason": None,
    }
    transport: dict[str, Any] = {"kind": None, "state": "UNKNOWN"}
    last: Event | None = None
    reasons: list[str] = []
    ignored = 0

    for event in events:
        if event.worker_id != worker_id:
            continue
        if _stale_attempt(goal, event):
            ignored += 1
            continue
        if event.authority >= AUTH_TUI:
            ignored += 1
            continue
        if event.kind.startswith("transport."):
            _apply_transport(transport, event)
            last = event
            continue
        if event.kind == "process.sample":
            execution["last_process_at"] = _event_time(event, now)
            payload = event.payload or {}
            execution["process_alive"] = bool(payload.get("alive"))
            execution["cpu_delta"] = int(payload.get("cpu_delta") or 0)
            last = event
            continue
        if event.authority == AUTH_PROCESS:
            last = event
            continue
        if event.kind == "heartbeat":
            execution["last_heartbeat_at"] = _event_time(event, now)
            last = event
            continue
        if _should_start_epoch(goal, event):
            _start_epoch(goal, event, reasons)
        elif _skip_running_hint_on_terminal(goal, event):
            ignored += 1
            continue
        _apply_kind(goal, execution, event, now, reasons)
        last = event

    _apply_time_effects(goal, execution, now)
    waiting = goal["state"] == "WAITING"
    observation = _observation(execution, now)
    policy = policy_for(
        goal_state=goal["state"],
        park_reason=goal["park_reason"],
        stalled=bool(execution["stalled"]),
        waiting=waiting,
        completion_open=completion_open and goal["state"] == "COMPLETED",
    )
    reason = reasons[-1] if reasons else (
        "no evidence; worker unseen" if not events else "resolved"
    )
    snapshot_version = last.journal_seq if last is not None else 0
    return {
        "schema_version": SCHEMA_VERSION,
        "resolver_version": RESOLVER_VERSION,
        "snapshot_version": snapshot_version,
        "worker": worker_id,
        "goal": {
            "goal_id": goal["goal_id"],
            "turn_id": goal["turn_id"],
            "dispatch_id": goal["dispatch_id"],
            "attempt_id": goal["attempt_id"],
            "state": goal["state"],
            "park_reason": goal["park_reason"],
        },
        "transport": transport,
        "observation": {"state": observation},
        "execution": {
            "progress_seq": execution["progress_seq"],
            "current_operation": execution["current_operation"],
            "stalled": bool(execution["stalled"]),
            "stall_reason": execution["stall_reason"],
        },
        "policy": policy,
        "evidence": {
            "kind": last.kind if last else None,
            "source": last.source if last else None,
            "journal_seq": last.journal_seq if last else 0,
        },
        "reason": reason,
        "server_time": iso_from(now),
        "debug": {
            "ignored_events": ignored,
            "in_flight": execution["in_flight"],
            "wait_kind": execution["wait_kind"],
        },
    }


def _stale_attempt(goal: dict, event: Event) -> bool:
    current = goal["attempt_id"]
    incoming = event.attempt_id
    return current is not None and incoming is not None and incoming < current


def _skip_running_hint_on_terminal(goal: dict, event: Event) -> bool:
    if goal["state"] not in TERMINAL_ATTEMPT:
        return False
    if event.kind in AUTHORITATIVE_TERMINAL:
        return False
    if event.kind == "permission.resolved":
        return False
    if event.kind in NEW_EPOCH_KINDS:
        return False
    if event.kind in RUNNING_HINT_KINDS or event.kind in WAIT_BEGIN | WAIT_END:
        return True
    if event.kind == "turn.ended":
        return True
    return False


def _should_start_epoch(goal: dict, event: Event) -> bool:
    payload = event.payload or {}
    dispatch = event.kind == "terminal_write.succeeded" and payload.get("action") == "dispatch_goal"
    resume = event.kind == "input.resume" or (
        event.kind == "terminal_write.succeeded" and payload.get("action") == "resume"
    )
    if event.kind == "input.goal" or event.kind == "goal.created" or dispatch:
        return True
    if resume:
        return True
    if goal["state"] not in TERMINAL_ATTEMPT:
        return False
    if event.kind not in RUNNING_HINT_KINDS:
        return False
    incoming = event.turn_id
    current = goal["turn_id"]
    return bool(incoming) and incoming != current


def _start_epoch(goal: dict, event: Event, reasons: list[str]) -> None:
    resume = event.kind in {"input.resume", "terminal_write.succeeded"} and (
        (event.payload or {}).get("action") == "resume" or event.kind == "input.resume"
    )
    if event.kind == "input.goal" or (
        event.kind == "terminal_write.succeeded"
        and (event.payload or {}).get("action") == "dispatch_goal"
    ):
        goal["goal_id"] = event.goal_id or _mint_from_event(event, "g")
        goal["attempt_id"] = event.attempt_id or 1
        goal["dispatch_id"] = event.dispatch_id or _mint_from_event(event, "d")
    elif resume:
        goal["goal_id"] = event.goal_id or goal["goal_id"]
        goal["attempt_id"] = event.attempt_id or (goal["attempt_id"] or 0) + 1
        goal["dispatch_id"] = event.dispatch_id or _mint_from_event(event, "d")
    else:
        goal["goal_id"] = event.goal_id or goal["goal_id"]
        goal["attempt_id"] = event.attempt_id or (goal["attempt_id"] or 0) + 1
        if event.dispatch_id:
            goal["dispatch_id"] = event.dispatch_id
    if event.turn_id:
        goal["turn_id"] = event.turn_id
    goal["park_reason"] = None
    goal["state"] = "INJECTED" if event.kind in NEW_EPOCH_KINDS else "RUNNING"
    reasons.append("new goal epoch")


def _mint_from_event(event: Event, prefix: str) -> str:
    ident = event.turn_id or event.event_id
    return f"{prefix}-{ident}"[:40]


def _apply_kind(
    goal: dict,
    execution: dict,
    event: Event,
    now: float,
    reasons: list[str],
) -> None:
    kind = event.kind
    payload = event.payload or {}
    when = _event_time(event, now)
    if event.goal_id and goal["goal_id"] is None:
        goal["goal_id"] = event.goal_id
    if event.dispatch_id and goal["dispatch_id"] is None:
        goal["dispatch_id"] = event.dispatch_id
    if event.attempt_id is not None and goal["attempt_id"] is None:
        goal["attempt_id"] = event.attempt_id
    if event.turn_id and goal["turn_id"] is None:
        goal["turn_id"] = event.turn_id

    if kind in {"terminal_write.succeeded", "goal.created", "input.goal", "input.resume"}:
        _bind_identity(goal, event)
        goal["state"] = "INJECTED"
        goal["park_reason"] = None
        goal["injected_at"] = when
        execution["in_flight"] = None
        execution["wait_kind"] = None
        reasons.append("injected awaiting authoritative record")
        return

    if kind == "goal.parked":
        goal["state"] = "PARKED"
        goal["park_reason"] = str(payload.get("park_reason") or "parked")
        if event.turn_id:
            goal["turn_id"] = event.turn_id
        execution["in_flight"] = None
        execution["wait_kind"] = None
        execution["current_operation"] = None
        _bump_progress(execution, kind, when)
        reasons.append(f"parked:{goal['park_reason']}")
        return

    if kind == "goal.completed":
        goal["state"] = "COMPLETED"
        goal["park_reason"] = None
        execution["in_flight"] = None
        execution["wait_kind"] = None
        execution["current_operation"] = None
        _bump_progress(execution, kind, when)
        reasons.append("goal completed")
        return

    if kind in {"goal.failed", "goal.stopped"}:
        goal["state"] = "FAILED" if kind == "goal.failed" else "FAILED"
        goal["park_reason"] = None
        execution["in_flight"] = None
        execution["wait_kind"] = None
        reasons.append("goal failed")
        return

    if kind == "permission.resolved":
        allowed = payload.get("allowed")
        if goal["state"] == "PARKED" and goal["park_reason"] == "plan_gate" and allowed is not False:
            goal["state"] = "RUNNING"
            goal["park_reason"] = None
            reasons.append("plan gate resolved")
        execution["last_structured_at"] = when
        return

    if kind in WAIT_BEGIN:
        _maybe_accept_or_run(goal, event, execution, when, reasons)
        goal["state"] = "WAITING"
        execution["wait_kind"] = kind.rsplit(".", 1)[0]
        execution["current_operation"] = execution["wait_kind"]
        execution["in_flight"] = None
        reasons.append("waiting")
        return

    if kind in WAIT_END:
        if goal["state"] == "WAITING":
            goal["state"] = "RUNNING"
        execution["wait_kind"] = None
        reasons.append("wait ended")
        return

    if kind == "turn.ended":
        last_prog = execution.get("last_structured_at") or execution.get("last_progress_at")
        if last_prog is not None and when < float(last_prog):
            return
        execution["in_flight"] = None
        execution["wait_kind"] = None
        execution["current_operation"] = None
        _bump_progress(execution, kind, when)
        if goal["state"] not in TERMINAL_ATTEMPT:
            goal["state"] = "IDLE"
            reasons.append("turn ended; worker at prompt")
        return

    if kind == "tool.started":
        _maybe_accept_or_run(goal, event, execution, when, reasons)
        execution["in_flight"] = str(payload.get("tool") or payload.get("name") or "tool")
        execution["current_operation"] = execution["in_flight"]
        _bump_progress(execution, kind, when)
        reasons.append("active correlated tool operation")
        return

    if kind in {"tool.completed", "tool.failed"}:
        execution["in_flight"] = None
        execution["last_structured_at"] = when
        _bump_progress(execution, kind, when)
        if goal["state"] in {"IDLE", "INJECTED", "UNCONFIRMED", "ACCEPTED"}:
            _maybe_accept_or_run(goal, event, execution, when, reasons)
        return

    if kind in RUNNING_HINT_KINDS:
        _maybe_accept_or_run(goal, event, execution, when, reasons)
        if kind.startswith("model.request"):
            execution["current_operation"] = "model.request"
        _bump_progress(execution, kind, when)
        reasons.append("active correlated tool operation" if "tool" in kind else "execution evidence")
        return

    execution["last_structured_at"] = when


def _bind_identity(goal: dict, event: Event) -> None:
    payload = event.payload or {}
    if event.goal_id or payload.get("goal_id"):
        goal["goal_id"] = event.goal_id or payload.get("goal_id")
    if event.dispatch_id or payload.get("dispatch_id"):
        goal["dispatch_id"] = event.dispatch_id or payload.get("dispatch_id")
    if event.attempt_id is not None:
        goal["attempt_id"] = event.attempt_id
    elif payload.get("attempt_id") is not None:
        goal["attempt_id"] = int(payload["attempt_id"])
    if event.turn_id:
        goal["turn_id"] = event.turn_id


def _maybe_accept_or_run(
    goal: dict,
    event: Event,
    execution: dict,
    when: float,
    reasons: list[str],
) -> None:
    if goal["state"] in {"INJECTED", "UNCONFIRMED"}:
        if event.turn_id:
            goal["turn_id"] = event.turn_id
        goal["state"] = "ACCEPTED"
        _bump_progress(execution, "goal.accepted", when)
        reasons.append("accepted")
    if goal["state"] in {"IDLE", "ACCEPTED"}:
        goal["state"] = "RUNNING"
        if event.turn_id and goal["turn_id"] is None:
            goal["turn_id"] = event.turn_id


def _bump_progress(execution: dict, kind: str, when: float) -> None:
    execution["last_structured_at"] = when
    if kind in PROGRESS_KINDS:
        execution["progress_seq"] = int(execution["progress_seq"]) + 1
        execution["last_progress_at"] = when


def _apply_transport(transport: dict, event: Event) -> None:
    payload = event.payload or {}
    kind = payload.get("kind") or transport.get("kind")
    if kind:
        transport["kind"] = kind
    if event.kind == "transport.reachable":
        transport["state"] = "REACHABLE"
    elif event.kind == "transport.down":
        transport["state"] = "DOWN"
    elif event.kind == "transport.degraded":
        transport["state"] = "DEGRADED"


def _apply_time_effects(goal: dict, execution: dict, now: float) -> None:
    if goal["state"] == "INJECTED" and goal["injected_at"] is not None:
        if now - float(goal["injected_at"]) >= UNCONFIRMED_SEC:
            goal["state"] = "UNCONFIRMED"
    execution["stalled"] = False
    execution["stall_reason"] = None
    if goal["state"] != "RUNNING":
        return
    if execution["wait_kind"] or execution["in_flight"]:
        return
    last_proc = execution.get("last_process_at")
    if (
        execution.get("process_alive")
        and int(execution.get("cpu_delta") or 0) > 0
        and last_proc is not None
        and now - float(last_proc) < STALL_DEFAULT_SEC
    ):
        return
    last_progress = execution["last_progress_at"]
    if last_progress is None:
        return
    age = now - float(last_progress)
    op = execution["current_operation"]
    limit = STALL_MODEL_SEC if op == "model.request" else STALL_DEFAULT_SEC
    if age >= limit:
        execution["stalled"] = True
        execution["stall_reason"] = f"no progress for {int(age)}s ({op or 'idle-run'})"


def _observation(execution: dict, now: float) -> str:
    hb = execution["last_heartbeat_at"]
    structured = execution["last_structured_at"]
    if hb is not None and now - float(hb) <= HEARTBEAT_HEALTHY_SEC:
        return "HEALTHY"
    if structured is not None and now - float(structured) <= STRUCTURED_HEALTHY_SEC:
        return "HEALTHY"
    if structured is not None or hb is not None:
        return "DEGRADED"
    return "UNKNOWN"


def _event_time(event: Event, now: float) -> float:
    return event.source_ts if event.source_ts is not None else now
