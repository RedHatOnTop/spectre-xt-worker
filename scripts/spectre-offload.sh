#!/bin/bash
# Offload a heavy build/test command to a Lightning AI Studio (RUNBOOK 7.21).
#
# The Spectre is an agent runtime, not a compile farm: spectre-thermal-guard
# SIGTERMs build-class workloads on the box. This is the sanctioned alternative.
#
# Verified on the box 2026-09-26: dry-run -> rsync + remote exec ->
# `./gradlew --version` -> `./gradlew compileJava` BUILD SUCCESSFUL in the
# Studio -> stop. See RUNBOOK 7.21 for the ladder and the measured Studio.
#
# Storage is billed above the first 10 GB ($0.10/GB/month, billed daily) and a
# fat Studio also sleeps slower, so the uploaded tree is DELETED after the run
# unless --keep-remote says otherwise. Artifacts are pulled first.
#
# Usage:
#   spectre-offload [--repo DIR] [--dry-run] [--keep] [--keep-remote]
#                   [--setup CMD] [--artifact RELPATH]... -- COMMAND [ARGS...]
#
#   --keep         leave the Studio running (default: stop it afterwards)
#   --keep-remote  do not delete the uploaded tree in the Studio
#   --prune-caches also drop build caches in the Studio home (slower next build)
#   --studio-report print Studio status + storage usage and exit
set -euo pipefail

ENV_FILE="${LIGHTNING_ENV_FILE:-$HOME/.config/remote-agent/lightning.env}"
REPO="$(pwd)"
DRY_RUN=0
KEEP=0
KEEP_REMOTE=0
PRUNE=0
REPORT=0
SETUP_CMD=""
ARTIFACTS=()
UPLOADED=0
LEASED=0
# One Studio serves every session on this box; each run holds a lease named by its pid.
LEASE_DIR="${LIGHTNING_LEASE_DIR:-$HOME/.local/state/remote-agent/offload-leases}"
TIMEOUT="${LIGHTNING_SSH_TIMEOUT:-300}"
# Build caches that live in the persistent (billed) Studio home.
CACHE_DIRS='.gradle/caches .m2/repository .cache .npm .cargo/registry'

die() { printf '%s\n' "spectre-offload: $*" >&2; exit "${2:-1}"; }
note() { printf '%s\n' "spectre-offload: $*" >&2; }
# LogLevel=ERROR keeps stdout machine-readable (the Studio host key changes
# across boots, so ssh prints "Permanently added ..." on almost every call).
remote() { ssh -o BatchMode=yes -o LogLevel=ERROR "${STUDIO}" "$@"; }

while (($#)); do
  case "$1" in
    --repo) REPO="${2:?--repo needs a directory}"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --keep) KEEP=1; shift ;;
    --keep-remote) KEEP_REMOTE=1; shift ;;
    --prune-caches) PRUNE=1; shift ;;
    --studio-report) REPORT=1; shift ;;
    --setup) SETUP_CMD="${2:?--setup needs a command}"; shift 2 ;;
    --artifact) ARTIFACTS+=("${2:?--artifact needs a path}"); shift 2 ;;
    --timeout) TIMEOUT="${2:?--timeout needs seconds}"; shift 2 ;;
    --help|-h) sed -n '2,30p' "$0"; exit 0 ;;
    --) shift; break ;;
    *) die "unknown option: $1 (use -- before the command)" 2 ;;
  esac
