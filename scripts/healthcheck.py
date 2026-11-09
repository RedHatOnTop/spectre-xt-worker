#!/usr/bin/env python3
"""Spectre worker health probe.

Runs every 60s from a user timer. Checks proxy, orca serve, ZCode,
temperature, disk, memory, tmux control session, AC power, and Tailscale.
Failures go to /work/logs/health.log, ntfy, and Slack (independent backends);
this process always exits 0 so the timer never accumulates failed units.

Notification state machine (pure functions, unit-tested):
- new failure set        -> notify immediately
- changed failure set    -> notify immediately, keep original since
- same set within window -> log only
- same set past window   -> renotify with total duration
- recovered              -> one recovery notice, state cleared

A heartbeat file is touched on every passing tick so `ssh` + stat (or the
phone via spectre-status) can distinguish "all good" from "box is dark".
"""
from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import sys
import time
from pathlib import Path

LOG_DIR = Path("/work/logs")
LOG_FILE = LOG_DIR / "health.log"
STATE_DIR = Path.home() / ".local/state/remote-agent"
STATE_FILE = STATE_DIR / "health-state.json"
HEARTBEAT = STATE_DIR / "heartbeat"
ENV_FILE = Path.home() / ".config/remote-agent/health.env"

DEFAULT_PROXY_URL = "http://127.0.0.1:18088/health"
DEFAULT_NTFY_URL = "https://ntfy.sh"
DEFAULT_TEMP_FAIL_C = 85.0
DEFAULT_DISK_FAIL_PCT = 90
DEFAULT_MEM_FLOOR_MB = 800
DEFAULT_SWAP_FAIL_PCT = 90
DEFAULT_SWAP_USED_GIB = 4.0
DEFAULT_RENOTIFY_MIN = 30
DEFAULT_BRIDGE_URL = "http://127.0.0.1:8787/mcp"
DEFAULT_ORCA_URL = "http://127.0.0.1:6768/web-index.html"


def _flag(environ: dict[str, str], key: str, *, default: bool) -> bool:
    raw = environ.get(key)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off")


@dataclasses.dataclass(frozen=True)
class Config:
    proxy_url: str
    ntfy_url: str
    ntfy_topic: str
    temp_fail_c: float
    disk_fail_pct: int
    mem_floor_mb: int
    swap_fail_pct: int
    swap_used_gib: float
    renotify_min: int
    require_proxy: bool
    require_zcode: bool
    require_claude: bool
    require_bridge: bool
    bridge_health_url: str
    orca_url: str
    require_orca: bool
    require_slack: bool


def load_env_file(path: Path) -> dict[str, str]:
    """Parse KEY=VALUE lines. Comments and blanks skipped. Quotes not stripped."""
    out: dict[str, str] = {}
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


def config_from_env(environ: dict[str, str]) -> Config:
    def get(key: str, default: str) -> str:
        return environ.get(key) or default

    return Config(
        proxy_url=get("PROXY_HEALTH_URL", DEFAULT_PROXY_URL),
        ntfy_url=get("NTFY_URL", DEFAULT_NTFY_URL).rstrip("/"),
        ntfy_topic=get("NTFY_TOPIC", ""),
        temp_fail_c=float(get("TEMP_FAIL_C", DEFAULT_TEMP_FAIL_C)),
        disk_fail_pct=int(get("DISK_FAIL_PCT", DEFAULT_DISK_FAIL_PCT)),
        mem_floor_mb=int(get("MEM_FLOOR_MB", DEFAULT_MEM_FLOOR_MB)),
        swap_fail_pct=int(get("SWAP_FAIL_PCT", DEFAULT_SWAP_FAIL_PCT)),
        swap_used_gib=float(get("SWAP_USED_GIB", DEFAULT_SWAP_USED_GIB)),
        renotify_min=int(get("RENOTIFY_MIN", DEFAULT_RENOTIFY_MIN)),
        require_proxy=_flag(environ, "REQUIRE_PROXY", default=True),
        require_zcode=_flag(environ, "REQUIRE_ZCODE", default=True),
        require_claude=_flag(environ, "REQUIRE_CLAUDE", default=False),
        require_bridge=_flag(environ, "REQUIRE_BRIDGE", default=False),
        bridge_health_url=get("BRIDGE_HEALTH_URL", DEFAULT_BRIDGE_URL),
        orca_url=get("ORCA_URL", DEFAULT_ORCA_URL),
        require_orca=_flag(environ, "REQUIRE_ORCA", default=True),
        require_slack=_flag(environ, "REQUIRE_SLACK", default=False),
    )


