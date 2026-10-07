#!/bin/bash
# Run a heavy build/test command on a throwaway AWS spot instance (aws-burst), not on this box.
#
# The Spectre is an agent runtime, not a compile farm: spectre-thermal-guard SIGTERMs
# build-class work here. This is the burst-capacity route next to spectre-offload (Lightning):
# aws-burst starts an Amazon Linux 2023 spot instance that powers itself off after --ttl
# minutes or 10 idle minutes; this wrapper prepares it, pushes the repo, runs the command,
# pulls artifacts back and terminates the instance (unless --keep).
#
# Usage:
#   burst-offload [--repo DIR] [--dry-run] [--keep] [--type TYPE] [--ttl MIN] [--rust TOOLCHAIN]
#                 [--setup CMD] [--artifact RELPATH]... -- COMMAND [ARGS...]
#
#   --repo DIR        tree to push (default: the current directory). In a git checkout only
#                     tracked and untracked-but-not-ignored files go, so target/ and
#                     node_modules/ stay home; uncommitted edits are included.
#   --type TYPE       instance type (default: aws-burst's c7i-flex.large, 2 vCPU / 4 GiB)
#   --ttl MIN         hard power-off after MIN minutes (default 90)
#   --rust TOOLCHAIN  install rustup with TOOLCHAIN plus clippy and rustfmt first
#   --setup CMD       shell command run in the tree before COMMAND (a failing setup fails the run)
#   --artifact PATH   file or directory (relative to the tree) to copy back afterwards
#   --keep            leave the instance running; `aws-burst down <id>` ends it
#   --dry-run         print the plan as JSON and exit
#
# COMMAND's output streams to stderr and to the run log; stdout carries only the final JSON
# line. The exit status is COMMAND's, or one of: 2 usage, 3 no instance, 4 missing local
# tool, 5 instance preparation failed, 6 sync failed.
#
#   burst-offload --repo ~/Projects/modeldesk --rust 1.97.1 \
#     -- bash -c 'cd crates && cargo test -p orca-wire -j 2'

set -uo pipefail

export PATH="$HOME/.local/bin:$PATH"
REPO="$PWD"
TYPE=""
TTL=90
RUST=""
SETUP_CMD=""
KEEP=0
DRY_RUN=0
ARTIFACTS=()
EXCLUDES="target node_modules .gradle build output .venv __pycache__"
RUN_ROOT="${BURST_OFFLOAD_HOME:-$HOME/.cache/burst-offload}"

die() { printf '%s\n' "burst-offload: $*" >&2; exit "${2:-1}"; }
note() { printf '%s\n' "burst-offload: $*" >&2; }

while (($#)); do
  case "$1" in
    --repo) REPO="${2:?--repo needs a directory}"; shift 2 ;;
    --type) TYPE="${2:?--type needs an instance type}"; shift 2 ;;
    --ttl) TTL="${2:?--ttl needs minutes}"; shift 2 ;;
    --rust) RUST="${2:?--rust needs a toolchain}"; shift 2 ;;
    --setup) SETUP_CMD="${2:?--setup needs a command}"; shift 2 ;;
    --artifact) ARTIFACTS+=("${2:?--artifact needs a path}"); shift 2 ;;
    --keep) KEEP=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    --) shift; break ;;
    *) die "unknown option $1 (see the usage at the top of $0)" 2 ;;
  esac
