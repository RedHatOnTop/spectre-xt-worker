#!/usr/bin/env python3
"""Grokbot: review a stopped /goal unit and dispatch the next one.

Why this exists (RUNBOOK 7.16): a `/goal`-driven worker on the box stops for
reasons that are invisible to every log the box watches. `qoder-goal-watch`
detects them and posts to Slack, and the bridge can type a new goal — but the
middle piece (read the evidence, decide what happens next, write the next goal)
was a human. On a 24/7 box that human is the bottleneck: `ssh spectre` needs a
browser re-approval every 24 h (Tailscale check mode), so a host-side loop that
drives the box over SSH dies daily. This runs on the box, off the same
outbound-only Slack path the community already uses.

Loop, one stop at a time:

    probe (qoder-goal-watch --probe)  -> gate  -> reviewer (grok, headless)
      -> validate (schema + evidence)  -> dispatch (bridge guard chain)
      -> report (#lobby as `grok`)

The reviewer is the grok CLI in headless mode with a JSON schema
(`structuredOutput`), read-only by construction: `--tools Read` plus a deny
floor, no shell, and an isolated `GROK_HOME` with MCP scanning off — the flags
alone are not enough, because `--tools` restricts built-in tools only and a live
probe on the daily driver caught the model reaching for an MCP write tool
instead (2026-09-15). The supervisor never dispatches on its own word: the
bridge re-applies every guard (registry, worker probe, tmux pane + cwd) before a
single byte is typed.

Safety rails, all fail closed:

  * kill switch — `SPECTRE_GOAL_SUPERVISOR` must be truthy, else this exits 0
    without doing anything (installed off; the unit turns it on)
  * caps — per-worker/day, global/day, cooldown, and a daily USD cost cap read
    from grok's own `total_cost_usd`
  * dedup — one review per stop event (reason + turn_id), persisted
  * repeat guard — the same goal text is never dispatched twice in a row
  * no new evidence, no dispatch — a decision without evidence is discarded
  * plan_gate parks are never typed into; they post an escalation instead

Cost: a review is one grok call (measured 2026-09-15: ~$0.015 for a trivial
prompt, $0.024 for a 4-turn review, ~22k input tokens of system prompt and
rules). This is therefore event-driven — the timer polls cheap local state and
only a *new* stop event buys a model call.

Stdlib only. Never prints token values; neither Slack credentials nor any other
secret enters the reviewer's environment (see reviewer_env). Posts go through
spectre-slack-notify, which reads slack.env itself.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent

HOME = Path.home()
BOX_WORKERS_FILE = Path("/usr/local/share/remote-agent/qoder-workers.json")
REPO_WORKERS_FILE = REPO / "config" / "qoder-workers.json"
BOX_PROMPT_FILE = Path("/usr/local/share/remote-agent/goal-supervisor-prompt.md")
REPO_PROMPT_FILE = REPO / "config" / "goal-supervisor-prompt.md"
DEFAULT_GROK_HOME = HOME / ".local/share/remote-agent/grok-supervisor"
DEFAULT_GROK_BIN = HOME / ".local/bin/grok"
# The reviewer's credential, when the operator does not want an interactive
# login on the box. Same shape and rules as slack.env: 0600, never committed,
# referenced by name only. Exactly the names in GROK_KEY_NAMES are passed to the
# reviewer — never the whole file, never the process environment.
GROK_ENV_FILE = HOME / ".config/remote-agent/grok.env"
GROK_KEY_NAMES = ("XAI_API_KEY", "GROK_CODE_XAI_API_KEY")
STATE_FILE = HOME / ".local/state/remote-agent/goal-supervisor.json"
REVIEW_CWD = HOME / ".local/state/remote-agent/supervisor-cwd"
LOG_FILE = Path("/work/logs/goal-supervisor.log")
WATCHER_BIN = "spectre-qoder-goal-watch"
REPO_WATCHER = REPO / "scripts" / "qoder-goal-watch.py"
BRIDGE_BIN = "spectre-slack-bridge"
REPO_BRIDGE = REPO / "scripts" / "slack-bridge.mjs"
NOTIFY_BIN = "/usr/local/bin/spectre-slack-notify"
REPO_NOTIFY = REPO / "scripts" / "slack-notify.py"

# Parked reasons the reviewer may act on. plan_gate is deliberately absent: the
# CLI is blocked on an ExitPlanMode approval dialog, and only an answer in the
# TUI clears it (see qoder-goal-watch.py).
REVIEW_REASONS = frozenset({"goal_budget"})
# Completion reasons. UNVERIFIED: qoder-goal-watch does not classify a
# "model declared the goal complete" record yet — it needs the box probe in
# RUNBOOK 7.16 first. The supervisor already handles those reasons so the
# watcher can start emitting them without a second change here.
COMPLETE_REASONS = frozenset({"goal_complete", "goal_done", "update_goal_complete"})
DECISIONS = ("resume", "goal", "stop", "escalate")
ACTING_DECISIONS = ("resume", "goal")
GOAL_MAX = 600
RATIONALE_MAX = 600
EVIDENCE_MAX = 4
SESSION_TAIL_RECORDS = 5
SESSION_TAIL_CHARS = 4000
REPO_EVIDENCE_CHARS = 4000
PROMPT_MAX = 24000
GROK_TIMEOUT_SEC = 300
STATE_KEEP = 200
ALERT_COOLDOWN_SEC = 3600

SCHEMA = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": list(DECISIONS)},
        "goal": {"type": "string"},
        "rationale": {"type": "string"},
        "evidence": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["decision", "goal", "rationale", "evidence"],
}

PLACEHOLDERS = ("WORKER", "PROBE_JSON", "REPO_EVIDENCE", "SESSION_TAIL", "PRIOR_ACTIONS")
TRUTHY = ("1", "true", "yes", "on")

# ---------------------------------------------------------------------------
# pure helpers
# ---------------------------------------------------------------------------


def normalize_goal(text: str) -> str:
    """One line, collapsed whitespace: the shape the bridge will type."""
    return re.sub(r"\s+", " ", str(text or "")).strip()


def goal_fingerprint(text: str) -> str:
    return hashlib.sha256(normalize_goal(text).encode("utf-8")).hexdigest()[:16]


TOKEN_RE = re.compile(r"(xox[baprs]-|xapp-|xai-)\S+")


def one_line_log(text: str, limit: int = 200) -> str:
    """Bounded single-line log detail: no newlines, nothing credential-shaped.

    Used for a failed CLI's stderr, which is otherwise the only trace of a
    rejected argument — but a log must never carry a token, even a leaked one.
    """
    flat = re.sub(r"\s+", " ", str(text or "")).strip()
    flat = TOKEN_RE.sub(r"\1<redacted>", flat)
    return flat[:limit]


def day_key(now: float) -> str:
    return datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%d")


def review_key(pos: dict) -> str | None:
    """Dedup key for one reviewable stop; None when there is nothing to review."""
    state = str(pos.get("state") or "unknown")
    reason = str(pos.get("reason") or "")
    ident = str(pos.get("turn_id") or pos.get("ts") or "")
    if not ident:
        return None
    if state == "parked" and reason in REVIEW_REASONS:
        return f"park:{reason}:{ident}"
    if state == "idle" and reason in COMPLETE_REASONS:
        return f"done:{reason}:{ident}"
    return None


def same_day(entry: dict, day: str) -> bool:
    return isinstance(entry, dict) and entry.get("day") == day


def dispatches_today(state: dict, now: float) -> int:
    entry = state.get("day")
    if not isinstance(entry, dict) or entry.get("date") != day_key(now):
        return 0
    return int(entry.get("dispatches") or 0)


def cost_today(state: dict, now: float) -> float:
    entry = state.get("day")
    if not isinstance(entry, dict) or entry.get("date") != day_key(now):
        return 0.0
    try:
        return float(entry.get("cost_usd") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def worker_action_count(worker_state: dict, now: float) -> int:
    """Dispatches for one worker today. Pure."""
    if not same_day(worker_state, day_key(now)):
        return 0
    return int(worker_state.get("count") or 0)


def gate(pos: dict, worker_state: dict, cfg: dict, state: dict, now: float) -> dict:
    """Decide what to do about one worker position. Pure.

    Returns {"action": "review"|"escalate"|"skip", "reason": str, "key": str|None}.
    Every skip carries a machine-readable reason so a quiet tick is explainable.
    """
    name = str(pos.get("state") or "unknown")
    reason = str(pos.get("reason") or "")
    policy = pos.get("policy") if isinstance(pos.get("policy"), dict) else None
    if policy is not None and not policy.get("grokbot_may_advance"):
        if name == "unknown":
            return {"action": "skip", "reason": "unknown_state", "key": None}
        if name == "parked" and reason == "plan_gate":
            ident = str(pos.get("turn_id") or pos.get("ts") or "")
            return {"action": "escalate", "reason": "plan_gate", "key": f"gate:{ident}"}
        if name == "active":
            return {"action": "skip", "reason": "active", "key": None}
        return {"action": "skip", "reason": f"not_a_stop:{reason or name}", "key": None}
    if name == "unknown":
        return {"action": "skip", "reason": "unknown_state", "key": None}
    if name == "active":
        return {"action": "skip", "reason": "active", "key": None}
    if name == "parked" and reason == "plan_gate":
        ident = str(pos.get("turn_id") or pos.get("ts") or "")
        return {"action": "escalate", "reason": "plan_gate", "key": f"gate:{ident}"}
    key = review_key(pos)
    if key is None:
        return {"action": "skip", "reason": f"not_a_stop:{reason or name}", "key": None}
    if key in state.get("reviewed", {}) or key in state.get("escalated", {}):
        return {"action": "skip", "reason": "already_handled", "key": key}
    if cost_today(state, now) >= cfg["max_cost_usd_day"]:
        return {"action": "skip", "reason": "cost_cap", "key": key}
    if dispatches_today(state, now) >= cfg["max_per_day"]:
        return {"action": "skip", "reason": "day_cap", "key": key}
    if worker_action_count(worker_state, now) >= cfg["max_per_worker_day"]:
        return {"action": "skip", "reason": "worker_day_cap", "key": key}
    last_at = worker_state.get("last_at")
    if isinstance(last_at, (int, float)) and 0 <= now - last_at < cfg["cooldown_sec"]:
        return {"action": "skip", "reason": "cooldown", "key": key}
    return {"action": "review", "reason": "reviewable", "key": key}
def validate_decision(payload: object, goal_max: int = GOAL_MAX) -> tuple[bool, str]:
    """Shape and semantics check on the reviewer's structured output. Pure."""
    if not isinstance(payload, dict):
        return False, "payload is not an object"
    decision = str(payload.get("decision") or "")
    if decision not in DECISIONS:
        return False, f"unknown decision {decision!r}"
    rationale = normalize_goal(payload.get("rationale"))
    if not rationale:
        return False, "empty rationale"
    if len(rationale) > RATIONALE_MAX:
        return False, "rationale too long"
    evidence = payload.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        return False, "no evidence"
    if not [item for item in evidence if normalize_goal(item)]:
        return False, "no usable evidence"
    if len(evidence) > EVIDENCE_MAX:
        return False, "too many evidence items"
    if decision == "goal":
        goal = normalize_goal(payload.get("goal"))
        if not goal:
            return False, "goal decision without goal text"
        if len(goal) > goal_max:
            return False, "goal text too long"
    return True, ""


