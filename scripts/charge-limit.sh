#!/bin/bash
# Cap charge at 60% when the EC exposes a sysfs threshold.
# 2012 HP Spectre XT TouchSmart often has no such node. Exit 0 either way;
# bootstrap must not fail because the firmware is old.
set -u

LIMIT="${CHARGE_LIMIT:-60}"
START="${CHARGE_START:-50}"
LOG="${CHARGE_LIMIT_LOG:-/work/logs/charge-limit.log}"
install -d -m 0755 "$(dirname "${LOG}")" 2>/dev/null || true

ts() { date -u +%Y-%m-%dT%H:%M:%SZ; }
note() { printf '%s %s\n' "$(ts)" "$*" | tee -a "${LOG}" 2>/dev/null || printf '%s %s\n' "$(ts)" "$*"; }

wrote=0
found=0

write_node() {
  local node="$1" value="$2"
  [[ -e "${node}" ]] || return 1
  found=1
  if ! echo "${value}" >"${node}" 2>/dev/null; then
    note "cannot write ${value} to ${node}"
    return 1
  fi
  local now
  now="$(tr -d '[:space:]' <"${node}" 2>/dev/null || true)"
  note "wrote ${value} -> ${node} (now ${now})"
  wrote=1
}

for bat in /sys/class/power_supply/BAT*; do
  [[ -d "${bat}" ]] || continue
  write_node "${bat}/charge_control_start_threshold" "${START}" || true
  write_node "${bat}/charge_control_end_threshold" "${LIMIT}" || true
  write_node "${bat}/charge_stop_threshold" "${LIMIT}" || true
done

if (( found == 0 )); then
  note "no charge threshold sysfs on this chassis"
  note "check BIOS Power / Battery Health Manager if it exists; otherwise the 2012 EC will sit at 100% on AC"
  echo "UNSUPPORTED"
  exit 0
fi

if (( wrote == 1 )); then
  echo "OK ${LIMIT}"
  exit 0
fi

echo "PRESENT_BUT_READ_ONLY"
exit 0
