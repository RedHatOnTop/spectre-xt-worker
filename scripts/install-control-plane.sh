#!/bin/bash
# Targeted install: no service restart, timer enablement, registry overwrite, or model call.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEST="${DESTDIR:-}"
PERSON_USER="${PERSON_USER:-${SUDO_USER:-$(id -un)}}"
PERSON_HOME="${PERSON_HOME:-$(getent passwd "${PERSON_USER}" | cut -d: -f6)}"
if [[ -z "${DEST}" && ${EUID} -ne 0 ]]; then
  echo "run as root or set DESTDIR for a staged installation" >&2
  exit 1
fi
if [[ -z "${PERSON_HOME}" || "${PERSON_HOME}" != /* ]]; then
  echo "PERSON_HOME must be an absolute path" >&2
  exit 1
fi
install -d -m 0755 "${DEST}/usr/local/bin" "${DEST}/usr/local/share/remote-agent" \
  "${DEST}/usr/local/lib/spectre-worker-state/worker_state" \
  "${DEST}/usr/local/lib/spectre-worker-state/control_plane" \
  "${DEST}${PERSON_HOME}/.config/systemd/user"
install -m 0644 "${ROOT}/scripts/worker_state/"*.py "${ROOT}/scripts/worker_state/schema.sql" \
  "${DEST}/usr/local/lib/spectre-worker-state/worker_state/"
install -m 0644 "${ROOT}/scripts/control_plane/"*.py "${DEST}/usr/local/lib/spectre-worker-state/control_plane/"
for name in spectre-state spectre-continuity spectre-loop spectre-reaper; do
  install -m 0755 "${ROOT}/scripts/${name}.py" "${DEST}/usr/local/bin/${name}"
done
install -m 0755 "${ROOT}/scripts/native-worker-pin-sync.py" "${DEST}/usr/local/bin/spectre-pin-sync"
install -m 0755 "${ROOT}/scripts/codex-provider-health.py" "${DEST}/usr/local/bin/spectre-codex-provider-health"
install -m 0755 "${ROOT}/scripts/slack-bridge.mjs" "${DEST}/usr/local/bin/spectre-slack-bridge"
install -m 0755 "${ROOT}/scripts/dsh-clinepass" "${ROOT}/scripts/spectre-astra" "${DEST}/usr/local/bin/"
install -m 0644 "${ROOT}/scripts/worker-state-client.mjs" "${ROOT}/scripts/planner-dispatch.mjs" \
  "${ROOT}/scripts/dispatch-process.mjs" "${DEST}/usr/local/bin/"
install -m 0644 "${ROOT}/config/astra-plan-prompt.md" "${ROOT}/config/astra-packet.schema.json" \
  "${ROOT}/config/qoder-goal-clause.md" "${DEST}/usr/local/share/remote-agent/"
for name in spectre-loop spectre-reaper spectre-pin-sync codex-provider-health; do
  install -m 0644 "${ROOT}/systemd/${name}.service" "${ROOT}/systemd/${name}.timer" \
    "${DEST}${PERSON_HOME}/.config/systemd/user/"
done
if [[ -z "${DEST}" ]]; then
  chown "${PERSON_USER}:$(id -gn "${PERSON_USER}")" "${PERSON_HOME}/.config/systemd/user/"{spectre-loop,spectre-reaper,spectre-pin-sync,codex-provider-health}.{service,timer}
fi
printf 'Control plane installed; services and feature flags were not changed.\n'