def decision_of(payload: dict) -> dict:
    """Normalized view of a validated decision. Pure."""
    evidence = payload.get("evidence")
    if not isinstance(evidence, list):
        evidence = []
    return {
        "decision": str(payload.get("decision") or ""),
        "goal": normalize_goal(payload.get("goal")),
        "rationale": normalize_goal(payload.get("rationale")),
        "evidence": [normalize_goal(item) for item in evidence],
    }


def repeats_last_goal(fingerprint: str, worker_state: dict, limit: int = 3) -> bool:
    """True when this goal text was already dispatched recently. Pure."""
    goals = worker_state.get("goals")
    if not isinstance(goals, list):
        return False
    return fingerprint in [str(entry) for entry in goals[:limit]]


def render_prompt(template: str, values: dict) -> str:
    """Substitute {{NAME}} placeholders; fail closed on a missing one.

    A template edit that drops a placeholder must stop the run, not silently
    review without evidence.
    """
    found = set(re.findall(r"\{\{([A-Z_]+)\}\}", template))
    missing = sorted(name for name in found if name not in values)
    if missing:
        raise ValueError(f"prompt placeholders not provided: {', '.join(missing)}")
    for name in PLACEHOLDERS:
        template = template.replace("{{" + name + "}}", str(values.get(name, "")))
    if "{{" in template:
        raise ValueError("prompt template still has an unsubstituted placeholder")
    return template


