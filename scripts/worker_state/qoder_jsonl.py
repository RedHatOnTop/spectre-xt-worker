"""Map qoder session JSONL records to evidence. Adapter, not a classifier."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from .types import COMPLETE_REASONS

TAIL_BYTES = 256 * 1024
BOX_WORKERS_FILE = Path("/usr/local/share/remote-agent/qoder-workers.json")
REPO_WORKERS_FILE = Path(__file__).resolve().parents[2] / "config" / "qoder-workers.json"
SESSIONS_DIR = Path.home() / ".qoder/logs/sessions"


def session_dir_name(cwd: str) -> str:
    return cwd.rstrip("/").replace("/", "-")


def workers_path() -> Path:
    override = os.environ.get("QODER_WORKERS_FILE", "").strip()
    if override:
        return Path(override)
    if BOX_WORKERS_FILE.is_file():
        return BOX_WORKERS_FILE
    return REPO_WORKERS_FILE


def load_workers(path: Path | None = None) -> dict[str, dict]:
    target = path or workers_path()
    payload = json.loads(target.read_text(encoding="utf-8"))
    workers = payload.get("workers")
    if not isinstance(workers, dict) or not workers:
        raise ValueError(f"no workers in {target}")
    return workers


def newest_segment(sessions_root: Path, cwd: str) -> Path | None:
    base = sessions_root / session_dir_name(cwd)
    if not base.is_dir():
        return None
    best: tuple[float, Path] | None = None
    for path in base.glob("*/segments/*.jsonl"):
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if best is None or mtime > best[0]:
            best = (mtime, path)
    return best[1] if best else None


def tail_records(path: Path, count: int = 50) -> list[dict]:
    with path.open("rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        fh.seek(max(0, size - TAIL_BYTES))
        chunk = fh.read().decode("utf-8", "replace")
    lines = chunk.splitlines()
    if size > TAIL_BYTES and lines:
        lines = lines[1:]
    out: list[dict] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out[-count:]


def event_id_for(path: Path, record: dict) -> str:
    raw = json.dumps(record, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(f"{path}:{raw}".encode()).hexdigest()[:32]
    return f"jsonl-{digest}"


def map_record(record: dict[str, Any]) -> dict[str, Any] | None:
    """Return evidence fields (no worker/event_id) or None to skip."""
    rtype = str(record.get("type") or "")
    if not rtype:
        return None
    data = record.get("data") if isinstance(record.get("data"), dict) else {}
    turn_id = record.get("turn_id") or None
    ts = record.get("ts") or None
    base = {
        "source": "session_jsonl",
        "source_timestamp": ts,
        "turn_id": str(turn_id) if turn_id else None,
        "payload": dict(data),
    }
    if rtype == "turn.finished":
        reason = str(data.get("reason") or "")
        if reason == "max_turns":
            return {
                **base,
                "kind": "goal.parked",
                "payload": {
                    **data,
                    "park_reason": "goal_budget",
                    "reason": reason,
                },
            }
        if reason in COMPLETE_REASONS:
            return {**base, "kind": "goal.completed", "payload": {**data, "reason": reason}}
        return {**base, "kind": "turn.ended", "payload": {**data, "reason": reason}}
    if rtype == "hook.finished":
        hook = str(data.get("hook_name") or "")
        if hook.endswith("PreToolUse:ExitPlanMode"):
            return {
                **base,
                "kind": "goal.parked",
                "payload": {**data, "park_reason": "plan_gate", "hook_name": hook},
            }
        return {**base, "kind": "hook.finished", "payload": {**data, "hook_name": hook}}
    if rtype == "permission.resolved":
        return {**base, "kind": "permission.resolved", "payload": data}
    if rtype == "input.prompt.received":
        text = str(data.get("prompt") or data.get("text") or "").strip()
        if text == "/goal resume" or text.startswith("/goal resume"):
            return {**base, "kind": "input.resume", "payload": {**data, "text": text}}
        if text.startswith("/goal"):
            return {**base, "kind": "input.goal", "payload": {**data, "text": text}}
        return {**base, "kind": "input.prompt.received", "payload": {**data, "text": text}}
    if rtype == "session.phase.finished":
        # A per-iteration sub-phase, not a turn boundary. Every observed record
        # carries phase=input.attachments.collect, which happens *inside* a
        # live /goal loop: mapping it to turn.ended idled the worker between
        # iterations and produced dangerous_false_idle rows on zzbrush
        # (2026-09-19, old probe still active, can_dispatch_goal flipped true).
        # The turn/goal boundary is turn.finished.
        return None
    if rtype == "error":
        return {**base, "kind": "turn.ended", "payload": {**data, "reason": rtype}}
    if rtype == "tool.requested":
        return {**base, "kind": "tool.started", "payload": data}
    if rtype in {
        "model.request.started",
        "model.request.completed",
        "model.request.failed",
        "model.request.first_token",
        "model.response.completed",
        "tool.started",
        "tool.completed",
        "tool.failed",
        "heartbeat",
    }:
        kind = "model.request.completed" if rtype == "model.response.completed" else rtype
        return {**base, "kind": kind, "payload": data}
    return None


def evidence_from_record(worker: str, path: Path, record: dict) -> dict[str, Any] | None:
    mapped = map_record(record)
    if mapped is None:
        return None
    return {
        "event_id": event_id_for(path, record),
        "worker": worker,
        **mapped,
    }


def collect_worker_evidence(
    worker: str,
    entry: dict,
    sessions_root: Path,
) -> list[dict[str, Any]]:
    cwd = str(entry.get("cwd") or "")
    segment = newest_segment(sessions_root, cwd) if cwd else None
    if segment is None:
        return []
    try:
        records = tail_records(segment)
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for record in records:
        item = evidence_from_record(worker, segment, record)
        if item:
            out.append(item)
    return out