def run(cmd: list[str], timeout: float = 8.0) -> str:
    """Command output or empty string. Never raises."""
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout.strip()


# ---------------------------------------------------------------------------
# individual probes: each returns None when healthy, a reason when not
# ---------------------------------------------------------------------------

def probe_proxy(url: str) -> str | None:
    body = run(["curl", "-fsS", "--max-time", "5", url])
    if not body:
        return "proxy_down"
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return "proxy_bad_json"
    status = payload.get("status")
    keys = payload.get("activeKeys", 0)
    reasons: list[str] = []
    if status != "ok":
        reasons.append(f"proxy_status={status or 'empty'}")
    try:
        keys_num = int(keys)
    except (TypeError, ValueError):
        keys_num = 0
    if keys_num == 0:
        reasons.append("activeKeys=0")
    return ",".join(reasons) or None


def probe_zcode() -> str | None:
    out = run(["pgrep", "-f", "/zcode|[/ ]ZCode"])
    return None if out else "zcode_missing"


def _orca_reason(http_code: str) -> str | None:
    if not http_code:
        return "orca_down"
    if http_code != "200":
        return f"orca_http={http_code}"
    return None


def probe_orca(url: str) -> str | None:
    code = run(
        [
            "curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
            "--max-time", "5", url,
        ]
    )
    return _orca_reason(code)


def probe_slack() -> str | None:
    """Shape-check the Slack wiring (no network): self-test must end in OK."""
    out = run(["spectre-slack-notify", "--self-test"])
    if not out:
        return "slack_notify_unavailable"
    if out.splitlines()[-1].startswith("OK"):
        return None
    return "slack_self_test_failed"


def probe_claude() -> str | None:
    out = run(["pgrep", "-f", r"/usr/local/bin/claude|/usr/bin/claude"])
    return None if out else "claude_missing"


def probe_bridge(url: str) -> str | None:
    """The local MCP endpoint must answer 401 with no token (fail-closed)."""
    code = run(
        [
            "curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
            "--max-time", "5", url,
        ]
    )
    if not code:
        return "bridge_down"
    if code != "401":
        return f"bridge_auth_http={code}"
    return None


def probe_bridge_tunnel() -> str | None:
    # (^|[/\ ]) covers bare argv[0] (PATH install), absolute paths, and
    # `sh -c cloudflared` wrappers alike.
    out = run(["pgrep", "-f", r"(^|[/\ ])cloudflared"])
    return None if out else "bridge_tunnel_missing"


def probe_temp(limit_c: float) -> str | None:
    sensors_json = run(["sensors", "-j"])
    if not sensors_json:
        return None  # lm-sensors absent or unreadable is not a failure
    temps = _extract_temps(sensors_json)
    if not temps:
        return None
    peak = max(temps)
    if peak >= limit_c:
        return f"temp={peak:.0f}C"
    return None