def build_grok_args(prompt: str, cfg: dict) -> list[str]:
    """Headless grok argv for the reviewer. Pure; the read-only pins are the point."""
    args = [
        "-p",
        prompt,
        "--output-format",
        "json",
        "--json-schema",
        json.dumps(SCHEMA, separators=(",", ":")),
        "-m",
        cfg["model"],
        "--effort",
        cfg["effort"],
        # Headless tools cannot prompt for approval, and a hung prompt is worse
        # than a deny: approve by name, then keep the surface small with the
        # tool allowlist (== Read) and the deny floor below.
        "--always-approve",
        "--tools",
        "Read",
        # Names are grok's own, and an unknown one aborts the run during
        # argument validation: `--deny NotebookEdit` (a Claude name) exits 1 with
        # "unsupported tool prefix" *before any model call* — how the first live
        # supervisor run failed on 2026-09-15 (then EnterWorktree, the same way).
        # Probed on grok 1.0.30 with a free sentinel (`--effort bogus`, which
        # fails validation too, so nothing is billed): every name below is
        # accepted; EnterWorktree and NotebookEdit are the only rejects found.
        # The allowlist above (--tools Read) is the primary restriction — these
        # denies are the second layer, and an unsupported name would cost the
        # whole run rather than tighten it.
        "--deny",
        "Write",
        "--deny",
        "Edit",
        "--deny",
        "MultiEdit",
        "--deny",
        "Notebook",
        "--deny",
        "WriteFile",
        "--deny",
        "ApplyPatch",
        "--deny",
        "Bash",
        "--deny",
        "BashOutput",
        "--deny",
        "Agent",
        "--deny",
        "Task",
        "--deny",
        "WebFetch",
        "--deny",
        "WebSearch",
        "--deny",
        "Skill",
        "--deny",
        "SlashCommand",
        "--deny",
        "Monitor",
        "--deny",
        "Workflow",
        "--deny",
        "CronCreate",
        "--deny",
        "TodoWrite",
        "--no-plan",
        "--max-turns",
        str(cfg["max_turns"]),
        "--no-auto-update",
    ]
    return args


# The reviewer's environment is built from nothing, never inherited: a secret
# that reaches this process (a unit Environment=, a shell export) must not reach
# the model call. GROK_HOME isolates config/auth; GROK_MEMORY=0 keeps reviews
# from accumulating cross-session memory.
#
# grok_env carries only the names in GROK_KEY_NAMES (see grok_env_keys): with an
# isolated GROK_HOME there is no auth.json, so XAI_API_KEY is the documented
# no-browser path and becomes the active credential. If a session token ever
# exists in that home it would take precedence over the key — do not mix the two.
def reviewer_env(grok_home: str, grok_env: dict | None = None) -> dict[str, str]:
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": str(HOME),
        "LANG": "C.UTF-8",
        "GROK_HOME": str(grok_home),
        "GROK_DISABLE_AUTOUPDATER": "1",
        "GROK_MEMORY": "0",
        "NO_COLOR": "1",
    }
    for name in GROK_KEY_NAMES:
        value = str((grok_env or {}).get(name) or "").strip()
        if value:
            env[name] = value
    return env


def parse_grok_result(stdout: str) -> dict:
    """Pull the structured verdict and its accounting out of grok's JSON. Pure."""
    text = str(stdout or "").strip()
    payload = None
    if text:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            for line in reversed(text.splitlines()):
                line = line.strip()
                if line.startswith("{") and line.endswith("}"):
                    try:
                        payload = json.loads(line)
                        break
                    except json.JSONDecodeError:
                        continue
    if not isinstance(payload, dict):
        return {"ok": False, "error": "no json envelope on stdout"}
    structured = payload.get("structuredOutput")
    if not isinstance(structured, dict):
        structured = payload.get("structured_output")
    if not isinstance(structured, dict):
        return {
            "ok": False,
            "error": f"no structured output (stopReason={payload.get('stopReason') or '?'})",
        }
    cost = payload.get("total_cost_usd")
    try:
        cost_usd = float(cost) if cost is not None else 0.0
    except (TypeError, ValueError):
        cost_usd = 0.0
    return {
        "ok": True,
        "error": "",
        "payload": structured,
        "cost_usd": cost_usd,
        "num_turns": payload.get("num_turns"),
        "stop_reason": payload.get("stopReason"),
        "session_id": payload.get("sessionId"),
    }
