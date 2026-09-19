#!/bin/bash
# Run the RUNBOOK verification gates in one pass.
# PASS/FAIL/WARN per gate; exit 1 if any FAIL. Run on the Spectre itself.
#
#   sudo spectre-doctor
#
# WARN lines are things that cannot fail hard from a script (e.g. the
# looks-off check needs human eyes) or that depend on optional hardware.
set -u

PASS=0
FAIL=0

ok() { printf 'PASS  %s\n' "$1"; PASS=$((PASS + 1)); }
bad() { printf 'FAIL  %s\n' "$1"; FAIL=$((FAIL + 1)); }
warn() { printf 'WARN  %s\n' "$1"; }

check_cmd() {
  local desc="$1" cmd="$2"
  if eval "${cmd}" >/dev/null 2>&1; then ok "${desc}"; else bad "${desc}"; fi
}

# Under sudo, user-level units and files belong to SUDO_USER, not root.
if [[ -n "${SUDO_USER:-}" ]]; then
  TARGET_USER="${SUDO_USER}"
else
  TARGET_USER="${USER:-person}"
fi
TARGET_HOME="$(getent passwd "${TARGET_USER}" | cut -d: -f6)"
RUN_AS_USER() {
  if [[ "$(id -u)" == "0" ]]; then
    sudo -u "${TARGET_USER}" XDG_RUNTIME_DIR="/run/user/$(id -u "${TARGET_USER}")" "$@"
  else
    "$@"
  fi
}

echo "== firmware =="
if [[ -r /sys/firmware/efi/fw_platform_size ]]; then
  size="$(cat /sys/firmware/efi/fw_platform_size)"
  if [[ "${size}" == "64" ]]; then ok "firmware is 64-bit UEFI"; else bad "fw_platform_size=${size} (must be 64)"; fi
else
  warn "/sys/firmware/efi/fw_platform_size missing (BIOS boot?)"
fi

echo "== sleep is dead =="
check_cmd "sleep.target masked" "[[ \$(systemctl is-enabled sleep.target 2>/dev/null || true) == masked ]]"
for t in suspend.target hibernate.target; do
  check_cmd "${t} masked" "[[ \$(systemctl is-enabled ${t} 2>/dev/null || true) == masked ]]"
done
check_cmd "lid-inhibit.service active" "[[ \$(systemctl is-active lid-inhibit.service 2>/dev/null) == active ]]"
grep_line="$(grep -s '^IgnoreLid' /etc/UPower/UPower.conf || true)"
if [[ "${grep_line}" == "IgnoreLid=true" ]]; then ok "UPower IgnoreLid=true"; else bad "UPower IgnoreLid (${grep_line:-missing})"; fi
check_cmd "systemd-logind lid ignore" "grep -qs HandleLidSwitch=ignore /etc/systemd/logind.conf.d/ignore-lid.conf"
check_cmd "acpid.service active" "[[ \$(systemctl is-active acpid.service 2>/dev/null) == active ]]"

echo "== charge cap =="
if cap="$(cat /sys/class/power_supply/BAT*/charge_control_end_threshold 2>/dev/null | head -1)" && [[ -n "${cap}" ]]; then
  if [[ "${cap}" -le 60 ]] 2>/dev/null; then ok "charge threshold ${cap}%"; else bad "charge threshold ${cap}% (>60)"; fi
else
  warn "no charge threshold sysfs — 2012 EC will sit at 100% on AC"
fi

echo "== proxy (retired on the box 2026-09-15) =="
if [[ ! -d /work/hardened-zai-proxy && ! -e "${TARGET_HOME}/.config/systemd/user/glm-proxy.service" ]]; then
  ok "hardened-zai-proxy retired on the box (key pool dead; worker is orca serve + qoder)"
