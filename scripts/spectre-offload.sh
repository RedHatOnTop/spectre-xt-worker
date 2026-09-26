#!/bin/bash
# Offload a heavy build/test command to a Lightning AI Studio (RUNBOOK 7.21).
#
# The Spectre is an agent runtime, not a compile farm: spectre-thermal-guard
# SIGTERMs build-class workloads on the box. This is the sanctioned alternative.
#
# UNVERIFIED as of 2026-09-26: no Lightning account existed when it was written.
# Only the refusal path and --dry-run were exercised on the box. Verify with
#   spectre-offload --dry-run -- ./gradlew test
# then a trivial command before trusting a real build.
#
# Usage:
#   spectre-offload [--repo DIR] [--dry-run] [--keep] [--setup CMD]
#                   [--artifact RELPATH]... -- COMMAND [ARGS...]
#
# Requires (one-time, interactive): `pip install lightning-sdk`, `lightning login`,
# `lightning studio create --name <LIGHTNING_STUDIO> --teamspace <O>/<T>`, and
# `lightning ssh configure --name <LIGHTNING_STUDIO>` so plain ssh/rsync work.
set -euo pipefail

ENV_FILE="${LIGHTNING_ENV_FILE:-$HOME/.config/remote-agent/lightning.env}"
REPO="$(pwd)"
DRY_RUN=0
KEEP=0
SETUP_CMD=""
ARTIFACTS=()
TIMEOUT="${LIGHTNING_SSH_TIMEOUT:-300}"

die() { printf '%s\n' "spectre-offload: $*" >&2; exit "${2:-1}"; }
note() { printf '%s\n' "spectre-offload: $*" >&2; }

while (($#)); do
  case "$1" in
    --repo) REPO="${2:?--repo needs a directory}"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --keep) KEEP=1; shift ;;
    --setup) SETUP_CMD="${2:?--setup needs a command}"; shift 2 ;;
    --artifact) ARTIFACTS+=("${2:?--artifact needs a path}"); shift 2 ;;
    --timeout) TIMEOUT="${2:?--timeout needs seconds}"; shift 2 ;;
    --help|-h) sed -n '2,20p' "$0"; exit 0 ;;
    --) shift; break ;;
    *) die "unknown option: $1 (use -- before the command)" 2 ;;
  esac
done
(($#)) || die "no command given; usage: spectre-offload -- ./gradlew test" 2
[[ -d "${REPO}" ]] || die "repo directory not found: ${REPO}" 2

# shellcheck disable=SC1090
[[ -f "${ENV_FILE}" ]] && source "${ENV_FILE}"
TEAMSPACE="${LIGHTNING_TEAMSPACE:-}"
STUDIO="${LIGHTNING_STUDIO:-}"
MACHINE="${LIGHTNING_MACHINE:-CPU}"
EXCLUDES="${LIGHTNING_EXCLUDES:-}"
ARTIFACT_DIR="${LIGHTNING_ARTIFACT_DIR:-$HOME/.local/state/remote-agent/offload-artifacts}"
[[ -n "${LIGHTNING_USER_ID:-}" && -n "${LIGHTNING_API_KEY:-}" ]] \
  || die "Lightning credentials missing in ${ENV_FILE} (RUNBOOK 7.21)" 3
[[ -n "${TEAMSPACE}" && -n "${STUDIO}" ]] \
  || die "LIGHTNING_TEAMSPACE / LIGHTNING_STUDIO missing in ${ENV_FILE} (RUNBOOK 7.21)" 3

WORKDIR="${LIGHTNING_WORKDIR:-offload/$(basename "${REPO}")}"
REMOTE_DIR="~/${WORKDIR#\~/}"
exclude_args=()
for pattern in ${EXCLUDES}; do exclude_args+=(--exclude "${pattern}"); done

if ((DRY_RUN)); then
  printf '{"ok":true,"dry_run":true,"studio":"%s","teamspace":"%s","machine":"%s","repo":"%s","remote":"%s","command":"%s"}\n' \
    "${STUDIO}" "${TEAMSPACE}" "${MACHINE}" "${REPO}" "${REMOTE_DIR}" "$*"
  exit 0
fi

command -v lightning >/dev/null 2>&1 || die "lightning CLI not installed (pip install lightning-sdk)" 4
command -v rsync >/dev/null 2>&1 || die "rsync not installed" 4

note "starting Studio ${STUDIO} (${MACHINE})"
lightning studio start --name "${STUDIO}" --teamspace "${TEAMSPACE}" --machine "${MACHINE}" >&2 || true

deadline=$((SECONDS + TIMEOUT))
until ssh -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=accept-new \
      "${STUDIO}" true 2>/dev/null; do
  ((SECONDS < deadline)) || die "Studio ${STUDIO} did not accept SSH within ${TIMEOUT}s" 5
  sleep 5
done

note "syncing ${REPO} -> ${STUDIO}:${REMOTE_DIR}"
ssh "${STUDIO}" "mkdir -p ${REMOTE_DIR}"
rsync -az --delete "${exclude_args[@]}" "${REPO}/" "${STUDIO}:${REMOTE_DIR}/"

if [[ -n "${SETUP_CMD}" ]]; then
  note "studio setup: ${SETUP_CMD}"
  ssh "${STUDIO}" "cd ${REMOTE_DIR} && ${SETUP_CMD}" >&2
fi

set +e
note "running: $*"
ssh -o BatchMode=yes "${STUDIO}" "cd ${REMOTE_DIR} && $*"
rc=$?
set -e

for rel in "${ARTIFACTS[@]}"; do
  mkdir -p "${ARTIFACT_DIR}/$(dirname "${rel}")"
  rsync -az "${STUDIO}:${REMOTE_DIR}/${rel}" "${ARTIFACT_DIR}/${rel}" 2>/dev/null \
    || note "artifact not returned: ${rel}"
done

if ((KEEP == 0)); then
  note "stopping Studio ${STUDIO}"
  lightning studio stop --name "${STUDIO}" --teamspace "${TEAMSPACE}" >&2 || true
fi

printf '{"ok":%s,"exit":%s,"studio":"%s","remote":"%s","artifacts":"%s"}\n' \
  "$([[ ${rc} -eq 0 ]] && echo true || echo false)" "${rc}" "${STUDIO}" "${REMOTE_DIR}" "${ARTIFACT_DIR}"
exit "${rc}"
