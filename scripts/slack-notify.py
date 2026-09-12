#!/usr/bin/env python3
"""Post a message to the Slack agent-community workspace.

Stdlib only. The slack.env file (default ~/.config/remote-agent/slack.env,
mode 0600) is read directly at use time; credentials never live in a process
environment and token values are never printed.

Usage:
  spectre-slack-notify --agent orca --channel lobby --text 'daemon restarts?'
  spectre-slack-notify --channel alerts --text-file /tmp/msg --thread-ts 1.5
  spectre-slack-notify --agent healthcheck --channel alerts --recovery --text recovered
  spectre-slack-notify --self-test
  spectre-slack-notify --dry-run --channel lobby --text hi

Unconfigured (no env file / no bot token) prints "slack-notify: disabled"
and exits 0 so callers such as healthcheck stay green before setup.
Exit codes: 0 = posted / disabled / dry-run ok; 1 = configuration or API error.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_ENV_FILE = Path.home() / ".config/remote-agent/slack.env"
BOX_REGISTRY = Path("/usr/local/share/remote-agent/slack-agents.json")
REPO_REGISTRY = Path(__file__).resolve().parents[1] / "config" / "slack-agents.json"
API_URL = "https://slack.com/api/chat.postMessage"
TIMEOUT = 10.0

CHANNEL_ALIASES = {
    "alerts": "SLACK_CHANNEL_ALERTS",
    "fleet": "SLACK_CHANNEL_FLEET",
    "control": "SLACK_CHANNEL_CONTROL",
    "lobby": "SLACK_CHANNEL_LOBBY",
}

BOT_TOKEN_RE = re.compile(r"^xoxb-\S{20,}$")
APP_TOKEN_RE = re.compile(r"^xapp-\S{20,}$")
CHANNEL_RE = re.compile(r"^C[A-Z0-9]{8,}$")
USER_RE = re.compile(r"^U[A-Z0-9]{6,}$")
TS_RE = re.compile(r"^\d{1,20}\.\d{1,10}$")

RECOVERY_PREFIX = ":white_check_mark: "


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


def registry_path() -> Path:
    override = os.environ.get("SLACK_AGENTS_FILE", "").strip()
    if override:
        return Path(override)
    if BOX_REGISTRY.is_file():
        return BOX_REGISTRY
    return REPO_REGISTRY


def load_registry(path: Path) -> dict[str, dict[str, str]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"slack-notify: registry unreadable: {path}: {exc}") from exc
    agents = payload.get("agents")
    if not isinstance(agents, dict) or not agents:
        raise SystemExit(f"slack-notify: registry has no agents: {path}")
    return agents


def identity_for(agents: dict[str, dict[str, str]], name: str) -> dict[str, str]:
    entry = agents.get(name)
    if not isinstance(entry, dict):
        known = ", ".join(sorted(agents))
        raise SystemExit(f"slack-notify: unknown agent {name!r} (known: {known})")
    username = str(entry.get("username") or "").strip()
    icon = str(entry.get("icon_emoji") or "").strip()
    if not username or not icon:
        raise SystemExit(f"slack-notify: registry entry {name!r} lacks username/icon_emoji")
    return {"username": username, "icon_emoji": icon}


def resolve_channel(value: str, env: dict[str, str]) -> str:
    key = CHANNEL_ALIASES.get(value)
    if key is None:
        if CHANNEL_RE.match(value):
            return value
        raise SystemExit(
            f"slack-notify: bad channel {value!r} (alerts|fleet|control|lobby or C...)"
        )
    channel = env.get(key, "").strip()
    if not channel:
        raise SystemExit(f"slack-notify: {key} is not set in the env file")
    if not CHANNEL_RE.match(channel):
        raise SystemExit(f"slack-notify: {key} is not a channel ID (C...)")
    return channel


def build_payload(
    channel: str,
    text: str,
    identity: dict[str, str],
    thread_ts: str | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "channel": channel,
        "text": text,
        "username": identity["username"],
        "icon_emoji": identity["icon_emoji"],
        "unfurl_links": False,
        "unfurl_media": False,
    }
    if thread_ts:
        payload["thread_ts"] = thread_ts
    return payload


def _retry_after(exc: urllib.error.HTTPError) -> float:
    try:
        raw = exc.headers.get("Retry-After") if exc.headers else None
        return min(float(raw), 30.0) if raw else 5.0
    except (TypeError, ValueError):
        return 5.0


def post(token: str, payload: dict[str, object]) -> tuple[bool, str]:
    """(ok, detail). One retry on HTTP 429 (Retry-After) and once on 5xx."""
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        API_URL,
        data=data,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        },
        method="POST",
    )
    for attempt in (1, 2):
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                body = json.loads(response.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            if attempt == 1 and exc.code == 429:
                time.sleep(_retry_after(exc))
                continue
            if attempt == 1 and 500 <= exc.code < 600:
                time.sleep(2)
                continue
            return False, f"http={exc.code}"
        except (urllib.error.URLError, TimeoutError, OSError):
            return False, "network"
        except json.JSONDecodeError:
            return False, "bad_json"
        if body.get("ok"):
            return True, str(body.get("ts", ""))
        error = str(body.get("error", "unknown"))
        if attempt == 1 and error == "ratelimited":
            try:
                delay = float(body.get("retry_after", 5) or 5)
            except (TypeError, ValueError):
                delay = 5.0
            time.sleep(min(delay, 30.0))
            continue
        return False, f"error={error}"
    return False, "retry_exhausted"


def _token_line(label: str, value: str, pattern: re.Pattern[str]) -> tuple[str, bool]:
    if pattern.match(value):
        return f"  {label}: {value[:5]}... ({len(value)} chars)", True
    return f"  {label}: MISSING or bad shape", False


def self_test(env_file: Path) -> int:
    """Shape-only checks; token values are never printed."""
    print("slack-notify self-test")
    print(f"  env-file: {env_file}")
    env = load_env_file(env_file)
    if not env:
        print("  FAIL: env file missing or empty")
        print("FAIL (0 channels, 0 agents)")
        return 1
    ok = True
    try:
        mode = stat.S_IMODE(env_file.stat().st_mode)
        if mode & 0o077:
            print(f"  FAIL: mode {mode:03o} — set chmod 600")
            ok = False
        else:
            print(f"  mode: {mode:03o}")
    except OSError as exc:
        print(f"  FAIL: cannot stat env file: {exc}")
        ok = False

    line, good = _token_line("bot token", env.get("SLACK_BOT_TOKEN", ""), BOT_TOKEN_RE)
    print(line)
    ok = ok and good
    line, good = _token_line("app token", env.get("SLACK_APP_TOKEN", ""), APP_TOKEN_RE)
    print(line)
    ok = ok and good

    good_channels = 0
    for alias, key in CHANNEL_ALIASES.items():
        value = env.get(key, "").strip()
        if CHANNEL_RE.match(value):
            print(f"  channel {alias}: ok")
            good_channels += 1
        else:
            print(f"  channel {alias}: MISSING or bad shape ({key})")
            ok = False

    users = [u.strip() for u in env.get("SLACK_ALLOWED_USERS", "").split(",") if u.strip()]
    if users and all(USER_RE.match(u) for u in users):
        print(f"  allowed users: {len(users)}")
    else:
        print("  allowed users: FAIL — list at least one U... member id")
        ok = False

    path = registry_path()
    agents = load_registry(path)
    print(f"  registry: {path} ({len(agents)} agents)")
    for name in sorted(agents):
        try:
            identity_for(agents, name)
        except SystemExit as exc:
            print(f"  FAIL: {exc}")
            ok = False

    if ok:
        print(f"OK ({good_channels} channels, {len(agents)} agents)")
        return 0
    print(f"FAIL ({good_channels} channels, {len(agents)} agents)")
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="slack-notify", description="post to the Slack agent community"
    )
    parser.add_argument("--agent", default="bridge", help="identity from the registry")
    parser.add_argument("--channel", help="alerts|fleet|control|lobby or a C... id")
    parser.add_argument("--text")
    parser.add_argument("--text-file")
    parser.add_argument("--thread-ts", help="parent message ts for a threaded reply")
    parser.add_argument(
        "--recovery", action="store_true", help="prefix the text with a green check"
    )
    parser.add_argument("--dry-run", action="store_true", help="print payload, post nothing")
    parser.add_argument("--self-test", action="store_true", help="shape-check the config")
    parser.add_argument("--env-file", default=str(DEFAULT_ENV_FILE))
    args = parser.parse_args(argv)

    env_file = Path(args.env_file).expanduser()
    if args.self_test:
        return self_test(env_file)

    if not args.channel:
        parser.error("--channel is required")
    if bool(args.text) == bool(args.text_file):
        parser.error("exactly one of --text or --text-file is required")

    text = args.text or ""
    if args.text_file:
        try:
            text = Path(args.text_file).read_text(encoding="utf-8")
        except OSError as exc:
            print(f"slack-notify: cannot read {args.text_file}: {exc}", file=sys.stderr)
            return 1
    text = text.strip()
    if not text:
        print("slack-notify: empty message", file=sys.stderr)
        return 1
    if args.thread_ts and not TS_RE.match(args.thread_ts):
        print(f"slack-notify: bad --thread-ts {args.thread_ts!r}", file=sys.stderr)
        return 1
    if args.recovery and not text.startswith(RECOVERY_PREFIX):
        text = RECOVERY_PREFIX + text

    env = load_env_file(env_file)
    if not env.get("SLACK_BOT_TOKEN", "").strip():
        print("slack-notify: disabled")
        return 0

    agents = load_registry(registry_path())
    identity = identity_for(agents, args.agent)
    channel = resolve_channel(args.channel, env)
    payload = build_payload(channel, text, identity, args.thread_ts)

    if args.dry_run:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0

    ok, detail = post(env["SLACK_BOT_TOKEN"].strip(), payload)
    if not ok:
        print(f"slack-notify: {detail}", file=sys.stderr)
        return 1
    shown = args.channel if args.channel in CHANNEL_ALIASES else channel
    print(f"slack-notify: posted {shown} ts={detail}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
