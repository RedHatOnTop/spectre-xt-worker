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

echo "== proxy (optional since the orca+qoder stack) =="
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

echo "== zcode (optional since the orca+qoder stack) =="
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

echo "== orca serve + qoder (worker stack) =="
check_cmd "orca-serve.service active (user unit)" "$(declare -f RUN_AS_USER); RUN_AS_USER systemctl --user is-active orca-serve.service 2>/dev/null | grep -qx active"
check_cmd "orca serve listening on :6768" "ss -tln | grep -q ':6768'"
check_cmd "orca web client answers on :6768" "curl -fsS --max-time 5 -o /dev/null http://127.0.0.1:6768/web-index.html"
check_cmd "orca-port-guard.service active" "[[ \$(systemctl is-active orca-port-guard.service 2>/dev/null) == active ]]"
check_cmd "orca port guard rule present" "nft list table inet spectre_guard 2>/dev/null | grep -q 'tcp dport 6768'"
check_cmd "qoder tmux session present" "$(declare -f RUN_AS_USER); RUN_AS_USER tmux has-session -t qoder 2>/dev/null"
check_cmd "qoder autoupdate pinned off" "jq -e '.general.enableAutoUpdate == false' ${TARGET_HOME}/.qoder/settings.json >/dev/null"

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
    bad "~/.codexpro/http-token missing"
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
    bad "~/.local/bin/qoder-efficient missing — executor runs will fail"
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
else
  warn "slack.env missing — Slack community not configured (RUNBOOK 7.10)"
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
