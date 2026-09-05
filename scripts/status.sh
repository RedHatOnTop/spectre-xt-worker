#!/bin/bash
# One screen for a phone SSH session. --json emits the same facts as a
# single JSON object for tooling (and a future mobile app).
set -u

MODE="${1:-text}"

json_escape() { jq -Rn --arg v "$1" '$v'; }

host="$(hostname -s)"
uptime_p="$(uptime -p 2>/dev/null || uptime)"
load1="$(cut -d' ' -f1 /proc/loadavg)"
mem_used="$(free -m | awk '/Mem:/{print $3}')"
mem_total="$(free -m | awk '/Mem:/{print $2}')"
disk_root="$(df -P / | awk 'NR==2{gsub(/%/,"",$5); print $5}')"

disk_work=""
if findmnt -n /work >/dev/null 2>&1; then
  disk_work="$(df -P /work | awk 'NR==2{gsub(/%/,"",$5); print $5}')"
fi

tailscale_state="not_installed"
if command -v tailscale >/dev/null 2>&1; then
  tailscale_state="$(tailscale status --json 2>/dev/null | jq -r '.BackendState // "down"')"
fi

proxy_status="down"
proxy_keys="0"
if curl -fsS --max-time 2 http://127.0.0.1:18088/health >/tmp/spectre-health.json 2>/dev/null; then
  proxy_status="$(jq -r '.status // "unknown"' /tmp/spectre-health.json)"
  proxy_keys="$(jq -r '.activeKeys // 0' /tmp/spectre-health.json)"
fi

if pgrep -f '/zcode|[/ ]ZCode' >/dev/null 2>&1; then
  zcode_state="running"
else
  zcode_state="not_running"
fi

if pgrep -f '/usr/local/bin/claude|/usr/bin/claude' >/dev/null 2>&1; then
  claude_state="running"
else
  claude_state="not_running"
fi

ac_state=""
for ac_node in /sys/class/power_supply/A*/online; do
  [[ -r "${ac_node}" ]] || continue
  if [[ "$(cat "${ac_node}")" == "1" ]]; then ac_state="on"; else ac_state="off"; fi
done

battery_cap=""
battery_threshold=""
bat_cap_node="/sys/class/power_supply/BAT0/capacity"
if [[ -r "${bat_cap_node}" ]]; then
  battery_cap="$(cat "${bat_cap_node}")"
  battery_threshold="$(cat /sys/class/power_supply/BAT0/charge_control_end_threshold 2>/dev/null || echo none)"
fi

temp_c=""
if command -v sensors >/dev/null 2>&1; then
  temp_c="$(sensors -j 2>/dev/null | jq -r '[.. | objects | .temp1_input? // .temp2_input? // empty] | map(select(. != null)) | max | if . == null then "" else tostring end' 2>/dev/null || true)"
fi

heartbeat_age=""
hb="${HOME}/.local/state/remote-agent/heartbeat"
if [[ -f "${hb}" ]]; then
  heartbeat_age=$(( $(date +%s) - $(stat -c %Y "${hb}") ))
fi

sleep_state="$(systemctl is-enabled sleep.target 2>/dev/null || echo unknown)"
lid_state="$(systemctl is-active lid-inhibit.service 2>/dev/null || echo unknown)"

if [[ "${MODE}" == "--json" ]]; then
  jq -n \
    --arg host "${host}" \
    --arg uptime "${uptime_p}" \
    --arg load1 "${load1}" \
    --argjson memUsed "${mem_used:-0}" \
    --argjson memTotal "${mem_total:-0}" \
    --argjson diskRoot "${disk_root:-0}" \
    --argjson diskWork "${disk_work:-null}" \
    --arg tailscale "${tailscale_state}" \
    --arg proxyStatus "${proxy_status}" \
    --argjson proxyKeys "${proxy_keys:-0}" \
    --arg zcode "${zcode_state}" \
    --arg claude "${claude_state}" \
    --arg ac "${ac_state}" \
    --argjson battery "${battery_cap:-null}" \
    --arg batteryThreshold "${battery_threshold}" \
    --argjson temp "${temp_c:-null}" \
    --argjson heartbeatAge "${heartbeat_age:-null}" \
    --arg sleepState "${sleep_state}" \
    --arg lid "${lid_state}" \
    '{
      host: $host, uptime: $uptime, load1: $load1,
      mem: {usedMb: $memUsed, totalMb: $memTotal},
      disk: {rootPct: $diskRoot, workPct: $diskWork},
      tailscale: $tailscale,
      proxy: {status: $proxyStatus, activeKeys: $proxyKeys},
      zcode: $zcode, claude: $claude, ac: $ac,
      battery: {capacity: $battery, chargeLimit: $batteryThreshold},
      tempC: $temp, heartbeatAgeSec: $heartbeatAge,
      sleepTarget: $sleepState, lidInhibit: $lid
    }'
  exit 0
fi

echo "host     ${host}"
echo "time     $(date '+%Y-%m-%d %H:%M %Z')"
echo "uptime   ${uptime_p}"
echo "load     ${load1}"
echo "mem      ${mem_used}M / ${mem_total}M"
echo "disk /   ${disk_root}%"
if [[ -n "${disk_work}" ]]; then
  echo "disk /work ${disk_work}%"
fi
echo "tailscale ${tailscale_state}"
echo "proxy    ${proxy_status} keys=${proxy_keys}"
echo "zcode    ${zcode_state}"
echo "claude   ${claude_state}"
[[ -n "${ac_state}" ]] && echo "ac       ${ac_state}"
if [[ -n "${battery_cap}" ]]; then
  echo "battery  ${battery_cap}%  cap=${battery_threshold:-none}"
fi
if [[ -n "${temp_c}" ]]; then
  echo "temp     ${temp_c}C"
fi
if [[ -n "${heartbeat_age}" ]]; then
  echo "health   probe ${heartbeat_age}s ago"
else
  echo "health   no heartbeat yet (timer not running?)"
fi
echo "sleep    ${sleep_state}"
echo "lid      ${lid_state}"
