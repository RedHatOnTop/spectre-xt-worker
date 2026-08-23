#!/bin/bash
# Exit 0 always so the timer keeps ticking. Failures go to the log + ntfy.
set -u

LOG_DIR="${LOG_DIR:-/work/logs}"
LOG="${LOG_DIR}/health.log"
STATE_DIR="${XDG_STATE_HOME:-${HOME}/.local/state}/remote-agent"
install -d -m 0755 "${LOG_DIR}" "${STATE_DIR}" 2>/dev/null || true

PROXY_HEALTH_URL="${PROXY_HEALTH_URL:-http://127.0.0.1:18088/health}"
NTFY_URL="${NTFY_URL:-https://ntfy.sh}"
NTFY_TOPIC="${NTFY_TOPIC:-}"
TEMP_FAIL_C="${TEMP_FAIL_C:-85}"
DISK_FAIL_PCT="${DISK_FAIL_PCT:-90}"

ts() { date -u +%Y-%m-%dT%H:%M:%SZ; }
fail_bits=()

note() {
  printf '%s %s\n' "$(ts)" "$*" >>"${LOG}"
}

notify() {
  local body="$1"
  note "FAIL ${body}"
  [[ -z "${NTFY_TOPIC}" ]] && return 0
  curl -fsS -d "${body}" \
    -H "Title: spectre worker" \
    -H "Priority: high" \
    -H "Tags: warning" \
    "${NTFY_URL%/}/${NTFY_TOPIC}" >/dev/null 2>&1 || true
}

# Proxy
proxy_json="$(curl -fsS --max-time 5 "${PROXY_HEALTH_URL}" 2>/dev/null || true)"
if [[ -z "${proxy_json}" ]]; then
  fail_bits+=("proxy_down")
else
  status="$(printf '%s' "${proxy_json}" | jq -r '.status // empty' 2>/dev/null || true)"
  keys="$(printf '%s' "${proxy_json}" | jq -r '.activeKeys // 0' 2>/dev/null || echo 0)"
  if [[ "${status}" != "ok" ]]; then
    fail_bits+=("proxy_status=${status:-empty}")
  fi
  if [[ "${keys}" == "0" ]]; then
    fail_bits+=("activeKeys=0")
  fi
fi

# ZCode electron
if ! pgrep -f '/zcode|ZCode' >/dev/null 2>&1; then
  fail_bits+=("zcode_missing")
fi

# Temperature: first package/Tdie reading that looks like a number.
temp_c=""
if command -v sensors >/dev/null 2>&1; then
  temp_c="$(sensors -j 2>/dev/null | jq '[.. | objects | .temp1_input? // .temp2_input? // empty] | map(select(. != null)) | max' 2>/dev/null || true)"
fi
if [[ -n "${temp_c}" && "${temp_c}" != "null" ]]; then
  if awk "BEGIN {exit !(${temp_c} >= ${TEMP_FAIL_C})}"; then
    fail_bits+=("temp=${temp_c}C")
  fi
fi

# Disk
for mount in / /work; do
  [[ -d "${mount}" ]] || continue
  pct="$(df -P "${mount}" | awk 'NR==2 {gsub(/%/,"",$5); print $5}')"
  if [[ -n "${pct}" ]] && (( pct >= DISK_FAIL_PCT )); then
    fail_bits+=("disk:${mount}=${pct}%")
  fi
done

# Tailscale
if command -v tailscale >/dev/null 2>&1; then
  backend="$(tailscale status --json 2>/dev/null | jq -r '.BackendState // empty' || true)"
  if [[ "${backend}" != "Running" ]]; then
    fail_bits+=("tailscale=${backend:-down}")
  fi
fi

state_file="${STATE_DIR}/last-fail"
if (( ${#fail_bits[@]} > 0 )); then
  joined="$(IFS=','; echo "${fail_bits[*]}")"
  last="$(cat "${state_file}" 2>/dev/null || true)"
  if [[ "${joined}" != "${last}" ]]; then
    notify "${joined}"
    printf '%s\n' "${joined}" >"${state_file}"
  else
    note "STILL ${joined}"
  fi
else
  if [[ -f "${state_file}" ]]; then
    recovered="$(cat "${state_file}")"
    rm -f "${state_file}"
    note "OK recovered from ${recovered}"
    if [[ -n "${NTFY_TOPIC}" ]]; then
      curl -fsS -d "recovered from ${recovered}" \
        -H "Title: spectre worker" \
        -H "Tags: white_check_mark" \
        "${NTFY_URL%/}/${NTFY_TOPIC}" >/dev/null 2>&1 || true
    fi
  fi
fi

exit 0
