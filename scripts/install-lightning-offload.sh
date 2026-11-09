#!/bin/bash
# Install the Lightning AI CLI for the Spectre offload path (RUNBOOK 7.21).
#
# Creates a self-contained venv at /usr/local/lib/lightning-cli/venv, exposes
# `lightning` on PATH, and lays down ~/.config/remote-agent/lightning.env from
# the repo template when absent. It never writes credentials: authenticate with
# `lightning login` (browser) or by copying ~/.lightning/credentials.json.
#
#   sudo bash scripts/install-lightning-offload.sh
#   sudo SKIP_PIP=1 bash scripts/install-lightning-offload.sh   # layout only
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEST="${DESTDIR:-}"
PERSON_USER="${PERSON_USER:-${SUDO_USER:-$(id -un)}}"
PERSON_HOME="${PERSON_HOME:-$(getent passwd "${PERSON_USER}" | cut -d: -f6)}"
SKIP_PIP="${SKIP_PIP:-0}"
VENV=/usr/local/lib/lightning-cli/venv

if [[ -z "${DEST}" && ${EUID} -ne 0 ]]; then
  echo "run as root or set DESTDIR for a staged installation" >&2
  exit 1
fi
if [[ -z "${PERSON_HOME}" || "${PERSON_HOME}" != /* ]]; then
  echo "PERSON_HOME must be an absolute path" >&2
  exit 1
fi

install -d -m 0755 "${DEST}/usr/local/bin" "${DEST}/usr/local/lib/lightning-cli" \
  "${DEST}${PERSON_HOME}/.config/remote-agent" "${DEST}${PERSON_HOME}/.lightning"

if [[ "${SKIP_PIP}" != "1" ]]; then
  if [[ ! -x "${DEST}${VENV}/bin/lightning" ]]; then
    python3 -m venv "${DEST}${VENV}"
    "${DEST}${VENV}/bin/pip" install -q -U lightning-sdk
  else
    "${DEST}${VENV}/bin/pip" install -q -U lightning-sdk
  fi
fi
if [[ -x "${DEST}${VENV}/bin/lightning" ]]; then
  ln -sfn "${VENV}/bin/lightning" "${DEST}/usr/local/bin/lightning"
fi

if [[ ! -f "${DEST}${PERSON_HOME}/.config/remote-agent/lightning.env" ]]; then
  install -m 0600 "${ROOT}/config/lightning.env.example" \
    "${DEST}${PERSON_HOME}/.config/remote-agent/lightning.env"
  echo "wrote ${PERSON_HOME}/.config/remote-agent/lightning.env (fill in teamspace/studio)"
fi

if [[ -z "${DEST}" ]]; then
  chown "${PERSON_USER}:$(id -gn "${PERSON_USER}")" \
    "${PERSON_HOME}/.config/remote-agent/lightning.env" "${PERSON_HOME}/.lightning"
  echo "next (as ${PERSON_USER}):"
  echo "  1. lightning login                     # browser, once (writes ~/.lightning/credentials.json)"
  echo "  2. lightning studio create --name buildbox --teamspace <owner>/<teamspace>"
  echo "  3. lightning ssh configure --name buildbox   # plain ssh/rsync host entry"
  echo "  4. spectre-offload --dry-run -- ./gradlew test"
fi

printf 'Lightning offload installed (SKIP_PIP=%s).\n' "${SKIP_PIP}"