else
  proxy_enabled="$(RUN_AS_USER systemctl --user is-enabled glm-proxy.service 2>/dev/null || true)"
  proxy_json="$(curl -fsS --max-time 5 http://127.0.0.1:18088/health 2>/dev/null || true)"
  if [[ -n "${proxy_json}" ]] && echo "${proxy_json}" | jq -e '.status=="ok" and .activeKeys>0' >/dev/null 2>&1; then
    keys="$(echo "${proxy_json}" | jq -r '.activeKeys')"
    ok "proxy healthy, activeKeys=${keys}"
  elif [[ "${proxy_enabled}" == "enabled" ]]; then
    bad "glm-proxy enabled but unhealthy on :18088"
  else
    warn "proxy not running on :18088 (glm-proxy not enabled)"
  fi
fi

echo "== zcode (retired on the box 2026-09-15) =="
if [[ ! -e /opt/ZCode && ! -e "${TARGET_HOME}/.zcode" ]]; then
  ok "ZCode retired on the box (worker is orca serve + qoder)"
else
  if pgrep -f '/zcode|[/ ]ZCode' >/dev/null 2>&1; then ok "zcode running"; else warn "zcode not running (worker is orca serve + qoder now)"; fi
  setting="${TARGET_HOME}/.zcode/v2/setting.json"
  worker_desktop="${TARGET_HOME}/.config/autostart/zcode-worker.desktop"
  # The running ZCode rewrites setting.json from memory, so json values
  # race with the app. The enforced guarantee is --disable-gpu on the
  # autostart chain: the .desktop may call a wrapper that carries it,
  # and Electron overwrites its own process title, so neither the bare
  # .desktop text nor /proc cmdline alone is reliable.
  flag_found=0
  if [[ -f "${worker_desktop}" ]] && grep -q -- "--disable-gpu" "${worker_desktop}"; then
    flag_found=1
  else
    worker_bin="$(grep '^Exec=' "${worker_desktop}" 2>/dev/null | head -1 | cut -d= -f2- | awk '{print $1}')"
    if [[ -n "${worker_bin}" && -f "${worker_bin}" ]] && grep -q -- "--disable-gpu" "${worker_bin}" 2>/dev/null; then
      flag_found=1
    fi
  fi
  if [[ "${flag_found}" == "1" ]]; then
    ok "software rendering enforced (--disable-gpu in autostart chain)"
  else
    warn "no --disable-gpu in ${worker_desktop} or its wrapper (zcode-only concern)"
  fi
  if [[ -f "${setting}" ]]; then
    # jq's // operator falls through on false; read raw and default only
    # when the key is absent — otherwise a correct `false` reads as `true`.
    keep="$(jq -r '.keepAwakeWhileRunning // empty' "${setting}" 2>/dev/null || true)"
    [[ -z "${keep}" ]] && keep=false
    if [[ "${keep}" == "true" ]]; then ok "keepAwakeWhileRunning=true"; else warn "keepAwakeWhileRunning!=true (app may rewrite it)"; fi
  else
    warn "${setting} missing (start ZCode once, then pin settings)"
  fi
fi

echo "== orca serve + qoder (worker stack) =="
check_cmd "orca-serve.service active (user unit)" "$(declare -f RUN_AS_USER); RUN_AS_USER systemctl --user is-active orca-serve.service 2>/dev/null | grep -qx active"
check_cmd "orca serve listening on :6768" "ss -tln | grep -q ':6768'"
check_cmd "orca web client answers on :6768" "curl -fsS --max-time 5 -o /dev/null http://127.0.0.1:6768/web-index.html"
check_cmd "orca-port-guard.service active" "[[ \$(systemctl is-active orca-port-guard.service 2>/dev/null) == active ]]"
check_cmd "orca port guard rule present" "nft list table inet spectre_guard 2>/dev/null | grep -q 'tcp dport 6768'"
check_cmd "qoder tmux session present" "$(declare -f RUN_AS_USER); RUN_AS_USER tmux has-session -t qoder 2>/dev/null"
check_cmd "qoder autoupdate pinned off" "jq -e '.general.enableAutoUpdate == false' ${TARGET_HOME}/.qoder/settings.json >/dev/null"

