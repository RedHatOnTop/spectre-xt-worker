"""Bounded recovery when the resolver process lives but its socket is unusable."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import time

from .client import StateClient

STATE = Path.home() / ".local/state/remote-agent/worker-state-watchdog.json"
WINDOW = 3600
MAX_RESTARTS = 3


def load(path: Path) -> dict:
    if path.is_symlink():
        raise ValueError("watchdog state must not be a symlink")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"failures": 0, "restarts": [], "alert_at": 0}
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("watchdog state is unreadable") from exc
    if (not isinstance(data, dict) or type(data.get("failures")) is not int
            or data["failures"] < 0 or not isinstance(data.get("restarts"), list)
            or any(type(value) not in (int, float) for value in data["restarts"])):
        raise ValueError("watchdog state is invalid")
    return data


def save(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise ValueError("watchdog state must not be a symlink")
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(state, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def tick(path: Path, now: float, probe, restart) -> dict:
    state = load(path)
    recent = [value for value in state["restarts"] if 0 <= now - value < WINDOW]
    if probe():
        save(path, {**state, "failures": 0, "restarts": recent})
        return {"action": "healthy"}
    failures = state["failures"] + 1
    updated = {**state, "failures": failures, "restarts": recent}
    if failures < 2:
        save(path, updated)
        return {"action": "observe"}
    if len(recent) >= MAX_RESTARTS:
        alert = now - float(state.get("alert_at") or 0) >= WINDOW
        save(path, {**updated, "alert_at": now if alert else state.get("alert_at", 0)})
        return {"action": "restart_cap", "alert": alert}
    attempted = {**updated, "restarts": [*recent, now]}
    save(path, attempted)
    restarted = restart()
    recovered = restarted and probe()
    save(path, {**attempted, "failures": 0 if recovered else failures})
    return {"action": "recovered" if recovered else "restart_failed", "alert": True}


def _probe() -> bool:
    status, payload = StateClient(timeout=3).health()
    return status == 200 and payload.get("ok") is True


def _restart() -> bool:
    try:
        result = subprocess.run(["systemctl", "--user", "restart",
                                 "spectre-worker-state.service"],
                                capture_output=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    if result.returncode != 0:
        return False
    time.sleep(2)
    return True


def _notify(action: str) -> None:
    try:
        subprocess.run(["spectre-slack-notify", "--agent", "healthcheck",
                        "--channel", "fleet", "--text",
                        f"worker-state watchdog: {action}"],
                       capture_output=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        pass


def main() -> int:
    if os.environ.get("SPECTRE_STATE_WATCHDOG") != "1":
        print(json.dumps({"action": "disabled"}))
        return 0
    try:
        result = tick(STATE, time.time(), _probe, _restart)
    except (OSError, ValueError) as exc:
        print(json.dumps({"action": "error", "error": str(exc)}))
        return 1
    if result.get("alert"):
        _notify(result["action"])
    print(json.dumps(result, sort_keys=True))
    return 0 if result["action"] in {"healthy", "observe", "recovered"} else 1
