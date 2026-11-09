#!/bin/bash
# Install the thermal / compile-farm guard and its timer on the Spectre.
#
# Idempotent. Installs the control_plane package (thermal.py included), the
# guard binary, the user units, then enables the timer unless ENABLE=0. With
# DESTDIR set it is a staged install and nothing is enabled.
#
#   sudo bash scripts/install-thermal-guard.sh
#   sudo ENABLE=0 bash scripts/install-thermal-guard.sh     # install only
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEST="${DESTDIR:-}"
PERSON_USER="${PERSON_USER:-${SUDO_USER:-$(id -un)}}"
PERSON_HOME="${PERSON_HOME:-$(getent passwd "${PERSON_USER}" | cut -d: -f6)}"
ENABLE="${ENABLE:-1}"

if [[ -z "${DEST}" && ${EUID} -ne 0 ]]; then
  echo "run as root or set DESTDIR for a staged installation" >&2
  exit 1
fi
if [[ -z "${PERSON_HOME}" || "${PERSON_HOME}" != /* ]]; then
  echo "PERSON_HOME must be an absolute path" >&2
  exit 1
fi

install -d -m 0755 "${DEST}/usr/local/bin" \
  "${DEST}/usr/local/lib/spectre-worker-state/control_plane" \
  "${DEST}${PERSON_HOME}/.config/systemd/user" \
  "${DEST}${PERSON_HOME}/.config/remote-agent" \
  "${DEST}${PERSON_HOME}/.local/state/remote-agent"
install -m 0644 "${ROOT}/scripts/control_plane/"*.py \
  "${DEST}/usr/local/lib/spectre-worker-state/control_plane/"
install -m 0755 "${ROOT}/scripts/spectre-thermal-guard.py" "${DEST}/usr/local/bin/spectre-thermal-guard"
install -m 0644 "${ROOT}/systemd/spectre-thermal-guard.service" \
  "${ROOT}/systemd/spectre-thermal-guard.timer" "${DEST}${PERSON_HOME}/.config/systemd/user/"

if [[ -z "${DEST}" ]]; then
  chown "${PERSON_USER}:$(id -gn "${PERSON_USER}")" \
    "${PERSON_HOME}/.config/systemd/user/spectre-thermal-guard."{service,timer} \
    "${PERSON_HOME}/.config/remote-agent" "${PERSON_HOME}/.local/state/remote-agent"
  if [[ "${ENABLE}" == "1" ]]; then
    systemctl --user daemon-reload 2>/dev/null || true
    sudo -u "${PERSON_USER}" XDG_RUNTIME_DIR="/run/user/$(id -u "${PERSON_USER}")" \
      systemctl --user enable --now spectre-thermal-guard.timer
    echo "timer enabled: spectre-thermal-guard.timer"
  fi
  echo "status: $(sudo -u "${PERSON_USER}" XDG_RUNTIME_DIR="/run/user/$(id -u "${PERSON_USER}")" \
    /usr/local/bin/spectre-thermal-guard --check || true)"
fi

printf 'Thermal guard installed (ENABLE=%s).\n' "${ENABLE}"
