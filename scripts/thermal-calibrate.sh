#!/bin/bash
# Measure package temperature vs intel_pstate ceiling under a fixed 4-thread load.
#
# Purpose: pick a CPU_MAX_PERF_ON_AC value that stays quiet on the 2012 Spectre XT
# (2C/4T, 17 W). The EC owns the fan and exposes no tachometer (hp hwmon pwm1 is
# N/A), so temperature is the only available proxy for audibility.
#
# Read-only except for /sys/devices/system/cpu/intel_pstate/max_perf_pct, which is
# restored to its original value on exit (trap). Run as root.
#
# Usage (on the Spectre):
#   sudo LEVELS="55 45 35 30 25" STEP_SEC=75 scripts/thermal-calibrate.sh
#
# Output: one log at $OUT (default ~person/thermal-calibration-<ts>.log) with a
# per-level summary: steady-state (last third of the step) package temperature,
# sampled frequency, and the kernel throttle counters.
set -uo pipefail

LEVELS="${LEVELS:-55 45 35 30 25}"
STEP_SEC="${STEP_SEC:-75}"
SAMPLE_SEC="${SAMPLE_SEC:-5}"
LOAD_PROCS="${LOAD_PROCS:-4}"
OUT="${OUT:-$HOME/thermal-calibration-$(date +%Y%m%dT%H%M%S).log}"
PSTATE=/sys/devices/system/cpu/intel_pstate

if [[ "$(id -u)" -ne 0 ]]; then
  echo "must run as root (writes $PSTATE/max_perf_pct)" >&2
  exit 1
fi

# Discover the coretemp "Package id 0" input; hwmon numbering is not stable.
PKG_TEMP=""
for h in /sys/class/hwmon/hwmon*; do
  [[ "$(cat "$h/name" 2>/dev/null)" == "coretemp" ]] || continue
  for t in "$h"/temp*_input; do
    [[ -e "$t" ]] || continue
    label="$(cat "${t%_input}_label" 2>/dev/null || true)"
    if [[ "$label" == "Package id 0" ]]; then
      PKG_TEMP="$t"
      break 2
    fi
  done
done
if [[ -z "$PKG_TEMP" ]]; then
  echo "no coretemp 'Package id 0' sensor found" >&2
  exit 1
fi

read_number() { tr -dc '0-9' <"$1" 2>/dev/null || true; }
pkg_c() { local v; v="$(read_number "$PKG_TEMP")"; printf '%s' "$((v / 1000))"; }
acpi_c() { local v; v="$(read_number /sys/class/thermal/thermal_zone0/temp)"; printf '%s' "$((v / 1000))"; }
cur_mhz() { local v; v="$(read_number /sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq)"; printf '%s' "$((v / 1000))"; }
throttle_count() {
  local c=0 f
  for f in /sys/devices/system/cpu/cpu*/thermal_throttle/core_throttle_count \
           /sys/devices/system/cpu/cpu*/thermal_throttle/package_throttle_count; do
    [[ -e "$f" ]] && c=$((c + $(read_number "$f")))
  done
  printf '%s' "$c"
}

ORIG_PCT="$(read_number "$PSTATE/max_perf_pct")"
ORIG_TURBO="$(read_number "$PSTATE/no_turbo")"
LOAD_PIDS=()

cleanup() {
  for p in "${LOAD_PIDS[@]:-}"; do kill "$p" 2>/dev/null || true; done
  wait 2>/dev/null || true
  echo "$ORIG_PCT" >"$PSTATE/max_perf_pct" 2>/dev/null || true
  echo "$ORIG_TURBO" >"$PSTATE/no_turbo" 2>/dev/null || true
  echo "restored max_perf_pct=$ORIG_PCT no_turbo=$ORIG_TURBO" | tee -a "$OUT"
}
trap cleanup EXIT INT TERM

start_load() {
  local i
  for ((i = 0; i < LOAD_PROCS; i++)); do
    yes >/dev/null &
    LOAD_PIDS+=("$!")
  done
}
stop_load() {
  local p
  for p in "${LOAD_PIDS[@]:-}"; do kill "$p" 2>/dev/null || true; done
  wait 2>/dev/null || true
  LOAD_PIDS=()
}

{
  echo "# thermal calibration $(date -Is)"
  echo "# host=$(hostname) kernel=$(uname -r) procs=$LOAD_PROCS step=${STEP_SEC}s sample=${SAMPLE_SEC}s"
  echo "# pkg_sensor=$PKG_TEMP governor=$(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor 2>/dev/null)"
  echo "# hw_max_khz=$(read_number /sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq) no_turbo=$ORIG_TURBO start_pct=$ORIG_PCT"
  echo "level,elapsed_s,pkg_c,acpi_c,cpu0_mhz,throttle_total"
} >>"$OUT"

echo "== idle baseline (30 s, no load) ==" | tee -a "$OUT"
t0=$SECONDS
while ((SECONDS - t0 < 30)); do
  printf 'idle,%s,%s,%s,%s,%s\n' "$((SECONDS - t0))" "$(pkg_c)" "$(acpi_c)" "$(cur_mhz)" "$(throttle_count)" >>"$OUT"
  sleep "$SAMPLE_SEC"
done

for level in $LEVELS; do
  echo "$level" >"$PSTATE/max_perf_pct" 2>/dev/null || { echo "cannot set max_perf_pct=$level" | tee -a "$OUT"; continue; }
  echo "== level ${level}% (${STEP_SEC}s, ${LOAD_PROCS} threads) ==" | tee -a "$OUT"
  start_load
  t0=$SECONDS
  while ((SECONDS - t0 < STEP_SEC)); do
    printf '%s,%s,%s,%s,%s,%s\n' "$level" "$((SECONDS - t0))" "$(pkg_c)" "$(acpi_c)" "$(cur_mhz)" "$(throttle_count)" >>"$OUT"
    sleep "$SAMPLE_SEC"
  done
  stop_load
  sleep 5
done

# Steady-state = last third of each level's samples.
awk -F, '
  NR == 1 { next }
  $1 == "idle" { n_idle++; s_idle += $3; if ($3 > m_idle) m_idle = $3; next }
  $1 ~ /^[0-9]+$/ {
    n[$1]++; t[$1] += $3; if ($3 > m[$1]) m[$1] = $3
    if (!($1 in last) || $2 > last[$1]) last[$1] = $2
    f[$1] = $5
    thr[$1] = $6
  }
  END {
    printf "\n# summary (whole-step average; fan is EC-controlled, no tach)\n"
    if (n_idle) printf "idle      avg=%.1fC max=%.0fC\n", s_idle / n_idle, m_idle
    for (l in n) printf "pct=%-4s avg=%.1fC max=%.0fC last_mhz=%s throttle_total=%s\n", l, t[l] / n[l], m[l], f[l], thr[l]
  }
' "$OUT" >>"$OUT"

echo "log: $OUT"
tail -20 "$OUT"
