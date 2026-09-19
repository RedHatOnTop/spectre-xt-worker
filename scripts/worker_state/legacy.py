"""Last-record classifier kept only for shadow comparison. Not a live authority."""
from __future__ import annotations

from datetime import datetime, timezone

ACTIVE_FRESH_SEC = 600


def _age_seconds(ts: str, now: float) -> float | None:
    try:
        parsed = datetime.fromisoformat(ts)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return now - parsed.timestamp()


def position(records: list[dict], now: float) -> dict:
    """The 2026-09-14 last-record probe. Shadow only — do not dispatch from this."""
    if not records:
        return {"state": "unknown"}
    last = records[-1]
    rtype = str(last.get("type") or "")
    ts = str(last.get("ts") or "")
    age = _age_seconds(ts, now)
    data = last.get("data") if isinstance(last.get("data"), dict) else {}

    if rtype == "turn.finished":
        reason = str(data.get("reason") or "")
        out = {
            "state": "idle",
            "reason": reason,
            "ts": ts,
            "age_sec": age,
            "turn_id": str(last.get("turn_id") or ""),
        }
        if reason == "max_turns":
            out["state"] = "parked"
            out["reason"] = "goal_budget"
            out["num_turns"] = data.get("num_turns")
        return out

    if rtype == "hook.finished":
        hook = str(data.get("hook_name") or "")
        if hook.endswith("PreToolUse:ExitPlanMode"):
            return {
                "state": "parked",
                "reason": "plan_gate",
                "ts": ts,
                "age_sec": age,
                "turn_id": str(last.get("turn_id") or ""),
            }

    fresh = age is not None and age <= ACTIVE_FRESH_SEC
    return {
        "state": "active" if fresh else "idle",
        "reason": rtype,
        "ts": ts,
        "age_sec": age,
    }
