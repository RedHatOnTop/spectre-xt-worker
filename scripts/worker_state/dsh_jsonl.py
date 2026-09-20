"""Map DeepSeek Harness session.v3.jsonl.zstd records to evidence. Adapter only."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from .types import iso_from

DSH_HOME = Path.home() / ".local/share/fullmoon-dsh"
ZSTD_TIMEOUT_SEC = 1.0
ZSTD_SIZE_CAP = 8 * 1024 * 1024
_LAST_MTIME: dict[str, float] = {}
_LAST_SEQ: dict[str, int] = {}


def dsh_session_dir(cwd: str) -> str:
    """Harness encoding: -- + absolute-path-with-dashes + --."""
    raw = str(cwd or "")
    abs_path = raw if raw.startswith("/") else str(Path(raw).resolve())
    dashed = abs_path.rstrip("/").replace("/", "-")
    return f"--{dashed}--"


def map_dsh_record(record: dict[str, Any]) -> dict[str, Any] | None:
    rtype = str(record.get("type") or "")
    data = record.get("data") if isinstance(record.get("data"), dict) else record
    payload = dict(data) if isinstance(data, dict) else {}
    if rtype == "tool/call":
        return {"kind": "tool.started", "payload": payload}
    if rtype == "tool/result":
        failed = bool(payload.get("isError") or payload.get("is_error"))
        return {"kind": "tool.failed" if failed else "tool.completed", "payload": payload}
    if rtype in {"turn/start", "request/header"}:
        return {"kind": "model.request.started", "payload": payload}
    if rtype == "turn/end":
        return {"kind": "model.request.completed", "payload": payload}
    return None


def _record_time(record: dict[str, Any], now: float) -> str:
    raw = record.get("time") or record.get("createdAt") or record.get("created_at")
    if isinstance(raw, (int, float)):
        ts = float(raw)
        if ts > 1e12:
            ts = ts / 1000.0
        return iso_from(ts)
    if isinstance(raw, str) and raw:
        return raw
    return iso_from(now)


def evidence_from_dsh_record(
    worker: str,
    session_id: str,
    record: dict[str, Any],
    seq: int,
    now: float,
) -> dict[str, Any] | None:
    mapped = map_dsh_record(record)
    if mapped is None:
        return None
    turn = record.get("turn") or (record.get("data") or {}).get("turn")
    return {
        "event_id": f"dsh-{worker}-{session_id}-{seq}",
        "worker": worker,
        "source": "dsh_jsonl",
        "source_timestamp": _record_time(record, now),
        "turn_id": f"{session_id}-{turn}" if turn is not None else session_id,
        **mapped,
    }


def decompress_zstd(path: Path) -> str | None:
    try:
        size = path.stat().st_size
    except OSError:
        return None
    if size > ZSTD_SIZE_CAP:
        return None
    try:
        proc = subprocess.run(
            ["zstd", "-dc", str(path)],
            capture_output=True,
            timeout=ZSTD_TIMEOUT_SEC,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.decode("utf-8", "replace")


def collect_dsh_evidence(
    worker: str,
    cwd: str,
    now: float,
    *,
    dsh_home: Path | None = None,
    injected_at: float | None = None,
) -> list[dict[str, Any]]:
    root = (dsh_home or DSH_HOME) / "sessions" / dsh_session_dir(cwd)
    if not root.is_dir():
        return []
    events: list[dict[str, Any]] = []
    for session_dir in sorted(root.iterdir()):
        if not session_dir.is_dir():
            continue
        zpath = session_dir / "session.v3.jsonl.zstd"
        jsonl = session_dir / "session.v3.jsonl"
        path = zpath if zpath.is_file() else jsonl
        if not path.is_file():
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        key = str(path)
        if _LAST_MTIME.get(key) == mtime:
            continue
        if path.suffix == ".zstd" or path.name.endswith(".zstd"):
            text = decompress_zstd(path)
        else:
            try:
                if path.stat().st_size > ZSTD_SIZE_CAP:
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
        if text is None:
            continue
        _LAST_MTIME[key] = mtime
        session_id = session_dir.name
        last = _LAST_SEQ.get(session_id, -1)
        for seq, line in enumerate(text.splitlines()):
            if seq <= last:
                continue
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            rec_ts = record.get("time") or record.get("createdAt")
            if injected_at is not None and isinstance(rec_ts, (int, float)):
                ts = float(rec_ts) / 1000.0 if rec_ts > 1e12 else float(rec_ts)
                if ts < injected_at:
                    continue
            item = evidence_from_dsh_record(worker, session_id, record, seq, now)
            if item:
                events.append(item)
            last = seq
        _LAST_SEQ[session_id] = last
    return events


def dsh_exit_event(
    worker: str,
    dispatch_id: str,
    exit_code: int,
    snapshot: dict[str, Any],
    now: float,
) -> dict[str, Any]:
    goal = snapshot.get("goal") or {}
    kind = "goal.completed" if exit_code == 0 else "goal.failed"
    return {
        "event_id": f"dsh-exit-{worker}-{dispatch_id}",
        "worker": worker,
        "kind": kind,
        "source": "dsh_exit",
        "source_timestamp": iso_from(now),
        "goal_id": goal.get("goal_id"),
        "turn_id": goal.get("turn_id"),
        "dispatch_id": goal.get("dispatch_id") or dispatch_id,
        "attempt_id": goal.get("attempt_id"),
        "payload": {"exit_code": exit_code, "dispatch_id": dispatch_id},
    }


def parse_exit_file(path: Path) -> int | None:
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not text.isdigit():
        return None
    value = int(text)
    if 0 <= value <= 255:
        return value
    return None
