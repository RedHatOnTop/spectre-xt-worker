#!/bin/bash
# Make the Spectre look powered off while the agent keeps running.
# closed — internal panel off, backlights 0, LEDs 0, mute
# open   — internal panel back on (HDMI dummy is never touched)
set -u

MODE="${1:-closed}"
PERSON_USER="${STEALTH_USER:-person}"
LOG="${STEALTH_LOG:-/work/logs/stealth.log}"
CONF="${STEALTH_CONF:-/etc/default/spectre-stealth}"
install -d -m 0755 "$(dirname "${LOG}")" 2>/dev/null || true

# External-display policy when a real monitor is connected:
#   keep (default) — leave it on; the box is only guaranteed dark with
#                    an HDMI dummy instead of a monitor.
#   dpms           — blank it with DPMS (output stays alive, Electron safe)
external_mode() {
  if [[ -r "${CONF}" ]]; then
    # shellcheck disable=SC1090
    source "${CONF}" 2>/dev/null || true
  fi
  printf '%s\n' "${STEALTH_EXTERNAL:-keep}"
}

ts() { date -u +%Y-%m-%dT%H:%M:%SZ; }
note() { printf '%s %s\n' "$(ts)" "$*" >>"${LOG}" 2>/dev/null || true; }

as_graphical() {
  local uid display xauth
  uid="$(id -u "${PERSON_USER}" 2>/dev/null || echo 1000)"
  display=":0"
  xauth="/home/${PERSON_USER}/.Xauthority"
  if [[ ! -r "${xauth}" ]]; then
    xauth="/run/user/${uid}/gdm/Xauthority"
  fi
  if [[ ! -r "${xauth}" ]]; then
    xauth="/home/${PERSON_USER}/.Xauthority"
  fi
  runuser -u "${PERSON_USER}" -- env DISPLAY="${display}" XAUTHORITY="${xauth}" "$@"
}

internal_outputs() {
  as_graphical xrandr --query 2>/dev/null | awk '
    $2 == "connected" && $1 ~ /^(eDP|LVDS|DSI)/ { print $1 }
  '
}

external_connected() {
  as_graphical xrandr --query 2>/dev/null | awk '
    $2 == "connected" && $1 ~ /^(HDMI|DP|DisplayPort|VGA)/ { n++ }
    END { exit (n > 0) ? 0 : 1 }
  '
}

zero_backlights() {
  local b
  for b in /sys/class/backlight/*/brightness; do
    [[ -e "${b}" ]] || continue
    echo 0 >"${b}" 2>/dev/null || true
  done
}

zero_leds() {
  local led maxf
  for led in /sys/class/leds/*/brightness; do
    [[ -e "${led}" ]] || continue
    echo 0 >"${led}" 2>/dev/null || true
  done
  for maxf in /sys/class/leds/*/max_brightness; do
    [[ -e "${maxf}" ]] || continue
    echo 0 >"$(dirname "${maxf}")/brightness" 2>/dev/null || true
  done
}

mute_audio() {
  amixer -q sset Master mute 2>/dev/null || true
  as_graphical pactl set-sink-mute @DEFAULT_SINK@ 1 2>/dev/null || true
  as_graphical wpctl set-mute @DEFAULT_AUDIO_SINK@ 1 2>/dev/null || true
}

blank_vt() {
  # Only the Linux console. Do not blank /sys/class/graphics/fb* — Intel
  # HD 4000 may share a framebuffer with the HDMI dummy Electron is on.
  setterm --blank force --term linux </dev/tty1 >/dev/null 2>&1 || true
}

wait_for_x() {
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    as_graphical xrandr --query >/dev/null 2>&1 && return 0
    sleep 1
  done
  return 1
}

case "${MODE}" in
  closed)
    zero_backlights
    zero_leds
    mute_audio
    blank_vt
    if wait_for_x; then
      as_graphical xset -display :0 s off 2>/dev/null || true
      as_graphical xset -display :0 s noblank 2>/dev/null || true
      if external_connected; then
        while read -r out; do
          [[ -z "${out}" ]] && continue
          as_graphical xrandr --output "${out}" --off 2>/dev/null || true
          note "internal ${out} off (dummy HDMI stays)"
        done < <(internal_outputs)
        # A real monitor (not a dummy) stays lit by design — killing the
        # last output can wedge Electron. STEALTH_EXTERNAL=dpms in
        # /etc/default/spectre-stealth blanks it via DPMS instead, which
        # keeps the output alive and the window mapped.
        if [[ "$(external_mode)" == "dpms" ]]; then
          as_graphical xset -display :0 dpms force off 2>/dev/null || true
          note "external display dpms off (STEALTH_EXTERNAL=dpms)"
        fi
      else
        as_graphical xset -display :0 dpms force off 2>/dev/null || true
        note "no HDMI dummy — panel connected, dpms off, backlight 0"
      fi
    else
      note "X not ready; backlights and LEDs still zeroed"
    fi
    zero_backlights
    zero_leds
    note "stealth closed"
    ;;
  open)
    if wait_for_x; then
      as_graphical xset -display :0 dpms force on 2>/dev/null || true
      while read -r out; do
        [[ -z "${out}" ]] && continue
        as_graphical xrandr --output "${out}" --auto 2>/dev/null || true
        note "internal ${out} on"
      done < <(internal_outputs)
    fi
    note "stealth open"
    ;;
  *)
    echo "usage: $0 closed|open" >&2
    exit 2
    ;;
esac
