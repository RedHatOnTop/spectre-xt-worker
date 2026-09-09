#!/bin/bash
# One screen for a phone SSH session.
set -u

echo "host     $(hostname -s)"
echo "time     $(date '+%Y-%m-%d %H:%M %Z')"
echo "uptime   $(uptime -p 2>/dev/null || uptime)"
echo "load     $(cut -d' ' -f1-3 /proc/loadavg)"
echo "mem      $(free -h | awk '/Mem:/{print $3" / "$2}')"
echo "disk /   $(df -hP / | awk 'NR==2{print $3" / "$2"  "$5}')"
if findmnt -n /work >/dev/null 2>&1; then
  echo "disk /work $(df -hP /work | awk 'NR==2{print $3" / "$2"  "$5}')"
fi

if command -v tailscale >/dev/null 2>&1; then
  echo "tailscale $(tailscale status --json 2>/dev/null | jq -r '.BackendState // "down"')"
fi

if curl -fsS --max-time 2 http://127.0.0.1:18088/health >/tmp/spectre-health.json 2>/dev/null; then
  jq -r '"proxy    \(.status) keys=\(.activeKeys)/\(.totalKeys // .activeKeys)"' /tmp/spectre-health.json
else
  echo "proxy    down"
fi

if pgrep -f '/zcode|[/ ]ZCode' >/dev/null 2>&1; then
  echo "zcode    running"
else
  echo "zcode    not running"
fi

if pgrep -x qodercli >/dev/null 2>&1; then
  echo "qoder    $(pgrep -c -x qodercli) qodercli"
else
  echo "qoder    not running"
fi
if [[ -f "${HOME}/.local/state/remote-agent/qoder-efficient-billed" ]]; then
  echo "efficient STOPPED billed-sentinel"
elif [[ -f "${HOME}/.local/state/remote-agent/qoder-efficient-rate.json" ]]; then
  jq -r '"efficient \(.status) price_factor=\(.price_factor // "unknown")"' \
    "${HOME}/.local/state/remote-agent/qoder-efficient-rate.json" 2>/dev/null || true
fi

if [[ -e /sys/class/power_supply/BAT0/capacity ]]; then
  cap="$(cat /sys/class/power_supply/BAT0/capacity)"
  end="$(cat /sys/class/power_supply/BAT0/charge_control_end_threshold 2>/dev/null || echo none)"
  echo "battery  ${cap}%  cap=${end}"
fi

if command -v sensors >/dev/null 2>&1; then
  sensors -j 2>/dev/null | jq -r '[.. | objects | .temp1_input? // .temp2_input? // empty] | map(select(. != null)) | max | "temp     \(.)C"' 2>/dev/null || true
fi

echo "sleep    $(systemctl is-enabled sleep.target 2>/dev/null || echo unknown)"
echo "lid      $(systemctl is-active lid-inhibit.service 2>/dev/null || echo unknown)"