done
if ((REPORT == 0)); then
  (($#)) || die "no command given; usage: spectre-offload -- ./gradlew test" 2
  [[ -d "${REPO}" ]] || die "repo directory not found: ${REPO}" 2
fi

# shellcheck disable=SC1090
[[ -f "${ENV_FILE}" ]] && source "${ENV_FILE}"
TEAMSPACE="${LIGHTNING_TEAMSPACE:-}"
STUDIO="${LIGHTNING_STUDIO:-}"
MACHINE="${LIGHTNING_MACHINE:-CPU}"
EXCLUDES="${LIGHTNING_EXCLUDES:-}"
ARTIFACT_DIR="${LIGHTNING_ARTIFACT_DIR:-$HOME/.local/state/remote-agent/offload-artifacts}"
# The SDK's own credential file is the preferred home for the secret; the env
# file only has to carry the non-secret target (teamspace/studio).
CRED_FILE="${LIGHTNING_CREDENTIAL_PATH:-$HOME/.lightning/credentials.json}"
if [[ -z "${LIGHTNING_USER_ID:-}" || -z "${LIGHTNING_API_KEY:-}" ]]; then
  [[ -f "${CRED_FILE}" ]] \
    || die "no Lightning credentials: set LIGHTNING_USER_ID/LIGHTNING_API_KEY in ${ENV_FILE} or run 'lightning login' (${CRED_FILE}) — RUNBOOK 7.21" 3
fi
[[ -n "${TEAMSPACE}" && -n "${STUDIO}" ]] \
  || die "LIGHTNING_TEAMSPACE / LIGHTNING_STUDIO missing in ${ENV_FILE} (RUNBOOK 7.21)" 3
LIGHTNING_BIN="${LIGHTNING_BIN:-$(command -v lightning || true)}"
[[ -n "${LIGHTNING_BIN}" ]] || LIGHTNING_BIN=/usr/local/lib/lightning-cli/venv/bin/lightning

WORKDIR="${LIGHTNING_WORKDIR:-offload/$(basename "${REPO}")}"
REMOTE_DIR="~/${WORKDIR#\~/}"
exclude_args=()
for pattern in ${EXCLUDES}; do exclude_args+=(--exclude "${pattern}"); done

if ((DRY_RUN)); then
  printf '{"ok":true,"dry_run":true,"studio":"%s","teamspace":"%s","machine":"%s","repo":"%s","remote":"%s","credentials":"%s","command":"%s"}\n' \
    "${STUDIO}" "${TEAMSPACE}" "${MACHINE}" "${REPO}" "${REMOTE_DIR}" \
    "$([[ -f "${CRED_FILE}" ]] && echo file || echo env)" "$*"
  exit 0
fi

[[ -x "${LIGHTNING_BIN}" ]] || die "lightning CLI not installed (RUNBOOK 7.21: scripts/install-lightning-offload.sh)" 4
command -v rsync >/dev/null 2>&1 || die "rsync not installed" 4

# Never leave uploaded files or a running Studio behind, even on SIGHUP/SIGTERM.
cleanup_remote() {
  ((UPLOADED == 1)) || return 0
  ((KEEP_REMOTE == 1)) && return 0
  remote "rm -rf ${REMOTE_DIR}" >/dev/null 2>&1 || true
  UPLOADED=0
}
# Lease changes and the last-lease check run under one flock, so two runs that end
# together cannot both leave the Studio to the other.
with_leases() {
  mkdir -p "${LEASE_DIR}"
  exec {lease_fd}>"${LEASE_DIR}/.lock"
  flock "${lease_fd}"
  "$@"
  local status=$?
  exec {lease_fd}>&-
  return "${status}"
}
studio_status() {
  timeout 60 "${LIGHTNING_BIN}" studio list --teamspace "${TEAMSPACE}" --json 2>/dev/null \
    | python3 -c 'import json,sys; name=sys.argv[1]; rows=json.load(sys.stdin); print(next((s.get("status","?") for s in rows if s.get("name")==name), "?"))' "${STUDIO}" 2>/dev/null || echo '?'
}
# `.started` marks a Studio that an offload lease started. One found Running or Pending
# was started by someone else, whose run a stop from here would cut off.
take_lease() {
  printf '%s\n' "$$" > "${LEASE_DIR}/$$"
  case "$(studio_status)" in
    Running|Pending) ;;
    *) : > "${LEASE_DIR}/.started" ;;
  esac
}
# Drops this run's lease and any lease whose process is gone. Prints `stop` when this was
# the last live lease on a Studio a lease started, else the number of live others.
drop_lease() {
  rm -f "${LEASE_DIR}/$$"
  local lease pid live=0
  for lease in "${LEASE_DIR}"/*; do
    [[ -e "${lease}" ]] || continue
    pid="${lease##*/}"
    if [[ "${pid}" =~ ^[0-9]+$ ]] && kill -0 "${pid}" 2>/dev/null; then
      live=$((live + 1))
    else
      rm -f "${lease}"
    fi
  done
  if ((live == 0)) && [[ -e "${LEASE_DIR}/.started" ]]; then
    rm -f "${LEASE_DIR}/.started"
    echo stop
  else
    echo "${live}"
  fi
}
# Runs from the trap too, so a `die` after the start (SSH wait, rsync install or push)
# releases the lease; the Studio stops only with the last lease on one a lease started.
stop_studio() {
  ((LEASED == 1)) || return 0
  LEASED=0
  local verdict
  verdict="$(with_leases drop_lease)"
  ((KEEP == 0)) || return 0
  if [[ "${verdict}" != stop ]]; then
    if ((verdict > 0)); then
      note "leaving Studio ${STUDIO} running for ${verdict} other offload(s)"
    else
      note "leaving Studio ${STUDIO} running: it was already up when this offload began"
    fi
    return 0
  fi
  note "stopping Studio ${STUDIO}"
  "${LIGHTNING_BIN}" studio stop --name "${STUDIO}" --teamspace "${TEAMSPACE}" >&2 || true
}
trap 'cleanup_remote; stop_studio' EXIT HUP INT TERM