def _extract_temps(node: object) -> list[float]:
    found: list[float] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key.endswith("_input") and isinstance(value, (int, float)):
                found.append(float(value))
            else:
                found.extend(_extract_temps(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_extract_temps(item))
    return found


def probe_disks(limit_pct: int, mounts: tuple[str, ...] = ("/", "/work")) -> list[str]:
    reasons: list[str] = []
    for mount in mounts:
        line = run(["df", "-P", mount]).splitlines()
        if len(line) < 2:
            continue
        fields = line[1].split()
        if len(fields) < 5 or not fields[4].endswith("%"):
            continue
        pct = int(fields[4].rstrip("%"))
        if pct >= limit_pct:
            reasons.append(f"disk:{mount}={pct}%")
    return reasons


def parse_meminfo(meminfo: str) -> dict[str, int]:
    """kB values keyed by /proc/meminfo field name (kB-suffixed lines only)."""
    values: dict[str, int] = {}
    for raw_line in meminfo.splitlines():
        if not raw_line.rstrip().endswith("kB"):
            continue
        key, _, rest = raw_line.rpartition(":")
        if not key:
            continue
        try:
            values[key.strip()] = int(rest.strip().removesuffix("kB").strip())
        except ValueError:
            continue
    return values


def load_fail_threshold(nproc: int) -> float:
    """1-minute load alert floor: max(8, 2 * nproc). Alert only; never fail-closes dispatch."""
    cpus = max(1, int(nproc or 1))
    return float(max(8, 2 * cpus))


def parse_loadavg(text: str) -> float | None:
    parts = (text or "").split()
    if not parts:
        return None
    try:
        return float(parts[0])
    except ValueError:
        return None


def probe_load(loadavg_text: str = "", nproc: int | None = None) -> str | None:
    text = loadavg_text or _read_file("/proc/loadavg")
    load1 = parse_loadavg(text)
    if load1 is None:
        return None
    cpus = int(nproc) if nproc is not None else (os.cpu_count() or 1)
    limit = load_fail_threshold(cpus)
    if load1 >= limit:
        return f"load1={load1:.2f}>={limit:.0f}"
    return None


def probe_memory(
    floor_mb: int,
    swap_limit_pct: int,
    meminfo_text: str = "",
    swap_used_gib: float = DEFAULT_SWAP_USED_GIB,
) -> str | None:
    text = meminfo_text or _read_file("/proc/meminfo")
    if not text:
        return None
    info = parse_meminfo(text)
    available_kb = info.get("MemAvailable")
    total_kb = info.get("MemTotal")
    swap_total_kb = info.get("SwapTotal", 0)
    swap_free_kb = info.get("SwapFree", 0)
    if available_kb is None or total_kb in (None, 0):
        return None
    reasons: list[str] = []
    avail_mb = available_kb // 1024
    if avail_mb < floor_mb:
        reasons.append(f"mem_avail={avail_mb}M")
    if swap_total_kb > 0:
        used_kb = max(0, swap_total_kb - swap_free_kb)
        used_pct = round(100 * used_kb / swap_total_kb)
        if used_pct >= swap_limit_pct:
            reasons.append(f"swap={used_pct}%")
        used_gib = used_kb / (1024 * 1024)
        if used_gib >= float(swap_used_gib):
            reasons.append(f"swap_used={used_gib:.1f}G")
    return ",".join(reasons) or None


def probe_tmux() -> str | None:
    if run(["systemctl", "--user", "is-enabled", "tmux-work.service"]) != "enabled":
        return None
    if _tmux_has_session():
        return None
    return "tmux_work_missing"


def _tmux_has_session() -> bool:
    try:
        proc = subprocess.run(
            ["tmux", "has-session", "-t", "work"],
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def probe_ac() -> str | None:
    online = -1
    for ac in sorted(Path("/sys/class/power_supply").glob("A*")):
        value = _read_file(ac / "online")
        if value in ("0", "1"):
            online = max(online, int(value))
    if online == 0:
        return "ac_offline"
    return None


def probe_tailscale() -> str | None:
    backend = run(["tailscale", "status", "--json"])
    if not backend:
        return None  # tailscale not installed here
    try:
        state = json.loads(backend).get("BackendState")
    except json.JSONDecodeError:
        return "tailscale=unreadable"
    if state != "Running":
        return f"tailscale={state or 'down'}"
    return None


def _read_file(path: str | Path) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def collect_failures(cfg: Config) -> list[str]:
    bits: list[str] = []
    probes: list[str | None] = []
    if cfg.require_proxy:
        probes.append(probe_proxy(cfg.proxy_url))
    if cfg.require_orca:
        probes.append(probe_orca(cfg.orca_url))
    if cfg.require_zcode:
        probes.append(probe_zcode())
    if cfg.require_claude:
        probes.append(probe_claude())
    if cfg.require_slack:
        probes.append(probe_slack())
    if cfg.require_bridge:
        probes.append(probe_bridge(cfg.bridge_health_url))
        probes.append(probe_bridge_tunnel())
    probes.extend(
        (
            probe_temp(cfg.temp_fail_c),
            probe_memory(cfg.mem_floor_mb, cfg.swap_fail_pct, swap_used_gib=cfg.swap_used_gib),
            probe_load(),
            probe_tmux(),
            probe_ac(),
            probe_tailscale(),
        )
    )
    for result in probes:
        if result:
            bits.append(result)
    bits.extend(probe_disks(cfg.disk_fail_pct))
    return bits


# ---------------------------------------------------------------------------
# notification state machine (pure)
# ---------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class State:
    bits: frozenset[str]
    since: int          # epoch seconds of first failure in this streak
    last_notified: int  # epoch seconds of most recent notification
    count: int          # notifications sent for this streak


EMPTY_STATE = State(bits=frozenset(), since=0, last_notified=0, count=0)


def decide(
    current_bits: list[str], previous: State, now: int, renotify_min: int
) -> tuple[str, State]:
    """Return (action, next_state); action in notify_new|renotify|
    notify_recover|log_only|quiet_ok."""
    current = frozenset(current_bits)
    renotify_sec = renotify_min * 60

    if not current:
        if previous.bits:
            return "notify_recover", EMPTY_STATE
        return "quiet_ok", EMPTY_STATE

    if current == previous.bits:
        elapsed_ok = now - previous.last_notified >= renotify_sec
        if elapsed_ok:
            return "renotify", State(
                bits=current,
                since=previous.since,
                last_notified=now,
                count=previous.count + 1,
            )
        return "log_only", previous

    if previous.bits:
        # streak continues but composition changed: alert now, keep since.
        return "notify_new", State(
            bits=current,
            since=previous.since,
            last_notified=now,
            count=previous.count + 1,
        )

    return "notify_new", State(
        bits=current, since=now, last_notified=now, count=1
    )


def duration_text(seconds: int) -> str:
    minutes_total = seconds // 60
    hours, minutes = divmod(minutes_total, 60)
    if hours > 0:
        return f"{hours}h{minutes:02d}m"
    return f"{minutes}m"


def message_for(action: str, state: State, now: int) -> str:
    joined = ",".join(sorted(state.bits))
    if action == "notify_recover":
        return "recovered"
    if action == "renotify":
        return f"still failing {duration_text(now - state.since)}: {joined}"
    return joined


# ---------------------------------------------------------------------------
# side effects
# ---------------------------------------------------------------------------

def append_log(line: str) -> None:
    timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as handle:
            handle.write(f"{timestamp} {line}\n")
    except OSError as exc:
        print(f"cannot write {LOG_FILE}: {exc}", file=sys.stderr)


def send_ntfy(cfg: Config, body: str, *, recovery: bool = False) -> None:
    if not cfg.ntfy_topic:
        return
    args = [
        "curl", "-fsS", "--max-time", "10",
        "-d", body,
        "-H", "Title: spectre worker",
        cfg.ntfy_url + "/" + cfg.ntfy_topic,
    ]
    if recovery:
        args[3:3] = ["-H", "Tags: white_check_mark"]
    else:
        args[3:3] = ["-H", "Priority: high", "-H", "Tags: warning"]
    out = run(args)
    if not out and not recovery:
        append_log(f"NOTIFY_FAILED topic={cfg.ntfy_topic}")


def slack_notify_args(body: str, *, recovery: bool = False) -> list[str]:
    args = [
        "spectre-slack-notify",
        "--agent", "healthcheck",
        "--channel", "alerts",
        f"--text={body}",
    ]
    if recovery:
        args.append("--recovery")
    return args


def send_slack(cfg: Config, body: str, *, recovery: bool = False) -> None:
    """Independent Slack backend; the notifier prints 'disabled' and exits 0
    when slack.env is absent, so this is a no-op before setup."""
    out = run(slack_notify_args(body, recovery=recovery), timeout=15.0)
    if not out:
        append_log("SLACK_NOTIFY_FAILED")


def load_state() -> State:
    raw = _read_file(STATE_FILE)
    if not raw:
        return EMPTY_STATE
    try:
        payload = json.loads(raw)
        return State(
            bits=frozenset(payload["bits"]),
            since=int(payload["since"]),
            last_notified=int(payload["last_notified"]),
            count=int(payload["count"]),
        )
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        append_log("state_unreadable reset")
        return EMPTY_STATE


def save_state(state: State) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(
                {
                    "bits": sorted(state.bits),
                    "since": state.since,
                    "last_notified": state.last_notified,
                    "count": state.count,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        tmp.replace(STATE_FILE)
    except OSError as exc:
        print(f"cannot write {STATE_FILE}: {exc}", file=sys.stderr)


def touch_heartbeat() -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        HEARTBEAT.touch()
    except OSError as exc:
        print(f"cannot touch {HEARTBEAT}: {exc}", file=sys.stderr)


def main() -> int:
    environ = dict(os.environ)
    environ.update(load_env_file(ENV_FILE))
    cfg = config_from_env(environ)
    now = int(time.time())
    failures = collect_failures(cfg)
    action, next_state = decide(failures, load_state(), now, cfg.renotify_min)

    touch_heartbeat()

    if action == "quiet_ok":
        return 0
    if action == "log_only":
        append_log(f"STILL {','.join(sorted(next_state.bits))}")
        return 0

    body = message_for(action, next_state, now)
    append_log(f"{action.upper()} {body}")
    send_ntfy(cfg, body, recovery=(action == "notify_recover"))
    send_slack(cfg, body, recovery=(action == "notify_recover"))
    save_state(next_state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