def dispatch_argv(cfg: dict, builtin: str, worker: str, text: str, dry_run: bool) -> list[str]:
    """argv for the bridge's guarded dispatch CLI. Pure.

    The bridge is the single implementation of the dispatch guards, so the
    supervisor shells out to it instead of re-implementing the registry, probe,
    pane and cwd checks.
    """
    argv = [cfg["bridge_bin"], "--dispatch", builtin, worker]
    if builtin == "goal":
        argv.append(text)
    if dry_run:
        argv.append("--dry-run")
    argv += ["--operator", "grok-supervisor"]
    if cfg.get("workers_file"):
        argv += ["--workers-file", str(cfg["workers_file"])]
    return argv


def format_review_post(worker: str, pos: dict, decision: dict, detail: str) -> str:
    """#lobby post for one reviewed stop. Pure."""
    was = f"{pos.get('state')}/{pos.get('reason')}"
    head = {
        "resume": f":rewind: Grokbot: {worker} stays on its unit (paused {was})",
        "goal": f":goal_net: Grokbot: new unit for {worker} (was {was})",
        "stop": f":checkered_flag: Grokbot: {worker} looks finished (was {was})",
        "escalate": f":raising_hand: Grokbot: {worker} needs a human (was {was})",
    }.get(decision.get("decision"), f":robot_face: Grokbot on {worker}")
    lines = [f"{head}."]
    if decision.get("decision") == "goal":
        lines.append(f"next goal: {decision.get('goal')}")
    lines.append(f"why: {decision.get('rationale')}")
    evidence = decision.get("evidence") or []
    if evidence:
        lines.append("evidence: " + " | ".join(evidence))
    if detail:
        lines.append(detail)
    return "\n".join(lines)


def format_escalation_post(worker: str, pos: dict, detail: str) -> str:
    return (
        f":raising_hand: {worker} is parked on a plan approval (ExitPlanMode) since "
        f"{pos.get('ts')}. Grokbot does not type into a permission dialog — "
        f"approve or deny it in the orca UI. {detail}"
    ).strip()


def format_alert_post(reason: str, detail: str) -> str:
    return f":warning: goal supervisor skipped: {reason} ({detail})"


# ---------------------------------------------------------------------------
# io
# ---------------------------------------------------------------------------


def run_capture(argv: list[str], timeout: int, env: dict | None = None, cwd: str | None = None) -> dict:
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=env,
            cwd=cwd,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "ok": False,
            "stdout": "",
            "stderr": "",
            "error": f"{type(exc).__name__}: {exc}",
            "errno": getattr(exc, "errno", None),
            "code": -1,
        }
    return {
        "ok": proc.returncode == 0,
        "stdout": proc.stdout or "",
        "stderr": proc.stderr or "",
        "error": "" if proc.returncode == 0 else f"exit={proc.returncode}",
        "errno": None,
        "code": proc.returncode,
    }


def missing_binary(result: dict) -> bool:
    """True when the spawn failed because the command is not installed.

    Python reports this as FileNotFoundError/errno 2, node as ENOENT; accept both
    so a dev checkout without the installed binaries still falls back to the repo
    scripts instead of silently treating every worker as unknown.
    """
    if result.get("errno") == 2:
        return True
    error = str(result.get("error") or "")
    return "ENOENT" in error or "FileNotFoundError" in error