if ((REPORT == 1)); then
  status="$(studio_status)"
  printf '{"studio":"%s","status":"%s"' "${STUDIO}" "${status}"
  if [[ "${status}" == "Running" ]]; then
    printf ',"home_used":"%s"' "$(remote 'du -sh "$HOME" 2>/dev/null | cut -f1' | tr -d '[:space:]')"
    printf ',"free":"%s"' "$(remote 'df -h "$HOME" | awk "NR==2{print \$4}"' | tr -d '[:space:]')"
    trees="$(remote 'du -sh ~/offload 2>/dev/null | cut -f1' | tr -d '[:space:]')"
    printf ',"offload_trees":"%s"' "${trees:-none}"
    printf ',"cache_mb":%s' "$(remote "for d in ${CACHE_DIRS}; do [ -e \"\$HOME/\$d\" ] && du -sm \"\$HOME/\$d\" 2>/dev/null; done | awk '{s+=\$1} END{print s+0}'" | tr -d '[:space:]')"
    # Session/boot evidence for the free tier's 4-hour cap: uptime and the
    # lifecycle log written by ~/.lightning_studio/on_start.sh plus a heartbeat.
    printf ',"uptime_s":%s' "$(remote 'cut -d" " -f1 /proc/uptime' | tr -d '[:space:]')"
    printf ',"boot":"%s"' "$(remote 'uptime -s' | tr -d '[:space:]')"
    printf ',"lifecycle":"%s"' "$(remote 'tail -1 "$HOME/studio-lifecycle.log" 2>/dev/null' | tr -d '[:space:]')"
  fi
  printf '}\n'
  exit 0
fi

note "starting Studio ${STUDIO} (${MACHINE})"
with_leases take_lease
LEASED=1
"${LIGHTNING_BIN}" studio start --name "${STUDIO}" --teamspace "${TEAMSPACE}" --machine "${MACHINE}" >&2 || true

# A freshly started Studio accepts SSH before it is set up and then answers commands with
# "We are still setting things up for you"; only an exact echo means it is ready.
deadline=$((SECONDS + TIMEOUT))
until [[ "$(ssh -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=accept-new \
      -o LogLevel=ERROR "${STUDIO}" 'echo spectre-ready' 2>/dev/null)" == spectre-ready ]]; do
  ((SECONDS < deadline)) || die "Studio ${STUDIO} did not accept SSH within ${TIMEOUT}s" 5
  sleep 5
done

# The Studio image ships without rsync; install it once (the Studio disk is
# persistent, so this is a no-op on later runs).
if ! ssh -o BatchMode=yes -o LogLevel=ERROR "${STUDIO}" 'command -v rsync >/dev/null 2>&1'; then
  note "installing rsync in the Studio (one-time)"
  ssh -o BatchMode=yes -o LogLevel=ERROR "${STUDIO}" \
    'sudo apt-get update -qq && sudo apt-get install -y -qq rsync' >&2 \
    || die "could not install rsync in ${STUDIO}" 6
fi