echo "== obscura (agent browser, RUNBOOK 7.12) =="
check_cmd "obscura CLI on PATH" "command -v obscura >/dev/null"
check_cmd "obscura-cdp.service active (user unit)" "$(declare -f RUN_AS_USER); RUN_AS_USER systemctl --user is-active obscura-cdp.service 2>/dev/null | grep -qx active"
check_cmd "obscura CDP answers on :9222" "curl -fsS --max-time 5 http://127.0.0.1:9222/json/version | grep -q webSocketDebuggerUrl"
check_cmd "obscura MCP in claude.json" "jq -e '.mcpServers.obscura' ${TARGET_HOME}/.claude.json >/dev/null"
check_cmd "obscura MCP in qoder settings" "jq -e '.mcpServers.obscura' ${TARGET_HOME}/.qoder/settings.json >/dev/null"
check_cmd "obscura MCP in codex config" "grep -qs '^\\[mcp_servers\\.obscura\\]' ${TARGET_HOME}/.codex/config.toml"

echo "== devcodex (workspace companion, RUNBOOK 7.13) =="
if command -v devcodex >/dev/null 2>&1; then
  ok "devcodex CLI on PATH"
  check_cmd "devcodex MCP handshake lists core tools" "printf '%s\n' '{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"initialize\",\"params\":{\"protocolVersion\":\"2025-11-25\",\"capabilities\":{},\"clientInfo\":{\"name\":\"doctor\",\"version\":\"0\"}}}' '{\"jsonrpc\":\"2.0\",\"method\":\"notifications/initialized\"}' '{\"jsonrpc\":\"2.0\",\"id\":2,\"method\":\"tools/list\"}' | devcodex mcp --root /usr/local/share/devcodex | tail -1 | grep -q task_bootstrap"
else
  warn "devcodex not installed (optional — RUNBOOK 7.13)"
fi

echo "== devspace connector (ChatGPT, RUNBOOK 7.14) =="
devspace_enabled="$(RUN_AS_USER systemctl --user is-enabled devspace.service 2>/dev/null || true)"
if [[ "${devspace_enabled}" == "enabled" ]]; then
  check_cmd "devspace.service active (user unit)" "$(declare -f RUN_AS_USER); RUN_AS_USER systemctl --user is-active devspace.service 2>/dev/null | grep -qx active"
  check_cmd "devspace CLI on PATH" "command -v devspace >/dev/null"
  # DevSpace advertises skill paths tilde-formatted but upstream resolved them
  # against the workspace root (ENOENT). Probe the installed module's
  # behaviour, not the file text: scripts/patch-devspace-tilde.sh re-applies
  # the fix after any devspace upgrade (RUNBOOK 7.14).
  devspace_dist="$(dirname "$(readlink -f "$(command -v devspace 2>/dev/null || echo /nonexistent)")")"
  check_cmd "devspace file tools expand ~ (tilde patch, RUNBOOK 7.14)" "$(declare -f RUN_AS_USER); RUN_AS_USER node --input-type=module -e 'import { resolveAllowedPath } from \"${devspace_dist}/roots.js\"; let probePath; try { probePath = resolveAllowedPath(\"~/probe\", \"/nonexistent-workspace\", [\"~\"]); } catch { process.exit(1); } process.exit(probePath.startsWith(\"/\") && probePath.endsWith(\"/probe\") && !probePath.startsWith(\"/nonexistent-workspace\") ? 0 : 1)'"
  dev_code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:7676/mcp 2>/dev/null || true)"
  if [[ "${dev_code}" == "401" ]]; then
    ok "devspace /mcp answers 401 without auth (fail-closed)"
  else
    bad "devspace /mcp answered '${dev_code:-nothing}' (want 401)"
  fi
  check_cmd "tailscale serve proxies to :7676" "tailscale serve status 2>/dev/null | grep -q '127.0.0.1:7676'"
