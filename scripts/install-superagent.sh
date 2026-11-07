#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEST="${DESTDIR:-}"
PERSON_USER="${PERSON_USER:-${SUDO_USER:-$(id -un)}}"
PERSON_HOME="${PERSON_HOME:-$(getent passwd "${PERSON_USER}" | cut -d: -f6)}"
if [[ -z "${DEST}" && ${EUID} -ne 0 ]]; then
  printf '%s\n' 'Run as root or set DESTDIR for a staged install.' >&2
  exit 1
fi
if [[ "${PERSON_HOME}" != /* ]]; then
  printf '%s\n' 'PERSON_HOME must be an absolute path.' >&2
  exit 1
fi
python3 -I -B - "${ROOT}/scripts" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
from superagent_queue import driver, execution, mail_obligation, native, offload_artifact, offload_cleanup, selection
from superagent_queue.store import Journal
PY
install -d -m 0755 "${DEST}/usr/local/bin" \
  "${DEST}/usr/local/lib/spectre-superagent/"{superagent_queue,orca_runtime}
install -m 0644 "${ROOT}/scripts/superagent_queue/"*.py "${DEST}/usr/local/lib/spectre-superagent/superagent_queue/"
install -m 0644 "${ROOT}/scripts/orca_runtime/"*.py "${DEST}/usr/local/lib/spectre-superagent/orca_runtime/"
install -m 0644 "${ROOT}/scripts/dsh-go-session.mjs" "${DEST}/usr/local/lib/spectre-superagent/dsh-go-session.mjs"
install -m 0644 "${ROOT}/scripts/dsh-worker-session.mjs" "${DEST}/usr/local/lib/spectre-superagent/dsh-worker-session.mjs"
install -m 0644 "${ROOT}/scripts/offload-command.py" "${DEST}/usr/local/lib/spectre-superagent/offload-command.py"
install -m 0644 "${ROOT}/scripts/offload-artifacts.py" "${DEST}/usr/local/lib/spectre-superagent/offload-artifacts.py"
install -m 0644 "${ROOT}/scripts/offload-cleanup.py" "${DEST}/usr/local/lib/spectre-superagent/offload-cleanup.py"
install -m 0644 "${ROOT}/scripts/offload-capacity.py" "${DEST}/usr/local/lib/spectre-superagent/offload-capacity.py"
install -d -m 0755 "${DEST}/usr/local/lib/spectre-superagent/dsh-worker-session"
install -m 0644 "${ROOT}/patches/dsh-worker-session/"{headless-0.1.5-rc.1.js,LICENSE} \
  "${DEST}/usr/local/lib/spectre-superagent/dsh-worker-session/"
install -m 0755 "${ROOT}/scripts/spectre-superagent.py" "${DEST}/usr/local/bin/spectre-superagent"
install -m 0755 "${ROOT}/scripts/harness-workers.py" "${DEST}/usr/local/bin/harness-workers"
install -m 0755 "${ROOT}/scripts/spectre-dsh-execution.py" "${DEST}/usr/local/bin/spectre-dsh-execution"
install -m 0755 "${ROOT}/scripts/spectre-offload-execution.py" "${DEST}/usr/local/bin/spectre-offload-execution"
install -m 0755 "${ROOT}/scripts/spectre-dsh-worker.py" "${DEST}/usr/local/bin/spectre-dsh-worker"
install -m 0644 "${ROOT}/scripts/spectre-dsh-worker.py" "${DEST}/usr/local/lib/spectre-superagent/spectre-dsh-worker.py"
if [[ -n "${DSH_WORKER_BINARY:-}" ]]; then
  install -m 0755 "${DSH_WORKER_BINARY}" "${DEST}/usr/local/bin/dsh-worker"
  install -m 0755 "${DSH_WORKER_BINARY}" "${DEST}/usr/local/bin/dsh-worker-go"
fi
install -m 0755 "${ROOT}/scripts/dsh-clinepass" "${DEST}/usr/local/bin/dsh-superagent-worker"
install -m 0755 "${ROOT}/scripts/stage-dsh-go.py" "${DEST}/usr/local/bin/dsh-superagent-stage-go"
install -m 0755 "${ROOT}/scripts/stage-dsh-session.py" "${DEST}/usr/local/bin/dsh-superagent-stage-session"
install -d -m 0755 "${DEST}${PERSON_HOME}/.config/systemd/user"
install -m 0644 "${ROOT}/systemd/spectre-superagent."{service,timer} "${DEST}${PERSON_HOME}/.config/systemd/user/"
if [[ -z "${DEST}" ]]; then
  chown "${PERSON_USER}:$(id -gn "${PERSON_USER}")" "${PERSON_HOME}/.config/systemd/user/spectre-superagent."{service,timer}
fi
printf '%s\n' 'Continuity driver installed. No service, profile, credential, or native ownership was changed.'
