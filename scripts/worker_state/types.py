"""Constants and the evidence record used by the resolver."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

SCHEMA_VERSION = 1
# 1.0.1: session.phase.finished is no longer a turn boundary, and hook.finished
# no longer raises IDLE to RUNNING (design I3). Both changes alter resolved
# output for the same journal, so the version moves with them.
RESOLVER_VERSION = "1.1.0"

# API-down / never-asked. Distinct from IDLE (unseen worker, dispatch allowed).
GOAL_STATES = (
    "UNKNOWN",
    "IDLE",
    "INJECTED",
    "UNCONFIRMED",
    "ACCEPTED",
    "RUNNING",
    "WAITING",
    "ASSIGNING",
    "COMPLETED",
    "FAILED",
    "PARKED",
)

TERMINAL_ATTEMPT = frozenset({"COMPLETED", "FAILED", "PARKED"})
OCCUPIED = frozenset(
    {"INJECTED", "UNCONFIRMED", "ASSIGNING", "ACCEPTED", "RUNNING", "WAITING", "PARKED"}
)

AUTH_AUTHORITATIVE = 1
AUTH_EXECUTION = 2
AUTH_HEARTBEAT = 3
AUTH_PROCESS = 4
AUTH_TUI = 5

PROGRESS_KINDS = frozenset(
    {
        "goal.accepted",
        "model.request.started",
        "model.request.first_token",
        "model.request.completed",
        "tool.started",
        "tool.completed",
        "subagent.result",
        "goal.completed",
        "turn.ended",
    }
)

RUNNING_HINT_KINDS = frozenset(
    {
        "model.request.started",
        "model.request.completed",
        "model.request.failed",
        "model.request.first_token",
        "tool.started",
        "tool.completed",
        "tool.failed",
        "subagent.result",
    }
)
# `hook.finished` is deliberately absent: a hook is corroboration, not a
# lifecycle rising edge (design I3). It still counts as structured evidence for
# observation/health, but it may not raise IDLE to RUNNING — a 13 h old
# `PreToolUse` hook was the only record left in korea-metro-twin's tail, and
# letting it mint RUNNING would have refused every later /goal. The rising edge
# comes from input.goal/terminal_write or from model.request.*/tool.*.

WAIT_BEGIN = frozenset(
    {"delegated_wait.begin", "background_job.begin", "external_wait.begin"}
)
WAIT_END = frozenset(
    {"delegated_wait.end", "background_job.end", "external_wait.end"}
)

NEW_EPOCH_KINDS = frozenset(
    {
        "terminal_write.succeeded",
        "input.goal",
        "input.resume",
        "goal.created",
    }
)

AUTHORITATIVE_TERMINAL = frozenset(
    {"goal.completed", "goal.failed", "goal.stopped", "goal.parked"}
)

COMPLETE_REASONS = frozenset({"goal_complete", "goal_done", "update_goal_complete"})

UNCONFIRMED_SEC = 240.0
STALL_DEFAULT_SEC = 900.0
STALL_MODEL_SEC = 900.0
STRUCTURED_HEALTHY_SEC = 300.0
HEARTBEAT_HEALTHY_SEC = 60.0
CLAIM_RECONCILE_SEC = 30.0

WORKER_NAME_MAX = 64

FAIL_CLOSED_POLICY = {
    "can_dispatch_goal": False,
    "can_resume": False,
    "continuity_eligible": False,
    "continuity_recovery_allowed": False,
    "grokbot_may_advance": False,
    "idle_slo_violated": False,
}

CLAIM_ACTIONS = frozenset({"dispatch_goal", "resume", "advance"})


@dataclass(frozen=True)
class Event:
    journal_seq: int
    event_id: str
    worker_id: str
    kind: str
    source: str
    authority: int
    source_timestamp: str | None = None
    received_at: str = ""
    goal_id: str | None = None
    turn_id: str | None = None
    dispatch_id: str | None = None
    attempt_id: int | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    ignored: bool = False
    ignore_reason: str | None = None

    @property
    def source_ts(self) -> float | None:
        return parse_ts(self.source_timestamp)


def parse_ts(value: str | None) -> float | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def iso_from(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def authority_for(source: str, kind: str) -> int:
    src = str(source or "")
    if src == "tui" or src.startswith("pty"):
        return AUTH_TUI
    if src == "dsh_exit":
        return AUTH_AUTHORITATIVE
    if src == "dsh_jsonl":
        return AUTH_EXECUTION
    if src in {"cgroup", "process", "proc", "tmux", "orca"}:
        return AUTH_PROCESS
    if kind == "heartbeat":
        return AUTH_HEARTBEAT
    if kind.startswith("transport."):
        return AUTH_PROCESS
    if kind in AUTHORITATIVE_TERMINAL or kind in NEW_EPOCH_KINDS:
        return AUTH_AUTHORITATIVE
    if kind == "permission.resolved":
        return AUTH_EXECUTION
    return AUTH_EXECUTION