else
  warn "devspace connector not enabled (optional — RUNBOOK 7.14)"
fi

echo "== chatgpt handoff bridge (optional, RUNBOOK 7.9) =="
bridge_enabled="$(RUN_AS_USER systemctl --user is-enabled codexpro-handoff.service 2>/dev/null || true)"
if [[ "${bridge_enabled}" == "enabled" ]]; then
  check_cmd "codexpro-handoff.service active (user unit)" "$(declare -f RUN_AS_USER); RUN_AS_USER systemctl --user is-active codexpro-handoff.service 2>/dev/null | grep -qx active"
  check_cmd "codexpro on PATH" "command -v codexpro >/dev/null"
  bridge_code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:8787/mcp 2>/dev/null || true)"
  if [[ "${bridge_code}" == "401" ]]; then
    ok "bridge /mcp answers 401 without a token (fail-closed)"
  else
    bad "bridge /mcp answered '${bridge_code:-nothing}' (want 401)"
  fi
  if pgrep -f '(^|[/\ ])cloudflared' >/dev/null 2>&1; then
    ok "cloudflared tunnel process running"
  else
    bad "cloudflared not running — ChatGPT cannot reach the bridge"
  fi
  if [[ -f "${TARGET_HOME}/.codexpro/http-token" ]]; then
    ok "bridge http-token file present"
  else
    bad "codexpro http-token missing (${TARGET_HOME}/.codexpro/http-token)"
  fi
else
  warn "codexpro-handoff not enabled (bridge not installed — RUNBOOK 7.9)"
fi