note "syncing ${REPO} -> ${STUDIO}:${REMOTE_DIR}"
ssh -o LogLevel=ERROR "${STUDIO}" "mkdir -p ${REMOTE_DIR}"
rsync -az --delete "${exclude_args[@]}" "${REPO}/" "${STUDIO}:${REMOTE_DIR}/" \
  || die "rsync push to ${STUDIO} failed" 6
UPLOADED=1

if [[ -n "${SETUP_CMD}" ]]; then
  note "studio setup: ${SETUP_CMD}"
  ssh -o LogLevel=ERROR "${STUDIO}" "cd ${REMOTE_DIR} && ${SETUP_CMD}" >&2
  # apt leaves a cache behind and the Studio disk is billed storage.
  remote 'sudo apt-get clean' >/dev/null 2>&1 || true
fi

# Quote each argv word so `-- sh -lc 'a; b'` survives the remote shell.
remote_cmd=""
for arg in "$@"; do remote_cmd+="$(printf '%q' "$arg") "; done

set +e
note "running: $*"
ssh -o BatchMode=yes -o LogLevel=ERROR "${STUDIO}" "cd ${REMOTE_DIR} && ${remote_cmd}"
rc=$?
set -e

for rel in "${ARTIFACTS[@]}"; do
  mkdir -p "${ARTIFACT_DIR}/$(dirname "${rel}")"
  rsync -az "${STUDIO}:${REMOTE_DIR}/${rel}" "${ARTIFACT_DIR}/${rel}" 2>/dev/null \
    || note "artifact not returned: ${rel}"
done

# Storage above the first 10 GB is billed ($0.10/GB/month, billed daily) and a
# fat Studio also sleeps slower, so the uploaded tree never stays behind.
uploaded_bytes="$(remote "du -sb ${REMOTE_DIR} 2>/dev/null | cut -f1" || true)"
cleaned="no"
if ((KEEP_REMOTE == 0)); then
  remote "rm -rf ${REMOTE_DIR}" || note "could not remove ${REMOTE_DIR} in ${STUDIO}"
  still="$(remote "test -e ${REMOTE_DIR} && echo present || echo gone" || echo unknown)"
  cleaned="$([[ "${still}" == "gone" ]] && echo yes || echo "${still}")"
  note "removed ${REMOTE_DIR} (${uploaded_bytes:-?} bytes) from the Studio: ${cleaned}"
  UPLOADED=0
else
  note "--keep-remote: left ${REMOTE_DIR} (${uploaded_bytes:-?} bytes) in the Studio"
fi

pruned_bytes=0
if ((PRUNE == 1)); then
  before_mb="$(remote 'du -sm "$HOME" 2>/dev/null | cut -f1' | tr -d '[:space:]')"
  remote "for d in ${CACHE_DIRS}; do rm -rf \"\$HOME/\$d\"; done" || note "cache prune had errors"
  remote 'sudo apt-get clean' >/dev/null 2>&1 || true
  after_mb="$(remote 'du -sm "$HOME" 2>/dev/null | cut -f1' | tr -d '[:space:]')"
  pruned_bytes=$(( (${before_mb:-0} - ${after_mb:-0}) * 1024 * 1024 ))
  note "--prune-caches: home ${before_mb:-?}M -> ${after_mb:-?}M"
fi

home_used="$(remote 'du -sh "$HOME" 2>/dev/null | cut -f1' | tr -d '[:space:]' || true)"
studio_used="$(remote 'df -h "$HOME" | awk "NR==2{print \$3}"' | tr -d '[:space:]' || true)"

stop_studio

printf '{"ok":%s,"exit":%s,"studio":"%s","remote":"%s","remote_cleaned":"%s","uploaded_bytes":"%s","home_used":"%s","studio_used":"%s","pruned_bytes":%s,"artifacts":"%s"}\n' \
  "$([[ ${rc} -eq 0 ]] && echo true || echo false)" "${rc}" "${STUDIO}" "${REMOTE_DIR}" "${cleaned}" \
  "${uploaded_bytes:-0}" "${home_used:-?}" "${studio_used:-?}" "${pruned_bytes}" "${ARTIFACT_DIR}"
exit "${rc}"
