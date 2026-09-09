#!/usr/bin/env python3
"""Stop Spectre Qoder work if Efficient's live price_factor is no longer 0.

Qoder encrypts ~/.qoder/.models/*/catalog-v6. The signed-in CLI keeps the
plaintext catalog in qodercli RSS, including:

  {"key":"efficient","display_name":"Efficient","price_factor":0.0,
   "original_price_factor":0.3,"is_free":true}

A 0.0 factor is the promo. Anything greater is billed. Scanning /proc does
not call the chat API and does not spend credits.

Fail closed on new starts (unknown → refuse). Fail open on the 60s timer
(unknown → do not kill a live 0x fleet). A billed trip writes a sentinel
that stays until `qoder-efficient-guard clear`.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "remote-agent"
SENTINEL_NAME = "qoder-efficient-billed"
RATE_NAME = "qoder-efficient-rate.json"
STREAK_NAME = "qoder-efficient-billed-streak"
LOG_NAME = "qoder-efficient-guard.log"
BILL_STREAK_NEEDED = 2
WORK_LOG = Path("/work/logs") / LOG_NAME
ORCA = "/usr/bin/orca-ide"
QODERCLI = str(Path.home() / ".local/bin/qodercli")
HEALTH_ENV = Path.home() / ".config/remote-agent/health.env"

KEY_RE = re.compile(
    rb'["\']key["\']\s*:\s*["\']efficient["\']',
    re.IGNORECASE,
)
U16_KEY = '"key":"efficient"'.encode("utf-16le")
U16_KEY_SPACED = '"key" : "efficient"'.encode("utf-16le")


@dataclass(frozen=True)
class EfficientRate:
    key: str
    display_name: str
    price_factor: float
    original_price_factor: float | None
    is_free: bool | None
    source: str = ""


@dataclass(frozen=True)
class Decision:
    status: str
    price_factor: float | None = None
    original_price_factor: float | None = None
    is_free: bool | None = None
    source: str = ""
    checked_at: str = ""


def is_billed(rate: EfficientRate) -> bool:
    return rate.price_factor > 1e-12


def should_stop(decision: Decision) -> bool:
    return decision.status == "billed"


def allow_start(decision: Decision, sentinel_present: bool) -> bool:
    if sentinel_present:
        return False
    return decision.status == "free"


def _object_around(blob: bytes, marker_at: int) -> bytes | None:
    start = blob.rfind(b"{", 0, marker_at + 1)
    if start < 0:
        return None
    depth = 0
    in_str = False
    escape = False
    end = min(len(blob), start + 12000)
    for i in range(start, end):
        c = blob[i]
        if in_str:
            if escape:
                escape = False
            elif c == 0x5C:
                escape = True
            elif c == 0x22:
                in_str = False
            continue
        if c == 0x22:
            in_str = True
            continue
        if c == 0x7B:
            depth += 1
        elif c == 0x7D:
            depth -= 1
            if depth == 0:
                return blob[start : i + 1]
    return None


def _rate_from_obj(obj: dict, source: str) -> EfficientRate | None:
    key = str(obj.get("key") or "").strip().lower()
    if key != "efficient":
        return None
    factor = obj.get("price_factor")
    if isinstance(factor, bool) or not isinstance(factor, (int, float)):
        return None
    original = obj.get("original_price_factor")
    if isinstance(original, bool):
        original = None
    is_free = obj.get("is_free")
    return EfficientRate(
        key="efficient",
        display_name=str(obj.get("display_name") or "Efficient"),
        price_factor=float(factor),
        original_price_factor=float(original) if isinstance(original, (int, float)) else None,
        is_free=bool(is_free) if isinstance(is_free, bool) else None,
        source=source,
    )


def _parse_utf8(blob: bytes, source: str) -> list[EfficientRate]:
    found: list[EfficientRate] = []
    for match in KEY_RE.finditer(blob):
        raw = _object_around(blob, match.start())
        if not raw:
            continue
        try:
            obj = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(obj, dict):
            continue
        rate = _rate_from_obj(obj, source)
        if rate is not None:
            found.append(rate)
    return found


def _utf16_windows(blob: bytes) -> Iterable[bytes]:
    for needle in (U16_KEY, U16_KEY_SPACED):
        start = 0
        while True:
            idx = blob.find(needle, start)
            if idx < 0:
                break
            begin = max(0, idx - 400)
            if begin % 2:
                begin += 1
            end = min(len(blob), idx + len(needle) + 4000)
            if end % 2:
                end -= 1
            chunk = blob[begin:end]
            try:
                yield chunk.decode("utf-16le").encode("utf-8")
            except UnicodeDecodeError:
                pass
            start = idx + 2


def parse_efficient_rates(blob: bytes, source: str = "blob") -> list[EfficientRate]:
    found = _parse_utf8(blob, source)
    if found:
        return found
    extra: list[EfficientRate] = []
    for window in _utf16_windows(blob):
        extra.extend(_parse_utf8(window, source + "+utf16"))
    return extra


def decide_from_rates(rates: list[EfficientRate]) -> Decision:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if not rates:
        return Decision(status="unknown", checked_at=now)
    billed = [r for r in rates if is_billed(r)]
    free = [r for r in rates if not is_billed(r)]
    if billed and free:
        chosen = max(billed, key=lambda r: r.price_factor)
        status = "mixed"
    elif billed:
        chosen = max(billed, key=lambda r: r.price_factor)
        status = "billed"
    else:
        chosen = min(free, key=lambda r: r.price_factor)
        status = "free"
    return Decision(
        status=status,
        price_factor=chosen.price_factor,
        original_price_factor=chosen.original_price_factor,
        is_free=chosen.is_free,
        source=chosen.source,
        checked_at=now,
    )


def _is_our_qodercli(pid: int) -> bool:
    try:
        comm = Path(f"/proc/{pid}/comm").read_text().strip()
        if comm != "qodercli":
            return False
        exe = os.readlink(f"/proc/{pid}/exe")
    except OSError:
        return False
    name = Path(exe.split("\x00")[0]).name
    return name.startswith("qodercli")


def _qodercli_pids() -> list[int]:
    pids: list[int] = []
    proc = Path("/proc")
    if not proc.is_dir():
        return pids
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if _is_our_qodercli(pid):
            pids.append(pid)
    return pids


_SKIP_MAPS = ("[vvar]", "[vdso]", "[vsyscall]", "[stack]")
_CHUNK = 2 * 1024 * 1024
_UTF8_NEEDLE = b'"key":"efficient"'
_U16_NEEDLE = '"key":"efficient"'.encode("utf-16le")


def _should_scan_map(row: str) -> bool:
    parts = row.split()
    if len(parts) < 2:
        return False
    perms = parts[1]
    if "r" not in perms or "w" not in perms:
        return False
    pathname = parts[-1] if len(parts) >= 6 else ""
    if pathname.startswith("/"):
        return False
    if pathname in _SKIP_MAPS:
        return False
    return True


def _read_pid_blobs(pid: int) -> Iterable[bytes]:
    try:
        rows = Path(f"/proc/{pid}/maps").read_text().splitlines()
        mem = open(f"/proc/{pid}/mem", "rb")
    except OSError:
        return
    with mem:
        for row in rows:
            if not _should_scan_map(row):
                continue
            parts = row.split()
            start_s, end_s = parts[0].split("-")
            start, end = int(start_s, 16), int(end_s, 16)
            size = end - start
            if size <= 0 or size > 96 * 1024 * 1024:
                continue
            pos = 0
            overlap = b""
            while pos < size:
                n = min(_CHUNK, size - pos)
                try:
                    mem.seek(start + pos)
                    chunk = mem.read(n)
                except OSError:
                    break
                if not chunk:
                    break
                window = overlap + chunk
                if _UTF8_NEEDLE in window or _U16_NEEDLE in window:
                    try:
                        extra = mem.read(65536)
                    except OSError:
                        extra = b""
                    yield window + extra
                    break
                overlap = window[-256:] if len(window) > 256 else window
                pos += n


def scan_pid(pid: int) -> list[EfficientRate]:
    found: list[EfficientRate] = []
    for blob in _read_pid_blobs(pid):
        found.extend(parse_efficient_rates(blob, source=f"pid:{pid}"))
        if found:
            break
    return found


def scan_live() -> list[EfficientRate]:
    for pid in _qodercli_pids():
        found = scan_pid(pid)
        if found:
            return found
    return []


def _spawn_list_models(qodercli: str) -> list[EfficientRate]:
    if not Path(qodercli).exists():
        return []
    try:
        proc = subprocess.Popen(
            [qodercli, "--list-models"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return []
    found: list[EfficientRate] = []
    deadline = time.time() + 20
    try:
        while time.time() < deadline:
            found = scan_pid(proc.pid)
            if found:
                break
            if proc.poll() is not None:
                break
            time.sleep(0.15)
    finally:
        if proc.poll() is None:
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3)
    return found


def probe(
    qodercli: str = QODERCLI,
    *,
    spawn: str = "auto",
    state_dir: Path = STATE_DIR,
) -> Decision:
    if spawn != "force" and sentinel_path(state_dir).exists():
        try:
            data = json.loads(sentinel_path(state_dir).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        factor = data.get("price_factor")
        return Decision(
            status="billed",
            price_factor=float(factor) if isinstance(factor, (int, float)) and not isinstance(factor, bool) else None,
            original_price_factor=data.get("original_price_factor"),
            is_free=data.get("is_free"),
            source="sentinel",
            checked_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
    rates = scan_live()
    decision = decide_from_rates(rates)
    if spawn == "never":
        return decision
    if decision.status in ("billed", "mixed") and _qodercli_pids():
        fresh = _spawn_list_models(qodercli)
        if fresh:
            return decide_from_rates(fresh)
    if spawn == "auto" and not rates and not _qodercli_pids():
        return decide_from_rates(_spawn_list_models(qodercli))
    return decision


def sentinel_path(state_dir: Path = STATE_DIR) -> Path:
    return state_dir / SENTINEL_NAME


def rate_path(state_dir: Path = STATE_DIR) -> Path:
    return state_dir / RATE_NAME


def write_snapshot(decision: Decision, state_dir: Path = STATE_DIR) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    payload = asdict(decision)
    rate_path(state_dir).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def log(msg: str, state_dir: Path = STATE_DIR) -> None:
    line = f"{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')} {msg}"
    for dest in (state_dir / LOG_NAME, WORK_LOG):
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            with dest.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            continue
    print(line)


def _default_run(args: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        check=False,
        capture_output=True,
        text=True,
        timeout=kwargs.get("timeout", 20),
    )


def _load_health_env() -> dict[str, str]:
    env: dict[str, str] = {}
    try:
        text = HEALTH_ENV.read_text(encoding="utf-8")
    except OSError:
        return env
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key] = value
    return env


def _notify_ntfy(body: str) -> None:
    env = _load_health_env()
    topic = os.environ.get("NTFY_TOPIC") or env.get("NTFY_TOPIC") or ""
    if not topic:
        return
    url = (os.environ.get("NTFY_URL") or env.get("NTFY_URL") or "https://ntfy.sh").rstrip("/")
    req = urllib.request.Request(
        f"{url}/{topic}",
        data=body.encode("utf-8"),
        headers={
            "Title": "spectre qoder kill-switch",
            "Priority": "urgent",
            "Tags": "warning,moneybag",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp.read(256)
    except (OSError, urllib.error.URLError):
        pass


def _qoder_terminals(listing: dict) -> list[dict]:
    terminals = listing.get("result", {}).get("terminals") or []
    picked: list[dict] = []
    for term in terminals:
        blob = " ".join(
            str(term.get(key) or "") for key in ("title", "command", "worktreePath", "handle")
        )
        if "qoder" in blob.lower():
            picked.append(term)
    return picked


def _kill_qodercli(sleep: Callable[[float], None] = time.sleep) -> list[int]:
    pids = _qodercli_pids()
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            continue
    sleep(2)
    leftover = _qodercli_pids()
    for pid in leftover:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            continue
    sleep(0.5)
    return _qodercli_pids()


def streak_path(state_dir: Path = STATE_DIR) -> Path:
    return state_dir / STREAK_NAME


def bump_streak(state_dir: Path = STATE_DIR) -> int:
    path = streak_path(state_dir)
    try:
        n = int(path.read_text(encoding="utf-8").strip() or "0")
    except (OSError, ValueError):
        n = 0
    n += 1
    state_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{n}\n", encoding="utf-8")
    return n


def clear_streak(state_dir: Path = STATE_DIR) -> None:
    streak_path(state_dir).unlink(missing_ok=True)


def stop_all(
    decision: Decision,
    state_dir: Path = STATE_DIR,
    run: Callable[..., subprocess.CompletedProcess[str]] = _default_run,
    notify: Callable[[str], None] | None = None,
    orca: str = ORCA,
    sleep: Callable[[float], None] = time.sleep,
    kill_qodercli: Callable[[], list[int]] | None = None,
) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    payload = asdict(decision)
    payload["stopped_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    sentinel_path(state_dir).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    write_snapshot(decision, state_dir)
    factor = decision.price_factor
    log(f"STOP efficient billed price_factor={factor} source={decision.source}", state_dir)

    run(["systemctl", "--user", "stop", "qoder-nudge.timer"], timeout=10)
    run(["systemctl", "--user", "disable", "qoder-nudge.timer"], timeout=10)

    listed = run([orca, "terminal", "list", "--json"], timeout=15)
    try:
        listing = json.loads(listed.stdout or "{}")
    except json.JSONDecodeError:
        listing = {}
    for term in _qoder_terminals(listing):
        handle = term.get("handle")
        if not handle:
            continue
        run(
            [
                orca,
                "terminal",
                "send",
                "--terminal",
                str(handle),
                "--text",
                "/goal pause",
                "--enter",
                "--json",
            ],
            timeout=15,
        )

    leftover = (kill_qodercli or (lambda: _kill_qodercli(sleep)))()
    if leftover:
        log(f"WARN qodercli still alive after kill leftover={leftover}", state_dir)

    body = (
        f"Efficient is billing (price_factor={factor}). "
        "Stopped qodercli, paused /goal, disabled qoder-nudge.timer."
    )
    (notify or _notify_ntfy)(body)


def hook_response(event: dict, sentinel_present: bool) -> tuple[int, str, str]:
    if not sentinel_present:
        return 0, "", ""
    name = str(event.get("hook_event_name") or "")
    reason = (
        "Efficient is billing (price_factor > 0). "
        "All Spectre Qoder work is stopped to avoid spend."
    )
    if name in ("PreToolUse", "UserPromptSubmit"):
        return 2, reason, ""
    if name == "SessionStart":
        payload = {
            "continue": False,
            "stopReason": reason,
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": reason,
            },
        }
        return 0, "", json.dumps(payload)
    return 0, "", ""


def _install_hooks(settings_path: Path, command: str) -> None:
    if settings_path.exists():
        data = json.loads(settings_path.read_text(encoding="utf-8"))
    else:
        data = {}
    hooks = data.setdefault("hooks", {})

    def keep(entry: dict) -> bool:
        for item in entry.get("hooks") or []:
            cmd = str(item.get("command") or "")
            if "qoder-efficient-guard" in cmd:
                return False
        return True

    for event in ("PreToolUse", "UserPromptSubmit", "SessionStart"):
        existing = hooks.get(event) or []
        hooks[event] = [entry for entry in existing if keep(entry)]

    guard_hook = {
        "hooks": [
            {
                "type": "command",
                "command": command,
                "timeout": 5,
            }
        ]
    }
    hooks.setdefault("PreToolUse", []).append(dict(guard_hook))
    hooks.setdefault("UserPromptSubmit", []).append(dict(guard_hook))
    hooks.setdefault("SessionStart", []).append(dict(guard_hook))
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    mode = 0o600
    if settings_path.exists():
        mode = stat.S_IMODE(settings_path.stat().st_mode)
    tmp = settings_path.with_name(settings_path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.chmod(tmp, mode)
    tmp.replace(settings_path)


def _print_decision(decision: Decision) -> None:
    print(json.dumps(asdict(decision), indent=2))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        nargs="?",
        default="check",
        choices=("probe", "check", "hook", "stop", "clear", "allow-start", "install-hooks"),
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--state-dir", type=Path, default=STATE_DIR)
    parser.add_argument("--qodercli", default=QODERCLI)
    parser.add_argument("--orca", default=ORCA)
    args = parser.parse_args(argv)
    state_dir: Path = args.state_dir

    if args.action == "hook":
        raw = sys.stdin.read()
        try:
            event = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            event = {}
        code, stderr, stdout = hook_response(event, sentinel_path(state_dir).exists())
        if stderr:
            print(stderr, file=sys.stderr)
        if stdout:
            print(stdout)
        return code

    if args.action == "install-hooks":
        command = f"{Path.home() / '.local/bin' / 'qoder-efficient-guard'} hook"
        settings = Path.home() / ".qoder" / "settings.json"
        _install_hooks(settings, command)
        log(f"installed hooks into {settings}", state_dir)
        return 0

    if args.action == "clear":
        sentinel_path(state_dir).unlink(missing_ok=True)
        clear_streak(state_dir)
        _default_run(["systemctl", "--user", "enable", "--now", "qoder-nudge.timer"], timeout=10)
        log("cleared billed sentinel; re-enabled qoder-nudge.timer", state_dir)
        return 0

    if args.action == "stop":
        decision = Decision(
            status="billed",
            price_factor=None,
            source="manual",
            checked_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        if args.dry_run:
            _print_decision(decision)
            return 2
        stop_all(decision, state_dir=state_dir, orca=args.orca)
        return 2

    if args.action == "allow-start":
        if sentinel_path(state_dir).exists():
            print(
                "qoder-efficient: refused (kill-switch sentinel present)",
                file=sys.stderr,
            )
            return 75
        decision = probe(qodercli=args.qodercli, spawn="auto", state_dir=state_dir)
        write_snapshot(decision, state_dir)
        log(
            f"allow-start status={decision.status} price_factor={decision.price_factor} "
            f"source={decision.source}",
            state_dir,
        )
        if not allow_start(decision, sentinel_present=False):
            print(
                "qoder-efficient: refused (Efficient is billing or factor unknown)",
                file=sys.stderr,
            )
            return 75
        return 0

    if args.action == "probe":
        decision = probe(qodercli=args.qodercli, spawn="auto", state_dir=state_dir)
        write_snapshot(decision, state_dir)
        log(
            f"probe status={decision.status} price_factor={decision.price_factor} "
            f"source={decision.source}",
            state_dir,
        )
        _print_decision(decision)
        return 0 if decision.status != "unknown" else 1

    # check: never spawn if the fleet is already down or the sentinel is set.
    if sentinel_path(state_dir).exists():
        leftover = _qodercli_pids()
        if leftover and not args.dry_run:
            still = _kill_qodercli()
            log(f"sentinel present; reaped leftover={still}", state_dir)
        else:
            log("sentinel present; leaving fleet stopped", state_dir)
        return 0

    decision = probe(qodercli=args.qodercli, spawn="never", state_dir=state_dir)
    if decision.status in ("billed", "mixed") and _qodercli_pids():
        confirmed = probe(qodercli=args.qodercli, spawn="auto", state_dir=state_dir)
        if confirmed.status != "unknown":
            decision = confirmed
    log(
        f"check status={decision.status} price_factor={decision.price_factor} "
        f"source={decision.source}",
        state_dir,
    )
    _print_decision(decision)
    if should_stop(decision):
        n = bump_streak(state_dir)
        if args.dry_run:
            write_snapshot(decision, state_dir)
            return 2
        if n < BILL_STREAK_NEEDED:
            write_snapshot(decision, state_dir)
            log(f"billed tick {n}/{BILL_STREAK_NEEDED}; not stopping yet", state_dir)
            return 0
        stop_all(decision, state_dir=state_dir, orca=args.orca)
        return 2
    clear_streak(state_dir)
    write_snapshot(decision, state_dir)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