echo "== slack agent community (optional, RUNBOOK 7.10) =="
slack_env="${TARGET_HOME}/.config/remote-agent/slack.env"
if [[ -f "${slack_env}" ]]; then
  mode="$(stat -c '%a' "${slack_env}" 2>/dev/null || true)"
  if [[ "${mode}" == "600" ]]; then ok "slack.env mode 600"; else bad "slack.env mode ${mode:-unknown} (want 600)"; fi
  # Shape-only checks; values are never printed.
  if grep -qs '^SLACK_BOT_TOKEN=xoxb-' "${slack_env}"; then ok "slack.env bot token shape ok"; else bad "slack.env bot token missing (xoxb-)"; fi
  if grep -qs '^SLACK_APP_TOKEN=xapp-' "${slack_env}"; then ok "slack.env app token shape ok"; else bad "slack.env app token missing (xapp-)"; fi
  if command -v node >/dev/null 2>&1; then ok "node present (bridge runtime)"; else bad "node missing — slack-bridge cannot run"; fi
  # Executor: the box-local qoder-efficient wrapper (qodercli, Efficient
  # model) with its cost gate. Claude is NOT the executor (upstream auth
  # dead 2026-09-12); the bridge spawns this absolute path, not $PATH.
  if RUN_AS_USER test -x "${TARGET_HOME}/.local/bin/qoder-efficient"; then
    ok "qoder-efficient wrapper present (bridge executor)"
  else
    bad "executor wrapper missing at \$HOME/.local/bin/qoder-efficient — executor runs will fail"
  fi
  check_cmd "executor settings installed (slack-executor-settings.json)" "test -f /usr/local/share/remote-agent/slack-executor-settings.json"
  # Cost gate: the wrapper refuses (exit 75) when Efficient is not free; its
  # snapshot records what the runner last saw.
  rate_json="${TARGET_HOME}/.local/state/remote-agent/qoder-efficient-rate.json"
  if RUN_AS_USER jq -e '.is_free == true' "${rate_json}" >/dev/null 2>&1; then
    ok "qoder-efficient cost gate: free at last check"
  else
    warn "cost gate snapshot missing/not-free — executor runs may be refused (exit 75)"
  fi
  # #lobby debate (SLACK_DEBATE on): the second responder is the agy CLI.
  # Missing pieces do not break qoder turns — debate turns are skipped and
  # audited (debate_skip) — but with the switch on they are misconfiguration.
  # Parse the switch the way the bridge does (last key wins, trimmed value,
  # truthy = 1/true/yes/on) so the doctor can never disagree with the daemon.
  debate_val="$(sed -nE 's/^[[:space:]]*SLACK_DEBATE[[:space:]]*=[[:space:]]*(.*)$/\1/p' "${slack_env}" | tail -n1 | sed -E 's/^[[:space:]]+|[[:space:]]+$//g')"
  case "${debate_val,,}" in
    1|true|yes|on) debate_on=1 ;;
    *) debate_on=0 ;;
  esac
  # SLACK_AGY_BIN overrides the default bin (bridge: expandHome, default
  # ~/.local/bin/agy; a non-absolute value refuses startup).
  agy_bin="$(sed -nE 's/^[[:space:]]*SLACK_AGY_BIN[[:space:]]*=[[:space:]]*(.*)$/\1/p' "${slack_env}" | tail -n1 | sed -E 's/^[[:space:]]+|[[:space:]]+$//g')"
  # The "~" patterns below match a LITERAL leading tilde on purpose: the value
  # comes from slack.env, where "~" is a string, not a shell expansion.
  # shellcheck disable=SC2088
  case "${agy_bin}" in
    "") agy_bin="${TARGET_HOME}/.local/bin/agy" ;;
    "~") agy_bin="${TARGET_HOME}" ;;
    "~/"*) agy_bin="${TARGET_HOME}/${agy_bin:2}" ;;
  esac
  if (( debate_on )); then
    if [[ "${agy_bin}" != /* ]]; then
      bad "SLACK_AGY_BIN is not absolute (${agy_bin}) — the bridge refuses to start"
    elif RUN_AS_USER test -f "${agy_bin}" && RUN_AS_USER test -x "${agy_bin}"; then
      ok "agy binary present (lobby debate second voice)"
    else
      bad "SLACK_DEBATE on but agy missing/not executable (${agy_bin}) — debate turns skip"
    fi
    if RUN_AS_USER test -f "${TARGET_HOME}/.gemini/antigravity-cli/settings.json"; then
      ok "agy read-only settings profile present"
    else
      bad "SLACK_DEBATE on but ~/.gemini/antigravity-cli/settings.json missing"
    fi
    if grep -qs '"antigravity"' /usr/local/share/remote-agent/slack-agents.json 2>/dev/null; then
      ok "antigravity identity registered"
    else
      bad "antigravity missing from slack-agents.json — agy posts would be ignored"
    fi
  else
    warn "lobby debate off — single responder (SLACK_DEBATE unset or off)"
  fi
  check_cmd "slack-bridge.service active (user unit)" "$(declare -f RUN_AS_USER); RUN_AS_USER systemctl --user is-active slack-bridge.service 2>/dev/null | grep -qx active"
  check_cmd "slack-brief.timer enabled (user unit)" "$(declare -f RUN_AS_USER); RUN_AS_USER systemctl --user is-enabled slack-brief.timer 2>/dev/null | grep -qx enabled"
  if command -v spectre-slack-notify >/dev/null 2>&1; then
    if RUN_AS_USER spectre-slack-notify --self-test >/dev/null 2>&1; then
      ok "spectre-slack-notify self-test OK"
    else
      bad "spectre-slack-notify --self-test failed"
    fi
  else
    warn "spectre-slack-notify not installed"
  fi
  # Goal supervisor (RUNBOOK 7.16): the Grokbot that reviews a stopped /goal
  # unit and dispatches the next one. Installed off; each missing piece has its
  # own remedy line, and the reviewer fails closed rather than guessing.
  if [[ -x /usr/local/bin/spectre-goal-supervisor ]]; then
    ok "spectre-goal-supervisor installed"
  else
    warn "spectre-goal-supervisor not installed (goal handoff stays manual)"
  fi
  if RUN_AS_USER test -x "${TARGET_HOME}/.local/bin/grok"; then
    if grep -qs '^disabled_mcp_servers' "${TARGET_HOME}/.local/share/remote-agent/grok-supervisor/config.toml" 2>/dev/null; then
      ok "grok reviewer profile present (MCP scanning off)"
    else
      warn "grok reviewer profile missing or overwritten by a self-init stub — reviewer would load vendor MCP/config (RUNBOOK 7.16)"
    fi
    # Two ways to be authenticated, and the reviewer needs exactly one of them.
    # The shipped profile has no [auth] pin, so a device-code session token is
    # the default and works as-is. A pinned api_key (the no-login alternative)
    # has no fallthrough, so a token alone then fails; and a key without the pin
    # is refused by grok as "Not signed in" (verified 2026-09-15).
    pinned_api_key=0
    if grep -qs '^preferred_method *= *"api_key"' "${TARGET_HOME}/.local/share/remote-agent/grok-supervisor/config.toml" 2>/dev/null; then
      pinned_api_key=1
    fi
    has_token=0
    has_key=0
    RUN_AS_USER test -f "${TARGET_HOME}/.local/share/remote-agent/grok-supervisor/auth.json" && has_token=1
    RUN_AS_USER grep -qs 'XAI_API_KEY=.' "${TARGET_HOME}/.config/remote-agent/grok.env" 2>/dev/null && has_key=1
    if (( has_key && has_token )); then
      warn "both a session token and grok.env exist in the reviewer home — use one (see RUNBOOK 7.16)"
    fi
    if (( has_key )); then
      ok "grok reviewer key configured (no login needed)"
      if (( ! pinned_api_key )); then
        warn "key present but preferred_method is not pinned to api_key — grok refuses it as 'Not signed in'"
      fi
    elif (( has_token )); then
      if (( pinned_api_key )); then
        warn "session token present but the profile pins api_key — the token is ignored (comment out [auth])"
      else
        ok "grok session token present for the isolated GROK_HOME"
      fi
    else
      warn "grok not authenticated — reviews fail closed (grok login --device-code, or XAI_API_KEY in ~/.config/remote-agent/grok.env)"
    fi
  else
    warn "grok not installed — goal supervisor has no reviewer (handoff stays manual)"
  fi
  if RUN_AS_USER systemctl --user is-enabled goal-supervisor.timer >/dev/null 2>&1; then
    if grep -qs '^Environment=SPECTRE_GOAL_SUPERVISOR=1' "${TARGET_HOME}/.config/systemd/user/goal-supervisor.service" 2>/dev/null; then
      ok "goal-supervisor.timer enabled (autonomous handoff on)"
    else
      warn "goal-supervisor.timer enabled but the unit's kill switch is off (no-op)"
    fi
  else
    warn "goal-supervisor.timer disabled — parked goals wait for you"
  fi
else
  warn "slack.env missing — Slack community not configured (RUNBOOK 7.10)"
fi

echo "== worker-state (RUNBOOK 7.17, the resolver every consumer asks) =="
if command -v spectre-state >/dev/null 2>&1; then
  ok "spectre-state CLI on PATH"
  ws_enabled="$(RUN_AS_USER systemctl --user is-enabled spectre-worker-state.service 2>/dev/null || true)"
  if [[ "${ws_enabled}" == "enabled" ]]; then
    check_cmd "spectre-worker-state.service active (user unit)" "$(declare -f RUN_AS_USER); RUN_AS_USER systemctl --user is-active spectre-worker-state.service 2>/dev/null | grep -qx active"
    # `spectre-state health` is a raw read; the socket can still refuse under
    # load. What matters for dispatch is `get`: an UNKNOWN snapshot is
    # fail-closed (every policy flag false), which refuses /goal and /resume.
    if RUN_AS_USER spectre-state health >/dev/null 2>&1; then
      ok "spectre-state health"
    else
      bad "spectre-state health failed (socket down?) — consumers fail closed"
    fi
    ws_probe="$(RUN_AS_USER spectre-state get qoder 2>/dev/null || true)"
    if [[ -z "${ws_probe}" ]]; then
      bad "spectre-state get failed — consumers fail closed"
    else
      ws_reason="$(printf '%s' "${ws_probe}" | python3 -c "import json,sys;print(json.load(sys.stdin).get('reason') or '')" 2>/dev/null || true)"
      if [[ "${ws_reason}" == state\ api\ unavailable* || "${ws_reason}" == *timed\ out* ]]; then
        bad "spectre-state get returns UNKNOWN (${ws_reason}) — dispatch and resume are refused"
      else
        ok "spectre-state get serves a real snapshot"
      fi
    fi
  else
    warn "spectre-worker-state.service not enabled (resolver off — Slack cannot dispatch)"
  fi
else
  warn "spectre-state not installed (RUNBOOK 7.17)"
fi
# Retired with the cutover: each of these classified occupancy on its own and
# could type a second, contradictory goal. Enabled again = a regression.
for ws_legacy in qoder-nudge.timer qoder-continuity.timer \
                 codex-goal-healer.timer grokbot-goal-event.timer; do
  ws_legacy_state="$(RUN_AS_USER systemctl --user is-enabled "${ws_legacy}" 2>/dev/null || true)"
  if [[ "${ws_legacy_state}" == "enabled" ]]; then
    bad "${ws_legacy} enabled — retired classifier back on (RUNBOOK 7.17)"
  else
    ok "${ws_legacy} disabled"
  fi
done
ws_gs="$(RUN_AS_USER systemctl --user is-enabled goal-supervisor.timer 2>/dev/null || true)"
if [[ "${ws_gs}" == "enabled" ]]; then
  ok "goal-supervisor.timer enabled (Grokbot active, RUNBOOK 7.16)"
else
  warn "goal-supervisor.timer disabled — Grokbot advances nothing (installed off)"
fi

echo "== codex CLI + session handoff (RUNBOOK 7.11, 7.15) =="
if command -v codex >/dev/null 2>&1; then
  codex_ver="$(codex --version 2>/dev/null || echo 'version unknown')"
  ok "codex CLI on PATH (${codex_ver})"
else
  bad "codex CLI missing — RUNBOOK 7.11"
fi
check_cmd "codex config.toml present" "test -f ${TARGET_HOME}/.codex/config.toml"
check_cmd "codex third-party provider block present" "grep -qs '^\\[model_providers\\.' ${TARGET_HOME}/.codex/config.toml"
check_cmd "codex irreversible-guard hook installed" "test -f ${TARGET_HOME}/.codex/hooks/irreversible-guard.mjs"
# The guard is the only barrier left on the Codex path (approvals off, sandbox
# full-access, no read-guard), so prove both directions: an irreversible command
# is refused (exit 2) and ordinary work passes (exit 0).
check_cmd "codex guard blocks an irreversible command" "printf '%s' '{\"tool_name\":\"exec_command\",\"tool_input\":{\"command\":\"git merge main\"}}' | SPECTRE_WORKER_PROFILE=box node ${TARGET_HOME}/.codex/hooks/irreversible-guard.mjs; [[ \$? -eq 2 ]]"
check_cmd "codex guard allows ordinary work" "printf '%s' '{\"tool_name\":\"exec_command\",\"tool_input\":{\"command\":\"ls -la\"}}' | SPECTRE_WORKER_PROFILE=box node ${TARGET_HOME}/.codex/hooks/irreversible-guard.mjs"
check_cmd "codex read-guard removed (box posture 2026-09-16)" "test ! -f ${TARGET_HOME}/.codex/hooks/read-guard.mjs"
# codex-mode is user-level (~/.local/bin), which root's PATH does not carry —
# test the installed path as the user, the way the executor check does.
check_cmd "codex-mode installed (~/.local/bin, user)" "$(declare -f RUN_AS_USER); RUN_AS_USER test -x ${TARGET_HOME}/.local/bin/codex-mode"
check_cmd "codex sessions dir present" "test -d ${TARGET_HOME}/.codex/sessions"
if command -v codex-handoff >/dev/null 2>&1; then
  ok "codex-handoff on PATH (session handoff)"
else
  warn "codex-handoff not on PATH (optional — RUNBOOK 7.15)"
fi
check_cmd "rollout helper installed (/usr/local/lib/spectre-codex)" "test -f /usr/local/lib/spectre-codex/codex_rollout.py"
if [[ -s "${TARGET_HOME}/.local/state/spectre-codex-handoff/last.json" ]]; then
  ok "codex session handoff used from this box"
else
  warn "no codex session handoff run from this box yet (optional — RUNBOOK 7.15)"
fi

echo "== network reachability =="
check_cmd "tailscale backend Running" "[[ \$(tailscale status --json 2>/dev/null | jq -r .BackendState) == Running ]]"
ts_peers="$(tailscale status 2>/dev/null | grep -cE 'fedora|spectre|z-fold7' || true)"
if (( ts_peers >= 2 )); then ok "tailnet peers visible (${ts_peers})"; else warn "expected fedora+z-fold7 in tailscale status"; fi

echo "== firewall and ssh (harden-network.sh) =="
if command -v ufw >/dev/null 2>&1 && [[ "$(ufw status 2>/dev/null | awk '{print $1}')" == "Status: active" ]]; then
  ok "ufw active"
else
  warn "ufw not active — run: sudo bash scripts/harden-network.sh"
fi
pw="$(sshd -T 2>/dev/null | awk '/^passwordauthentication/ {print $2}')"
if [[ "${pw}" == "no" ]]; then ok "ssh password auth disabled"; else warn "ssh password auth ${pw:-unknown} — run harden-network.sh after pulling keys"; fi
if systemctl is-active --quiet fail2ban.service 2>/dev/null; then ok "fail2ban active"; else warn "fail2ban not running"; fi

echo "== disk and logs =="
check_cmd "/work mounted" "findmnt -n /work"
jcap="$(grep -s '^SystemMaxUse' /etc/systemd/journald.conf.d/*.conf 2>/dev/null | head -1 || true)"
if [[ -n "${jcap}" ]]; then ok "journal capped (${jcap})"; else warn "journald not capped — bootstrap re-run needed"; fi
# Debian calls the unit smartmontools.service; other distros smartd.service.
if systemctl is-active --quiet smartmontools.service 2>/dev/null || systemctl is-active --quiet smartd.service 2>/dev/null; then
  ok "smart monitoring active (smartmontools/smartd)"
else
  warn "smart monitoring not running"
fi
if [[ -e /etc/logrotate.d/work-logs ]]; then ok "logrotate for /work/logs installed"; else warn "logrotate rule missing"; fi

echo "== monitoring =="
hb="${TARGET_HOME}/.local/state/remote-agent/heartbeat"
if [[ -f "${hb}" ]]; then
  age=$(( $(date +%s) - $(stat -c %Y "${hb}") ))
  if (( age < 300 )); then ok "heartbeat fresh (${age}s old)"; else bad "heartbeat stale (${age}s)"; fi
else
  warn "no heartbeat yet — enable worker-health.timer"
fi
check_cmd "worker-health.timer active" "$(declare -f RUN_AS_USER); RUN_AS_USER systemctl --user is-active worker-health.timer 2>/dev/null | grep -qx active"

echo
echo "summary: ${PASS} passed, ${FAIL} failed"
(( FAIL == 0 )) || exit 1