def log_line(path: Path, text: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(f"{datetime.now().astimezone().isoformat()} {text}\n")
    except OSError:
        pass


def load_state(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    for key in ("reviewed", "escalated", "workers", "alerts"):
        if not isinstance(payload.get(key), dict):
            payload[key] = {}
    if not isinstance(payload.get("day"), dict):
        payload["day"] = {"date": "", "dispatches": 0, "cost_usd": 0.0}
    return payload


def save_state(path: Path, state: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        trimmed = dict(state)
        for key in ("reviewed", "escalated", "alerts"):
            items = state.get(key) or {}
            trimmed[key] = dict(list(items.items())[-STATE_KEEP:])
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(trimmed, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        log_line(LOG_FILE, f"state_write_failed {exc}")


def mark_handled(state: dict, key: str, now: float, kind: str) -> None:
    bucket = "escalated" if kind == "escalated" else "reviewed"
    state.setdefault(bucket, {})[key] = now


def record_dispatch(state: dict, worker: str, now: float, fingerprint: str) -> None:
    """Counters for the caps, and the fingerprints for the repeat guard.

    Spend is NOT counted here: record_cost already booked the call that produced
    this decision, and counting it twice would halve the effective USD cap.
    """
    day = day_key(now)
    entry = state.get("day")
    if not isinstance(entry, dict) or entry.get("date") != day:
        entry = {"date": day, "dispatches": 0, "cost_usd": 0.0}
    entry["dispatches"] = int(entry.get("dispatches") or 0) + 1
    state["day"] = entry

    worker_state = state.setdefault("workers", {}).get(worker)
    if not same_day(worker_state, day):
        worker_state = {"day": day, "count": 0, "goals": []}
    worker_state["count"] = int(worker_state.get("count") or 0) + 1
    worker_state["last_at"] = now
    if fingerprint:
        goals = worker_state.get("goals")
        goals = goals if isinstance(goals, list) else []
        worker_state["goals"] = ([fingerprint] + [str(g) for g in goals])[:5]
    state["workers"][worker] = worker_state


def record_cost(state: dict, now: float, cost_usd: float) -> None:
    """Spend is counted even when the decision turns out not to dispatch."""
    day = day_key(now)
    entry = state.get("day")
    if not isinstance(entry, dict) or entry.get("date") != day:
        entry = {"date": day, "dispatches": 0, "cost_usd": 0.0}
    entry["cost_usd"] = round(float(entry.get("cost_usd") or 0.0) + float(cost_usd or 0.0), 6)
    state["day"] = entry


def pick_path(box: Path, repo: Path, override: str | None = None) -> Path:
    """First existing candidate, else the installed (box) path.

    Returning the box path when nothing exists keeps an error message pointing at
    the installed location instead of a repo-relative guess — `REPO` is derived
    from the script's own directory, and for an installed copy that guess is
    nonsense (a verification run on the Spectre resolved the prompt to
    `/config/goal-supervisor-prompt.md`). A dev checkout still wins when present.
    """
    if override:
        return Path(override).expanduser()
    for candidate in (box, repo):
        if candidate.is_file():
            return candidate
    return box


def load_workers(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"goal-supervisor: workers file unreadable: {path}: {exc}") from exc
    workers = payload.get("workers")
    if not isinstance(workers, dict) or not workers:
        raise SystemExit(f"goal-supervisor: no workers in {path}")
    for name, entry in workers.items():
        if not isinstance(entry, dict) or not str(entry.get("cwd") or "").strip():
            raise SystemExit(f"goal-supervisor: worker {name!r} has no cwd in {path}")
    return workers


def load_prompt(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SystemExit(f"goal-supervisor: prompt unreadable: {path}: {exc}") from exc
    if "{{WORKER}}" not in text:
        raise SystemExit(f"goal-supervisor: prompt template lacks placeholders: {path}")
    return text


def load_env_file(path: Path) -> dict:
    """KEY=VALUE lines; comments and blanks skipped. Quotes are not stripped.

    Same shape as slack.env (the repo's one env-file convention), so an operator
    who has written one already knows this one.
    """
    out: dict = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key:
            out[key] = value.strip()
    return out


def grok_env_keys(path: Path) -> dict:
    """Only GROK_KEY_NAMES out of the env file — never the whole file.

    A grok.env with extra keys (a proxy token, a comment block) must not become
    the reviewer's environment; this is the whitelist's gate.
    """
    env = load_env_file(path)
    return {name: env[name] for name in GROK_KEY_NAMES if str(env.get(name) or "").strip()}


def probe_worker(cfg: dict, name: str) -> dict:
    for parent in (HERE, Path("/usr/local/lib/spectre-worker-state")):
        if (parent / "worker_state").is_dir() and str(parent) not in sys.path:
            sys.path.insert(0, str(parent))
            break
    from worker_state.client import StateClient
    from worker_state.view import snapshot_to_pos

    snap = StateClient().snapshot(name)
    if snap.get("debug", {}).get("unavailable") or snap.get("ok") is False:
        return {
            "ok": False,
            "error": str(snap.get("reason") or "state api unavailable"),
            "payload": {},
        }
    return {"ok": True, "error": "", "payload": snapshot_to_pos(snap)}


def git_evidence(cwd: str, limit_chars: int = REPO_EVIDENCE_CHARS) -> str:
    """Read-only git facts for the worker's checkout. Never writes."""
    if not Path(cwd).is_dir():
        return f"(cwd missing: {cwd})"
    parts = []
    log = run_capture(["git", "-C", cwd, "log", "--oneline", "-5"], 15)
    if log["ok"]:
        parts.append("git log --oneline -5:\n" + log["stdout"].strip())
    else:
        parts.append(f"git log unavailable ({log['error']})")
    status = run_capture(["git", "-C", cwd, "status", "--short"], 15)
    if status["ok"]:
        dirty = status["stdout"].strip()
        parts.append("git status --short:\n" + (dirty or "(clean)"))
    return "\n\n".join(parts)[:limit_chars]


def session_tail(segment: str, limit_chars: int = SESSION_TAIL_CHARS) -> str:
    """Last records of the worker's own session jsonl (read-only, truncated)."""
    if not segment:
        return "(no session segment found)"
    try:
        with Path(segment).open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 64 * 1024))
            chunk = fh.read().decode("utf-8", "replace")
    except OSError as exc:
        return f"(session tail unreadable: {exc})"
    lines = [line for line in chunk.splitlines() if line.strip()]
    return "\n".join(lines[-SESSION_TAIL_RECORDS:])[:limit_chars]


def prior_actions(state: dict, worker: str, limit: int = 3) -> str:
    worker_state = state.get("workers", {}).get(worker) or {}
    goals = worker_state.get("goals")
    if not isinstance(goals, list) or not goals:
        return "(none)"
    last_at = worker_state.get("last_at")
    when = ""
    if isinstance(last_at, (int, float)):
        stamp = datetime.fromtimestamp(last_at, tz=timezone.utc).isoformat()
        when = f" (last dispatch {stamp})"
    return f"goal fingerprints, newest first{when}: " + ", ".join(str(g) for g in goals[:limit])


def classify_grok_failure(text: str) -> str:
    """Map a grok failure to a stable reason. Pure; never echoes the text."""
    low = str(text or "").lower()
    if "402" in low or "balance exhausted" in low or "payment required" in low:
        return "quota_exhausted"
    # "Incorrect API key provided" (probed 2026-09-15: the api_key pin reached the
    # API and the server refused the key) has no "auth" substring, so it needs its
    # own branch or it would surface as a generic grok_failed.
    if "incorrect api key" in low or "invalid api key" in low or "api key provided" in low:
        return "auth_failed"
    if "401" in low or "unauthorized" in low or "not logged in" in low or "auth" in low:
        return "auth_failed"
    if "enoent" in low or "no such file" in low:
        return "grok_missing"
    return "grok_failed"


def call_grok(cfg: dict, prompt: str) -> dict:
    """One headless review call -> {ok, error, payload, cost_usd, ...}."""
    if not Path(cfg["grok_bin"]).is_file():
        return {"ok": False, "error": "grok_missing", "payload": {}, "cost_usd": 0.0}
    review_cwd = Path(cfg.get("review_cwd") or REVIEW_CWD)
    try:
        review_cwd.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    result = run_capture(
        [cfg["grok_bin"], *build_grok_args(prompt, cfg)],
        cfg["timeout_sec"],
        env=reviewer_env(str(cfg["grok_home"]), cfg.get("grok_env") or {}),
        # Neutral cwd on purpose: running inside the worker's repo would load
        # that repo's AGENTS.md and settings as *rules* for the reviewer.
        # Evidence is passed as text in the prompt instead.
        cwd=str(review_cwd),
    )
    if not result["ok"] and not result["stdout"].strip():
        reason = classify_grok_failure(result["stderr"] or result["error"])
        # Keep a bounded, single-line tail in the log (never in the Slack alert):
        # a CLI-level rejection like "unsupported tool prefix" is otherwise
        # invisible, and the first live run on 2026-09-15 was diagnosed from this.
        tail = one_line_log(result["stderr"] or result["error"])
        log_line(LOG_FILE, f"grok_failed reason={reason} code={result['code']} detail={tail}")
        return {"ok": False, "error": reason, "payload": {}, "cost_usd": 0.0, "detail": tail}
    parsed = parse_grok_result(result["stdout"])
    if not parsed["ok"]:
        reason = classify_grok_failure(result["stderr"])
        log_line(LOG_FILE, f"grok_result_invalid reason={reason} detail={parsed['error']}")
        return {"ok": False, "error": reason, "payload": {}, "cost_usd": parsed["cost_usd"]}
    return parsed


def dispatch(cfg: dict, builtin: str, worker: str, text: str, dry_run: bool) -> dict:
    """Shell out to the bridge's guarded dispatch and return its JSON verdict."""
    argv = dispatch_argv(cfg, builtin, worker, text, dry_run)
    if argv[0] == BRIDGE_BIN and not Path(BRIDGE_BIN).is_file():
        argv = ["node", str(REPO_BRIDGE), *argv[1:]]
    result = run_capture(argv, 60)
    verdict: dict = {}
    for line in reversed([line for line in result["stdout"].splitlines() if line.strip()]):
        try:
            verdict = json.loads(line)
            break
        except json.JSONDecodeError:
            continue
    if not isinstance(verdict, dict) or not verdict:
        verdict = {
            "ok": False,
            "evt": "dispatch_no_verdict",
            "detail": (result["stderr"].strip() or result["error"])[:200],
        }
    return verdict


def notify(cfg: dict, channel: str, text: str, recovery: bool = False) -> tuple[bool, str]:
    """Post through spectre-slack-notify. 'disabled' (no slack.env yet) is not
    a failure, but it must not consume state — the notice fires once configured."""
    argv = [cfg["notify_bin"], "--agent", cfg["agent"], "--channel", channel, "--text", text]
    if recovery:
        argv.append("--recovery")
    if not Path(argv[0]).is_file():
        argv = [sys.executable, str(REPO_NOTIFY), *argv[1:]]
    result = run_capture(argv, 30)
    if "disabled" in result["stdout"]:
        return False, "disabled"
    if not result["ok"]:
        return False, (result["stderr"].strip() or result["error"])[:200]
    return True, result["stdout"].strip()


def alert_throttled(state: dict, key: str, now: float, cooldown: int = ALERT_COOLDOWN_SEC) -> bool:
    """True when this alert may be posted (once per cooldown). Mutates state."""
    last = state.setdefault("alerts", {}).get(key)
    if isinstance(last, (int, float)) and 0 <= now - last < cooldown:
        return False
    state["alerts"][key] = now
    return True


def worker_block(name: str, entry: dict, pos: dict) -> str:
    """The WORKER section of the review prompt. Pure."""
    lines = [
        f"worker: {name}",
        f"cwd: {entry.get('cwd')}",
        f"tmux target: {entry.get('tmux') or '(none — orca native terminal, no injection path)'}",
        f"stop: {pos.get('state')}/{pos.get('reason')}",
        f"last record ts: {pos.get('ts')} ({pos.get('age_sec')}s before this scan)",
        f"turn_id: {pos.get('turn_id')}",
    ]
    if pos.get("num_turns") is not None:
        lines.append(f"num_turns: {pos.get('num_turns')}")
    if pos.get("segment"):
        lines.append(f"session segment: {pos.get('segment')}")
    return "\n".join(str(line) for line in lines)


def dispatch_detail(verdict: dict) -> str:
    """One line about what the guarded dispatch did. Pure."""
    evt = str(verdict.get("evt") or "?")
    detail = str(verdict.get("detail") or "").strip()
    if verdict.get("ok"):
        return f"dispatch: {evt} (tmux {verdict.get('tmux')}, chars {verdict.get('chars')})"
    if detail:
        return f"dispatch: {evt} — {detail}"
    return f"dispatch: {evt}"


# Refusals that are a policy answer, not a transient failure: retrying the same
# stop would loop forever, so they settle the event (with a Slack notice).
FINAL_DISPATCH_EVENTS = frozenset(
    {
        "dispatch_usage",
        "dispatch_registry_failed",
        "dispatch_unknown_worker",
        "dispatch_no_injection_path",
        "dispatch_clause_failed",
        "dispatch_refused",
        "dispatch_pane_refused",
    }
)


def handle_escalation(cfg: dict, state: dict, name: str, pos: dict, key: str, now: float) -> bool:
    """plan_gate: report, never type. Marks the event handled. Mutates state."""
    text = format_escalation_post(name, pos, "Grokbot will not type into a permission dialog.")
    delivered, detail = notify(cfg, cfg["channel"], text)
    log_line(LOG_FILE, f"escalated worker={name} reason=plan_gate delivered={delivered} ({detail})")
    if delivered or detail == "disabled":
        mark_handled(state, key or f"gate:{pos.get('ts')}", now, "escalated")
    return delivered


def review_and_act(cfg: dict, workers: dict, state: dict, name: str, pos: dict, key: str, now: float) -> bool:
    """One review call plus one guarded dispatch. Mutates state; returns acted."""
    entry = workers[name]
    values = {
        "WORKER": worker_block(name, entry, pos),
        "PROBE_JSON": json.dumps(pos, indent=2, sort_keys=True),
        "REPO_EVIDENCE": git_evidence(str(entry["cwd"])),
        "SESSION_TAIL": session_tail(str(pos.get("segment") or "")),
        "PRIOR_ACTIONS": prior_actions(state, name),
    }
    try:
        prompt = render_prompt(load_prompt(cfg["prompt_file"]), values)[:PROMPT_MAX]
    except ValueError as exc:
        log_line(LOG_FILE, f"prompt_render_failed worker={name}: {exc}")
        mark_handled(state, key, now, "reviewed")
        return False

    result = call_grok(cfg, prompt)
    record_cost(state, now, float(result.get("cost_usd") or 0.0))
    if not result["ok"]:
        reason = str(result["error"] or "grok_failed")
        alerted = ""
        if alert_throttled(state, f"grok:{reason}", now):
            delivered, detail = notify(
                cfg, cfg["alerts_channel"], format_alert_post(reason, f"worker={name}")
            )
            alerted = f" alerted={delivered} ({detail})"
        # Deliberately NOT marked handled: a quota/auth/model failure has to
        # retry once the cause clears, and the next tick rebuilds the prompt.
        log_line(LOG_FILE, f"review_failed worker={name} reason={reason}{alerted}")
        return False

    ok, problem = validate_decision(result["payload"], cfg["goal_max"])
    if not ok:
        # A malformed decision from a schema-constrained call is final for this
        # stop: retrying would buy the same misbehaviour for another call.
        mark_handled(state, key, now, "reviewed")
        notify(cfg, cfg["channel"], f":warning: Grokbot discarded its own decision on `{name}`: {problem}")
        log_line(LOG_FILE, f"decision_invalid worker={name} problem={problem}")
        return False

    decision = decision_of(result["payload"])
    worker_state = state.get("workers", {}).get(name) or {}
    fingerprint = ""

    if decision["decision"] in ACTING_DECISIONS:
        builtin = "resume" if decision["decision"] == "resume" else "goal"
        if builtin == "goal":
            fingerprint = goal_fingerprint(decision["goal"])
            if repeats_last_goal(fingerprint, worker_state):
                mark_handled(state, key, now, "reviewed")
                notify(
                    cfg,
                    cfg["channel"],
                    f":recycle: Grokbot proposed the same goal again for `{name}` "
                    f"(fingerprint {fingerprint}) — not dispatched.",
                )
                log_line(LOG_FILE, f"repeat_goal worker={name} fingerprint={fingerprint}")
                return False
        verdict = dispatch(cfg, builtin, name, decision["goal"], dry_run=False)
        delivered, post_detail = notify(
            cfg, cfg["channel"], format_review_post(name, pos, decision, dispatch_detail(verdict))
        )
        if verdict.get("ok"):
            record_dispatch(state, name, now, fingerprint)
            mark_handled(state, key, now, "reviewed")
        elif str(verdict.get("evt")) in FINAL_DISPATCH_EVENTS:
            # The guards answered; this stop gets no second try.
            mark_handled(state, key, now, "reviewed")
        event = "dispatch_ok" if verdict.get("ok") else str(verdict.get("evt"))
        log_line(
            LOG_FILE,
            f"reviewed worker={name} decision={decision['decision']} {event} "
            f"cost_usd={result.get('cost_usd')} posted={delivered} ({post_detail})",
        )
        return bool(verdict.get("ok"))

    mark_handled(state, key, now, "reviewed")
    delivered, post_detail = notify(
        cfg, cfg["channel"], format_review_post(name, pos, decision, "dispatch: none (no action)")
    )
    log_line(
        LOG_FILE,
        f"reviewed worker={name} decision={decision['decision']} no_dispatch "
        f"cost_usd={result.get('cost_usd')} posted={delivered} ({post_detail})",
    )
    return delivered
def cmd_probe(cfg: dict, workers: dict, name: str) -> int:
    """Classify one worker and show the plan. No model call, no dispatch, no post."""
    if name not in workers:
        print(json.dumps({"worker": name, "error": "unknown worker", "known": sorted(workers)}))
        return 1
    probe = probe_worker(cfg, name)
    if not probe["ok"]:
        print(json.dumps({"worker": name, "error": probe["error"]}))
        return 1
    pos = probe["payload"]
    state = load_state(cfg["state_file"])
    worker_state = state.get("workers", {}).get(name) or {}
    verdict = gate(pos, worker_state, cfg, state, now_ts())
    print(
        json.dumps(
            {
                "worker": name,
                "position": pos,
                "gate": verdict,
                "review_key": review_key(pos),
                "workers_file": str(cfg["workers_file"]),
                "prompt_file": str(cfg["prompt_file"]),
                "grok_bin": cfg["grok_bin"],
                "grok_home": str(cfg["grok_home"]),
                # Names only: a probe must never print a credential value.
                "grok_env_keys": sorted(cfg.get("grok_env") or {}),
                "grok_env_file": str(cfg.get("grok_env_file") or ""),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def cmd_scan(cfg: dict, workers: dict, now: float, dry_run: bool) -> int:
    state = load_state(cfg["state_file"])
    positions: dict[str, dict] = {}
    actions: list[dict] = []
    acted = 0

    for name in sorted(workers):
        if cfg.get("worker") and name != cfg["worker"]:
            continue
        probe = probe_worker(cfg, name)
        if not probe["ok"]:
            log_line(LOG_FILE, f"probe_failed worker={name} error={probe['error']}")
            positions[name] = {"state": "unknown", "error": probe["error"]}
            continue
        pos = probe["payload"]
        positions[name] = pos
        worker_state = state.get("workers", {}).get(name) or {}
        verdict = gate(pos, worker_state, cfg, state, now)
        if verdict["action"] == "skip":
            continue
        actions.append({"worker": name, "gate": verdict, "position": pos})
        if dry_run:
            continue
        if verdict["action"] == "escalate":
            if handle_escalation(cfg, state, name, pos, verdict["key"], now):
                acted += 1
            continue
        if review_and_act(cfg, workers, state, name, pos, verdict["key"], now):
            acted += 1

    if dry_run:
        print(json.dumps({"positions": positions, "actions": actions}, indent=2, sort_keys=True))
        return 0

    save_state(cfg["state_file"], state)
    log_line(
        LOG_FILE,
        f"scan workers={len(positions)} actions={len(actions)} acted={acted} "
        f"spend_today={cost_today(state, now)}",
    )
    if acted:
        print(f"goal-supervisor: {acted} action(s)")
    return 0
def now_ts() -> float:
    return datetime.now().timestamp()


def truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in TRUTHY


def build_cfg(args: argparse.Namespace) -> dict:
    return {
        "worker": (args.worker or "").strip().lower() or None,
        "workers_file": pick_path(BOX_WORKERS_FILE, REPO_WORKERS_FILE, args.workers_file),
        "prompt_file": pick_path(BOX_PROMPT_FILE, REPO_PROMPT_FILE, args.prompt_file),
        "state_file": Path(args.state_file).expanduser(),
        "grok_bin": str(Path(args.grok_bin).expanduser()),
        "grok_home": Path(args.grok_home).expanduser(),
        "grok_env": grok_env_keys(Path(args.grok_env_file).expanduser()),
        "grok_env_file": Path(args.grok_env_file).expanduser(),
        "review_cwd": Path(args.review_cwd).expanduser(),
        "bridge_bin": args.bridge_bin,
        "watcher_bin": args.watcher_bin,
        "notify_bin": args.notify_bin,
        "agent": args.agent,
        "channel": args.channel,
        "alerts_channel": args.alerts_channel,
        "model": args.model,
        "effort": args.effort,
        "max_turns": args.max_turns,
        "timeout_sec": args.timeout_sec,
        "goal_max": args.goal_max,
        "max_per_day": args.max_per_day,
        "max_per_worker_day": args.max_per_worker_day,
        "cooldown_sec": args.cooldown_sec,
        "max_cost_usd_day": args.max_cost_usd_day,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="goal-supervisor",
        description="Grokbot: review stopped worker goals and dispatch the next one",
    )
    parser.add_argument("--scan", action="store_true", help="scan all workers (default)")
    parser.add_argument("--probe", metavar="WORKER", help="classify one worker and exit")
    parser.add_argument("--worker", help="restrict a scan to one worker")
    parser.add_argument("--dry-run", action="store_true", help="print the plan; no call, no post, no state")
    parser.add_argument("--enable", action="store_true", help="run with the kill switch off (manual use)")
    parser.add_argument("--max-per-day", type=int, default=20)
    parser.add_argument("--max-per-worker-day", type=int, default=8)
    parser.add_argument("--cooldown-sec", type=int, default=900)
    parser.add_argument("--max-cost-usd-day", type=float, default=1.0)
    parser.add_argument("--goal-max", type=int, default=GOAL_MAX)
    parser.add_argument("--model", default="grok-4.6")
    parser.add_argument("--effort", default="low", help="xhigh|high|medium|low (the grok-4.6 menu)")
    parser.add_argument("--max-turns", type=int, default=6)
    parser.add_argument("--timeout-sec", type=int, default=GROK_TIMEOUT_SEC)
    parser.add_argument("--agent", default="grok", help="Slack identity for reports")
    parser.add_argument("--channel", default="lobby", help="Slack channel for reviews")
    parser.add_argument("--alerts-channel", default="fleet", help="Slack channel for supervisor alerts")
    parser.add_argument("--workers-file", default=None)
    parser.add_argument("--prompt-file", default=None)
    parser.add_argument("--state-file", default=str(STATE_FILE))
    parser.add_argument("--grok-bin", default=str(DEFAULT_GROK_BIN))
    parser.add_argument("--grok-home", default=str(DEFAULT_GROK_HOME))
    parser.add_argument(
        "--grok-env-file",
        default=str(GROK_ENV_FILE),
        help="0600 KEY=VALUE file whose only XAI_API_KEY/GROK_CODE_XAI_API_KEY values reach the reviewer",
    )
    parser.add_argument("--review-cwd", default=str(REVIEW_CWD), help="neutral cwd for the reviewer")
    parser.add_argument("--bridge-bin", default=BRIDGE_BIN)
    parser.add_argument("--watcher-bin", default=WATCHER_BIN)
    parser.add_argument("--notify-bin", default=NOTIFY_BIN)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = build_cfg(args)
    workers = load_workers(cfg["workers_file"])

    if args.probe:
        return cmd_probe(cfg, workers, str(args.probe).strip().lower())

    if (
        not args.dry_run
        and not args.enable
        and not truthy(os.environ.get("SPECTRE_GOAL_SUPERVISOR"))
    ):
        # Installed off. The unit sets SPECTRE_GOAL_SUPERVISOR=1; anything else
        # (a missing env line included) means no model call and no dispatch.
        print("goal-supervisor: disabled (SPECTRE_GOAL_SUPERVISOR is not set)")
        return 0

    return cmd_scan(cfg, workers, now_ts(), args.dry_run)


if __name__ == "__main__":
    sys.exit(main())