done
(($#)) || die "no command given; usage: burst-offload -- cargo test" 2
[[ -d "${REPO}" ]] || die "no such directory: ${REPO}" 2
[[ "${TTL}" =~ ^[0-9]+$ ]] || die "--ttl takes whole minutes" 2
REPO="$(cd "${REPO}" && pwd)"
for tool in aws-burst aws rsync ssh python3; do
  command -v "${tool}" >/dev/null 2>&1 || die "${tool} not found" 4
done

COMMAND="$(printf '%q ' "$@")"
RUN_ID="$(date +%Y%m%d-%H%M%S)-$$"
RUN_DIR="${RUN_ROOT}/${RUN_ID}"
RUN_LOG="${RUN_DIR}/run.log"

if ((DRY_RUN)); then
  python3 -c 'import json,sys; print(json.dumps(dict(dry_run=True, repo=sys.argv[1], type=sys.argv[2] or "c7i-flex.large", ttl_min=int(sys.argv[3]), rust=sys.argv[4] or None, setup=sys.argv[5] or None, command=sys.argv[6], artifacts=sys.argv[7:])))' \
    "${REPO}" "${TYPE}" "${TTL}" "${RUST}" "${SETUP_CMD}" "${COMMAND}" "${ARTIFACTS[@]}"
  exit 0
fi

mkdir -p "${RUN_DIR}"
STARTED=$(date +%s)
up_args=(--ttl "${TTL}" --name burst-offload)
[[ -n "${TYPE}" ]] && up_args+=(--type "${TYPE}")
UP_JSON="$(aws-burst up "${up_args[@]}" 2>>"${RUN_LOG}")" || die "aws-burst up failed (see ${RUN_LOG})" 3
read -r ID IP INSTANCE_TYPE REGION < <(python3 -c 'import json,sys; d=json.loads(sys.argv[1]); print(d["id"], d["ip"], d["type"], d["region"])' "${UP_JSON}") \
  || die "aws-burst up gave no instance: ${UP_JSON}" 3
note "instance ${ID} (${INSTANCE_TYPE}, ${REGION}) at ${IP}"

finish() {
  if ((KEEP)); then
    note "left running: ${ID} (end it with: aws-burst down ${ID})"
  else
    aws-burst down "${ID}" >>"${RUN_LOG}" 2>&1 || note "could not terminate ${ID}; it powers off by itself within ${TTL} minutes"
  fi
}
trap finish EXIT
# A trapped signal would otherwise resume the script after the cleanup; exiting runs it.
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

read -ra SSH_OPTS <<<"$(aws-burst sshopts) -o LogLevel=ERROR -o BatchMode=yes"
remote() { ssh "${SSH_OPTS[@]}" "ec2-user@${IP}" "$@"; }

prepare='set -e; sudo dnf install -y -q rsync gcc gcc-c++ make git tar >/dev/null; mkdir -p ~/work'
if [[ -n "${RUST}" ]]; then
  prepare+="; curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y -q --profile minimal --default-toolchain $(printf '%q' "${RUST}") -c clippy -c rustfmt"
fi
note "preparing the instance"
remote "${prepare}" >>"${RUN_LOG}" 2>&1 || die "preparing ${ID} failed (see ${RUN_LOG})" 5

note "pushing ${REPO}"
if git -C "${REPO}" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  git -C "${REPO}" ls-files -co --exclude-standard -z \
    | rsync -az --from0 --files-from=- -e "ssh ${SSH_OPTS[*]}" "${REPO}/" "ec2-user@${IP}:work/" 2>>"${RUN_LOG}" \
    || die "rsync push failed (see ${RUN_LOG})" 6
else
  exclude_args=()
  for pattern in ${EXCLUDES}; do exclude_args+=(--exclude "${pattern}"); done
  rsync -az "${exclude_args[@]}" -e "ssh ${SSH_OPTS[*]}" "${REPO}/" "ec2-user@${IP}:work/" 2>>"${RUN_LOG}" \
    || die "rsync push failed (see ${RUN_LOG})" 6
fi

{
  printf '%s\n' '[ -f ~/.cargo/env ] && . ~/.cargo/env' 'cd ~/work'
  if [[ -n "${SETUP_CMD}" ]]; then
    printf '%s\n' "${SETUP_CMD}" 'setup_rc=$?' '[ "$setup_rc" -eq 0 ] || { echo "burst-offload: setup failed with $setup_rc" >&2; exit "$setup_rc"; }'
  fi
  printf '%s\n' "${COMMAND}"
} >"${RUN_DIR}/run.sh"

note "running: ${COMMAND}"
remote bash -s <"${RUN_DIR}/run.sh" 2>&1 | tee -a "${RUN_LOG}" >&2
rc=${PIPESTATUS[0]}

ARTIFACT_DIR="${RUN_DIR}/artifacts"
pulled=()
for rel in "${ARTIFACTS[@]}"; do
  mkdir -p "$(dirname "${ARTIFACT_DIR}/${rel}")"
  if rsync -az -e "ssh ${SSH_OPTS[*]}" "ec2-user@${IP}:work/${rel}" "${ARTIFACT_DIR}/${rel}" 2>>"${RUN_LOG}"; then
    pulled+=("${rel}")
  else
    note "artifact not found: ${rel}"
  fi
done

python3 -c 'import json,sys; print(json.dumps(dict(ok=sys.argv[1]=="0", exit=int(sys.argv[1]), instance=sys.argv[2], type=sys.argv[3], region=sys.argv[4], seconds=int(sys.argv[5]), log=sys.argv[6], artifacts=sys.argv[7], pulled=sys.argv[8:])))' \
  "${rc}" "${ID}" "${INSTANCE_TYPE}" "${REGION}" "$(($(date +%s) - STARTED))" "${RUN_LOG}" "${ARTIFACT_DIR}" "${pulled[@]}"
exit "${rc}"
