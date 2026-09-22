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
    apply_time: bool = True,
) -> dict[str, Any]:
    goal: dict[str, Any] = {
        "goal_id": None,
        "turn_id": None,
        "dispatch_id": None,
        "attempt_id": None,
        "state": "IDLE",
        "park_reason": None,
        "injected_at": None,
        "assignment_id": None,
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
        "target": "efficient",
        "process_alive": False,
        "cpu_delta": 0,
        "last_process_at": None,
    }
    transport: dict[str, Any] = {"kind": None, "state": "UNKNOWN"}
    last: Event | None = None
    reasons: list[str] = []
    ignored = 0

    for event in events:
        if event.worker_id != worker_id:
            continue
        if goal["state"] == "ASSIGNING" and event.source != "api":
            ignored += 1
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

    if apply_time:
        _apply_time_effects(goal, execution, now)
    waiting = goal["state"] == "WAITING"
    observation = _observation(execution, now)
    idle_slo = _idle_slo(
        goal["state"],
        execution,
        now,
        completion_open=completion_open and goal["state"] in {"COMPLETED", "FAILED"},
    )
    policy = policy_for(
        goal_state=goal["state"],
        park_reason=goal["park_reason"],
        stalled=bool(execution["stalled"]),
        waiting=waiting,
        completion_open=completion_open and goal["state"] in {"COMPLETED", "FAILED"},
        target=str(execution.get("target") or "efficient"),
        idle_slo=idle_slo,
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
            "injected_at": goal["injected_at"],
            "assignment_id": goal["assignment_id"],
        },
        "transport": transport,
        "observation": {"state": observation},
        "execution": {
            "progress_seq": execution["progress_seq"],
            "current_operation": execution["current_operation"],
            "stalled": bool(execution["stalled"]),
            "stall_reason": execution["stall_reason"],
            "target": execution.get("target") or "efficient",
            "in_flight": execution.get("in_flight"),
            "wait_kind": execution.get("wait_kind"),
            "last_progress_at": execution.get("last_progress_at"),
            "process_alive": bool(execution.get("process_alive")),
            "cpu_delta": int(execution.get("cpu_delta") or 0),
            "last_process_at": execution.get("last_process_at"),
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

    if kind == "assignment.started":
        goal["state"] = "ASSIGNING"
        goal["assignment_id"] = payload.get("request_id")
        goal["park_reason"] = None
        reasons.append("assigning")
        return
    if kind == "assignment.failed":
        goal["state"] = "COMPLETED"
        goal["assignment_id"] = None
        reasons.append("assignment failed")
        return
    if kind == "assignment.finished":
        goal["state"] = "COMPLETED"
        goal["assignment_id"] = None
        reasons.append("assignment finished")
        return
    if kind == "advance.completed":
        reasons.append("advance closed")
        return
    if kind == "terminal_write.failed":
        goal["state"] = "FAILED"
        goal["park_reason"] = str(payload.get("error") or "write_failed")
        reasons.append("terminal write failed")
        return
    if kind in {"terminal_write.succeeded", "goal.created", "input.goal", "input.resume"}:
        if kind == "terminal_write.succeeded" and payload.get("action") == "advance":
            return
        _bind_identity(goal, event)
        if payload.get("target"):
            execution["target"] = str(payload.get("target"))
        elif kind != "input.resume":
            execution["target"] = execution.get("target") or "efficient"
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
    injected_at = goal.get("injected_at")
    if goal["state"] in {"INJECTED", "UNCONFIRMED"} and injected_at is not None:
        age = now - float(injected_at)
        if age >= UNCONFIRMED_SEC:
            goal["state"] = "FAILED"
            goal["park_reason"] = "unconfirmed_timeout"
        elif goal["state"] == "INJECTED" and age >= 120:
            goal["state"] = "UNCONFIRMED"
    execution["stalled"] = False
    execution["stall_reason"] = None
    if goal["state"] != "RUNNING":
        return
    if execution.get("wait_kind"):
        return
    last_proc = execution.get("last_process_at")
    cpu = int(execution.get("cpu_delta") or 0)
    if (
        execution.get("process_alive")
        and cpu > 0
        and last_proc is not None
        and now - float(last_proc) < STALL_DEFAULT_SEC
    ):
        return
    in_flight = execution.get("in_flight")
    if in_flight:
        structured = execution.get("last_structured_at") or execution.get("last_progress_at")
        if cpu > 0:
            return
        if structured is not None and now - float(structured) < STALL_DEFAULT_SEC:
            return
        execution["stalled"] = True
        execution["stall_reason"] = f"wedged_tool ({in_flight})"
        return
    last_progress = execution.get("last_progress_at")
    if last_progress is None:
        return
    age = now - float(last_progress)
    op = execution.get("current_operation")
    limit = STALL_MODEL_SEC if op == "model.request" else STALL_DEFAULT_SEC
    if age >= limit:
        execution["stalled"] = True
        execution["stall_reason"] = f"no progress for {int(age)}s ({op or 'idle-run'})"


def _idle_slo(goal_state: str, execution: dict, now: float, *, completion_open: bool) -> bool:
    if goal_state == "RUNNING" and execution.get("stalled"):
        return True
    if completion_open and goal_state == "COMPLETED":
        last = execution.get("last_progress_at")
        if last is not None and now - float(last) >= STALL_DEFAULT_SEC:
            return True
    return False


def apply_now_effects(snapshot: dict[str, Any], now: float) -> dict[str, Any]:
    """Recompute stall / UNCONFIRMED / policy from stored timestamps. No journal."""
    import copy

    out = copy.deepcopy(snapshot)
    goal = dict(out.get("goal") or {})
    execution = dict(out.get("execution") or {})
    _apply_time_effects(goal, execution, now)
    waiting = goal.get("state") == "WAITING"
    idle_slo = _idle_slo(
        str(goal.get("state") or ""),
        execution,
        now,
        completion_open=bool((out.get("policy") or {}).get("grokbot_may_advance"))
        and goal.get("state") in {"COMPLETED", "FAILED"},
    )
    out["goal"] = goal
    out["execution"] = execution
    out["observation"] = {"state": _observation(execution, now)}
    out["policy"] = policy_for(
        goal_state=str(goal.get("state") or "UNKNOWN"),
        park_reason=goal.get("park_reason"),
        stalled=bool(execution.get("stalled")),
        waiting=waiting,
        completion_open=bool((snapshot.get("policy") or {}).get("grokbot_may_advance"))
        and goal.get("state") in {"COMPLETED", "FAILED"},
        target=str(execution.get("target") or "efficient"),
        idle_slo=idle_slo,
    )
    out["server_time"] = iso_from(now)
    return out


def _observation(execution: dict, now: float) -> str:
    hb = execution.get("last_heartbeat_at")
    structured = execution.get("last_structured_at")
    if hb is not None and now - float(hb) <= HEARTBEAT_HEALTHY_SEC:
        return "HEALTHY"
    if structured is not None and now - float(structured) <= STRUCTURED_HEALTHY_SEC:
        return "HEALTHY"
    if structured is not None or hb is not None:
        return "DEGRADED"
    return "UNKNOWN"


def _event_time(event: Event, now: float) -> float:
    return event.source_ts if event.source_ts is not None else now
