#!/bin/bash
# codex-handoff — hand a Codex CLI session to the other machine.
#
#   codex-handoff push [id|prefix] [--peer spectre] [--all] [--replace] [--open]
#   codex-handoff pull [id|prefix] [--peer spectre] [--all] [--replace] [--open]
#   codex-handoff list [--peer spectre] [--all] [--json]
#   codex-handoff status
#
# A session is one or more rollout files under $CODEX_HOME/sessions (long
# sessions are paginated into `_<uuid>` shards), so a handoff copies every
# shard for that id. Measured 2026-09-15, codex-cli 0.154.0: a rollout
# copied into a CODEX_HOME is resolvable by id with no index or database
# present, which is what makes the transfer a file copy.
#
# Selection defaults to the newest interactive session recorded for the
# current directory (`--all` ignores the directory, `--include-non-interactive`
# adds `codex exec` sessions, mirroring codex's own resume flags).
#
# Safety model (same spirit as warp/session-sync):
# - additive: an identical session on the peer is a no-op, so re-running is safe
# - a *different* copy of the same id is a fork point and is refused; pass
#   --replace to overwrite it after the peer's copy is backed up in place
# - neither direction ever deletes a session
#
# Both machines are assumed to use the same absolute paths (as warp does);
# the peer's $HOME is checked before anything is written.
set -euo pipefail

SOURCE="${BASH_SOURCE[0]}"
while [[ -L "${SOURCE}" ]]; do
  dir="$(cd -P "$(dirname "${SOURCE}")" && pwd)"
  SOURCE="$(readlink "${SOURCE}")"
  [[ ${SOURCE} != /* ]] && SOURCE="${dir}/${SOURCE}"
done
HERE="$(cd -P "$(dirname "${SOURCE}")" && pwd)"
HELPER=""
for candidate in "${HERE}/codex_rollout.py" \
  "${HERE}/../scripts/codex_rollout.py" \
  /usr/local/lib/spectre-codex/codex_rollout.py \
  "${HOME}/.local/lib/spectre-codex/codex_rollout.py" \
  /usr/local/lib/spectre-warp/codex_rollout.py \
  "${HOME}/.local/lib/spectre-warp/codex_rollout.py"; do
  if [[ -f "${candidate}" ]]; then
    HELPER="${candidate}"
    break
  fi
done
if [[ -z "${HELPER}" ]]; then
  echo "codex-handoff: codex_rollout.py not found (repo checkout or install-warp.sh)" >&2
  exit 1
fi

CODEX_HOME_DIR="${CODEX_HOME:-${HOME}/.codex}"
SESSIONS_DIR="${CODEX_HOME_DIR}/sessions"
USER_NAME="${CODEX_HANDOFF_USER:-${USER:-person}}"
STATE_DIR="${XDG_STATE_HOME:-${HOME}/.local/state}/spectre-codex-handoff"
REMOTE_HELPER_CACHE="${HOME}/.cache/spectre-codex-handoff/codex_rollout.py"

usage() {
  cat <<'EOF'
codex-handoff push [id|prefix]   hand the selected session to the other machine
codex-handoff pull [id|prefix]   bring the other machine's session here
codex-handoff list [--peer P]    show sessions (local, or the peer's with --peer)
codex-handoff status             peer, last handoff, ledger tail

Options (push/pull):
  --peer <host>            explicit peer (default: the other of fedora/spectre)
  --all                    ignore the recorded directory
  --include-non-interactive  also consider `codex exec` sessions
  --replace                overwrite a differing peer copy (backs it up first)
  --open                   run `codex resume` on the receiving side right away

Options (list):
  --peer <host>            list the peer's sessions instead of the local ones
  --json                   machine-readable output
  --limit N                stop after N sessions

Default session: the newest interactive session recorded for the current
directory. An explicit id (or unique prefix) is matched regardless of
directory. The receiver resumes with `codex resume <id>`; a handed-off
session keeps its original absolute path, so the workspace must be there
too (`warp push` moves it).
EOF
}

die() { echo "codex-handoff: $*" >&2; exit 1; }

this_host() { hostname -s | tr '[:upper:]' '[:lower:]'; }

# The peer is the other machine; explicit names still work.
resolve_peer() {
  local peer="${1:-}"
  case "${peer}" in
    spectre|Spectre|SPECTRE|fedora|Fedora|FEDORA|zenbook|duo) ;;
    "") ;;
    *) echo "unknown peer '${peer}' (use fedora or spectre)" >&2; return 1 ;;
  esac
  if [[ -z "${peer}" ]]; then
    case "$(this_host)" in
      spectre*) peer=fedora ;;
      fedora*|zenbook*|duo*) peer=spectre ;;
      *)
        echo "hostname '$(this_host)' is neither fedora nor spectre;" >&2
        echo "pass it explicitly: --peer <host>" >&2
        return 1
        ;;
    esac
  fi
  case "${peer}" in
    zenbook|duo) peer=fedora ;;
  esac
  if [[ "${peer}" == "$(this_host)" ]]; then
    echo "peer resolves to this machine (${peer}); nothing to do" >&2
    return 1
  fi
  if command -v tailscale >/dev/null 2>&1 && command -v jq >/dev/null 2>&1; then
    if ! tailscale status --json 2>/dev/null | jq -e --arg h "${peer}" '
          .Peer[]? | select(.HostName==$h or .DNSName==($h+".") or (.DNSName|startswith($h+".")))
        ' >/dev/null; then
      echo "peer '${peer}' is not in this tailnet. tailscale status:" >&2
      tailscale status >&2 || true
      return 1
    fi
  fi
  printf '%s\n' "${peer}"
}

# Non-login ssh shells miss ~/.local/bin (install-warp.sh user installs).
ssh_peer() {
  local peer="$1"
  shift
  ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 \
    "${USER_NAME}@${peer}" "PATH=\"\${HOME}/.local/bin:\${PATH}\"; export PATH; $*"
}

now_iso() { date -u +%Y-%m-%dT%H:%M:%SZ; }

# One field out of a JSON object on stdin-friendly terms (no jq dependency).
json_field() {
  python3 -c 'import json,sys; value=json.loads(sys.argv[1]).get(sys.argv[2],""); print(value if not isinstance(value,bool) else str(value).lower())' "$1" "$2"
}

# --- options ---------------------------------------------------------------

SESSION_REF=""
PEER_ARG=""
ALL_DIRS=0
INCLUDE_NON_INTERACTIVE=0
REPLACE=0
OPEN_AFTER=0
JSON_OUT=0
LIMIT=""
SELECTION=()

parse_options() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --peer) [[ $# -ge 2 ]] || die "--peer needs a host"; PEER_ARG="$2"; shift 2 ;;
      --peer=*) PEER_ARG="${1#*=}"; shift ;;
      --all) ALL_DIRS=1; shift ;;
      --include-non-interactive) INCLUDE_NON_INTERACTIVE=1; shift ;;
      --replace) REPLACE=1; shift ;;
      --open) OPEN_AFTER=1; shift ;;
      --json) JSON_OUT=1; shift ;;
      --limit) [[ $# -ge 2 ]] || die "--limit needs a number"; LIMIT="$2"; shift 2 ;;
      --limit=*) LIMIT="${1#*=}"; shift ;;
      -h|--help) usage; exit 0 ;;
      --) shift; while [[ $# -gt 0 ]]; do SESSION_REF="$1"; shift; done ;;
      -*) die "unknown option '$1' (see --help)" ;;
      *) SESSION_REF="$1"; shift ;;
    esac
  done
}

# Selection flags for codex_rollout.py — identical on both machines.
selection_args() {
  SELECTION=()
  if (( ALL_DIRS )); then SELECTION+=(--all); else SELECTION+=(--cwd "${PWD}"); fi
  if (( INCLUDE_NON_INTERACTIVE )); then SELECTION+=(--include-non-interactive); fi
  if [[ -n "${SESSION_REF}" ]]; then SELECTION+=(--id "${SESSION_REF}"); fi
}

local_meta() {
  python3 "${HELPER}" meta "${SELECTION[@]}" ||
    die "session selection failed (message above)"
}

# --- peer-side helper ------------------------------------------------------

remote_helper_path() {
  local peer="$1" out
  # SC2016: the single-quoted body is evaluated on the peer, where $HOME is
  # that machine's home — it is deliberately not expanded here.
  # shellcheck disable=SC2016
  out="$(ssh_peer "${peer}" 'for c in /usr/local/lib/spectre-codex/codex_rollout.py "$HOME/.local/lib/spectre-codex/codex_rollout.py" /usr/local/lib/spectre-warp/codex_rollout.py "$HOME/.local/lib/spectre-warp/codex_rollout.py" "$HOME/.cache/spectre-codex-handoff/codex_rollout.py"; do
      if [ -f "$c" ]; then printf "%s" "$c"; exit 0; fi
    done; exit 1' 2>/dev/null || true)"
  printf '%s' "${out}"
}

# The peer needs the rollout helper to enumerate its own sessions. Prefer an
# installed copy; otherwise drop one into the peer's cache (never root).
ensure_remote_helper() {
  local peer="$1" path
  path="$(remote_helper_path "${peer}")"
  if [[ -z "${path}" ]]; then
    ssh_peer "${peer}" 'command -v python3 >/dev/null 2>&1' ||
      die "python3 is not on ${peer}"
    # SC2016: evaluated on the peer (see remote_helper_path).
    # shellcheck disable=SC2016
    ssh_peer "${peer}" 'mkdir -p "$HOME/.cache/spectre-codex-handoff" &&
      cat > "$HOME/.cache/spectre-codex-handoff/codex_rollout.py" &&
      chmod 700 "$HOME/.cache/spectre-codex-handoff/codex_rollout.py"' <"${HELPER}" ||
      die "could not install the rollout helper on ${peer}"
    path="${REMOTE_HELPER_CACHE}"
  fi
  printf '%s' "${path}"
}

remote_meta() {
  local peer="$1" path="$2"
  shift 2
  ssh_peer "${peer}" "python3 '${path}' meta $*" ||
    die "session selection failed on ${peer} (message above)"
}

# --- shard bookkeeping -----------------------------------------------------

shard_rels() {
  python3 -c 'import json,sys
for shard in json.loads(sys.argv[1]).get("shards", []):
    print(shard["rel"])' "$1"
}

# "<rel> <sha256>" for each shard, or "<rel> missing". One round trip.
# A here-string, not a pipe: ssh reads stdin, and if the peer's shell exits
# early a pipe would kill the writer with SIGPIPE (exit 141) instead of
# reporting the real problem.
remote_shard_state() {
  local peer="$1" rels="$2"
  # SC2016: the loop body runs on the peer (see remote_helper_path).
  # shellcheck disable=SC2016
  ssh_peer "${peer}" 'while read -r rel; do
    p="$HOME/.codex/sessions/$rel"
    if [ -f "$p" ]; then printf "%s %s\n" "$rel" "$(sha256sum "$p" | cut -d" " -f1)"
    else printf "%s missing\n" "$rel"; fi
  done' <<<"${rels}"
}

# new | partial | already | differs — compares a session JSON against a
# state list ("<rel> <sha256>" or "<rel> missing") from either side.
classify_shards() {
  python3 -c 'import json,sys
local = json.loads(sys.argv[1]).get("shards", [])
remote = {}
for line in sys.argv[2].splitlines():
    parts = line.split()
    if len(parts) == 2 and parts[1] != "missing":
        remote[parts[0]] = parts[1]
present = [s for s in local if s["rel"] in remote]
changed = [s for s in present if remote[s["rel"]] != s["sha256"]]
if changed:
    print("differs")
elif present and len(present) == len(local):
    print("already")
elif present:
    print("partial")
else:
    print("new")' "$1" "$2"
}

shard_tab() {
  python3 -c 'import json,sys
data = json.loads(sys.argv[1])
for shard in data.get("shards", []):
    print(shard["rel"] + "\t" + shard["sha256"])' "$1"
}

local_shard_state() {
  local rel path
  while read -r rel; do
    [[ -n "${rel}" ]] || continue
    path="${SESSIONS_DIR}/${rel}"
    if [[ -f "${path}" ]]; then
      printf '%s %s\n' "${rel}" "$(sha256sum "${path}" | cut -d' ' -f1)"
    else
      printf '%s missing\n' "${rel}"
    fi
  done <<<"$1"
}

# Both machines keep the same absolute paths (warp's rule). Check the
# assumption instead of trusting it — a mismatch would scatter rollouts.
verify_same_home() {
  local peer="$1" remote_home
  # SC2016: expanded by the peer's shell, not by this one.
  # shellcheck disable=SC2016
  remote_home="$(ssh_peer "${peer}" 'printf %s "$HOME"')"
  [[ -n "${remote_home}" ]] || die "could not read \$HOME on ${peer}"
  [[ "${remote_home}" == "${HOME}" ]] ||
    die "peer \$HOME is ${remote_home}, local is ${HOME} — codex-handoff assumes the same paths (like warp); sync by hand instead"
}

REMOTE_SESSIONS_DIR="${HOME}/.codex/sessions"

# copy_shards <push|pull> <peer> <session-json> <copy_existing:0|1>
# Missing shards are always filled in; mismatching ones only when
# copy_existing=1 (--replace), and then the destination is backed up first.
copy_shards() {
  local direction="$1" peer="$2" json="$3" copy_existing="$4"
  local stamp state rel sha current src dst
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  if [[ "${direction}" == "push" ]]; then
    state="$(remote_shard_state "${peer}" "$(shard_rels "${json}")")"
  else
    state="$(local_shard_state "$(shard_rels "${json}")")"
  fi
  while IFS=$'\t' read -r rel sha; do
    [[ -n "${rel}" ]] || continue
    current="$(printf '%s\n' "${state}" | awk -v r="${rel}" '$1==r {print $2}')"
    if [[ "${current}" == "${sha}" ]]; then
      echo "  = ${rel} (already there)"
      continue
    fi
    if [[ "${current}" != "missing" && "${copy_existing}" != "1" ]]; then
      echo "  = ${rel} (kept: peer copy differs, no --replace)"
      continue
    fi
    if [[ "${direction}" == "push" ]]; then
      src="${SESSIONS_DIR}/${rel}"
      dst="${REMOTE_SESSIONS_DIR}/${rel}"
      ssh_peer "${peer}" "install -d -m 0700 '$(dirname "${dst}")'"
      if [[ "${current}" != "missing" ]]; then
        ssh_peer "${peer}" "cp -p '${dst}' '${dst}.bak-${stamp}'"
      fi
      scp -q -o StrictHostKeyChecking=accept-new \
        "${src}" "${USER_NAME}@${peer}:${dst}.incoming"
      ssh_peer "${peer}" "mv -f '${dst}.incoming' '${dst}'"
      echo "  -> ${peer}: ${rel}"
    else
      src="${REMOTE_SESSIONS_DIR}/${rel}"
      dst="${SESSIONS_DIR}/${rel}"
      install -d -m 0700 "$(dirname "${dst}")"
      if [[ "${current}" != "missing" ]]; then
        cp -p "${dst}" "${dst}.bak-${stamp}"
      fi
      scp -q -o StrictHostKeyChecking=accept-new \
        "${USER_NAME}@${peer}:${src}" "${dst}.incoming"
      mv -f "${dst}.incoming" "${dst}"
      echo "  -> $(this_host): ${rel}"
    fi
  done < <(shard_tab "${json}")
}

# --- output and bookkeeping ------------------------------------------------

record_handoff() {
  local direction="$1" peer="$2" json="$3" id label
  install -d -m 0700 "${STATE_DIR}"
  id="$(json_field "${json}" id)"
  label="$(json_field "${json}" label | sed 's/["\\]//g')"
  printf '{"at":"%s","direction":"%s","peer":"%s","id":"%s","label":"%s","shards":%s,"lines":%s}\n' \
    "$(now_iso)" "${direction}" "${peer}" "${id}" "${label}" \
    "$(json_field "${json}" shard_count)" "$(json_field "${json}" lines)" \
    >>"${STATE_DIR}/handoff.jsonl"
  printf '{"direction":"%s","peer":"%s","id":"%s","at":"%s"}\n' \
    "${direction}" "${peer}" "${id}" "$(now_iso)" >"${STATE_DIR}/last.json"
}

session_name() {
  local base
  base="$(basename "${1:-${HOME}}")"
  printf 'codex-%s' "$(printf '%s' "${base}" | tr -c 'A-Za-z0-9._-' '-')"
}

# The command tmux runs on the receiving side. The directory comes from
# `tmux new-session -c`, not from a `cd` inside the command — that keeps the
# printed line free of nested quote styles that a paste would have to survive.
resume_cmd() {
  printf '. ~/.codex/modes/env.sh 2>/dev/null; codex resume %s' "$1"
}

print_hint() {
  local where="$1" peer="$2" json="$3" id cwd label name cmd
  id="$(json_field "${json}" id)"
  cwd="$(json_field "${json}" cwd)"
  label="$(json_field "${json}" label)"
  name="$(session_name "${cwd}")"
  cmd="$(resume_cmd "${id}")"
  echo
  echo "session ${id}  (${label:-no branch recorded})"
  echo "  recorded cwd: ${cwd:-(none)}"
  echo "  shards: $(json_field "${json}" shard_count)  records: $(json_field "${json}" lines)"
  if [[ "${where}" == "push" ]]; then
    echo "  resume on ${peer}:"
    echo "    ssh ${peer} -t 'tmux new-session -A -s ${name} -c \"${cwd}\" \"${cmd}\"'"
    echo "  without tmux there: ssh ${peer} -t \"cd '${cwd}' && ${cmd}\""
    echo "  the workspace must exist at ${cwd:-that path} on ${peer} — 'warp push' moves it."
  else
    echo "  resume here:"
    echo "    tmux new-session -A -s ${name} -c \"${cwd}\" \"${cmd}\""
    echo "  or just: codex resume ${id}"
  fi
}

open_session() {
  local where="$1" peer="$2" id="$3" cwd="$4" name cmd
  name="$(session_name "${cwd}")"
  cwd="${cwd:-${HOME}}"
  cmd="$(resume_cmd "${id}")"
  if [[ "${where}" == "pull" ]]; then
    if command -v tmux >/dev/null 2>&1; then
      exec tmux new-session -A -s "${name}" -c "${cwd}" "${cmd}"
    fi
    echo "codex-handoff: tmux is not installed here; resuming directly" >&2
    exec sh -c "cd \"${cwd}\" && ${cmd}"
  fi
  if ssh_peer "${peer}" 'command -v tmux >/dev/null 2>&1'; then
    exec ssh -t -o StrictHostKeyChecking=accept-new "${USER_NAME}@${peer}" \
      "tmux new-session -A -s ${name} -c \"${cwd}\" \"${cmd}\""
  fi
  echo "codex-handoff: tmux is not installed on ${peer}; resuming directly" >&2
  exec ssh -t -o StrictHostKeyChecking=accept-new "${USER_NAME}@${peer}" \
    "cd \"${cwd}\" && ${cmd}"
}

report_fork_point() {
  local direction="$1" peer="$2" id="$3"
  echo "codex-handoff: ${id} is already on ${peer} with different content." >&2
  echo "That copy may have advanced there — overwriting it discards that work." >&2
  echo "  bring it here first: codex-handoff pull ${id}" >&2
  echo "  or overwrite it:     codex-handoff ${direction} ${id} --replace" >&2
  echo "                       (the ${peer} copy is backed up in place as *.bak-<utc>)" >&2
}

run_copy() {
  local direction="$1" peer="$2" json="$3" verdict="$4"
  case "${verdict}" in
    already)
      echo "  identical copy on the other side — nothing to copy"
      ;;
    differs)
      if (( ! REPLACE )); then
        report_fork_point "${direction}" "${peer}" "$(json_field "${json}" id)"
        exit 3
      fi
      copy_shards "${direction}" "${peer}" "${json}" 1
      ;;
    *)
      copy_shards "${direction}" "${peer}" "${json}" 0
      ;;
  esac
}

# --- commands --------------------------------------------------------------

LIST_SELECTION=()

list_args() {
  LIST_SELECTION=()
  if (( ALL_DIRS )); then LIST_SELECTION+=(--all); else LIST_SELECTION+=(--cwd "${PWD}"); fi
  if (( INCLUDE_NON_INTERACTIVE )); then LIST_SELECTION+=(--include-non-interactive); fi
  if (( JSON_OUT )); then LIST_SELECTION+=(--json); fi
  if [[ -n "${LIMIT}" ]]; then LIST_SELECTION+=(--limit "${LIMIT}"); fi
}

cmd_push() {
  parse_options "$@"
  selection_args
  local peer json id rels state verdict
  peer="$(resolve_peer "${PEER_ARG}")" || exit 1
  json="$(local_meta)"
  verify_same_home "${peer}"
  id="$(json_field "${json}" id)"
  rels="$(shard_rels "${json}")"
  echo "codex-handoff push: $(this_host) -> ${peer}   ${id}"
  ssh_peer "${peer}" "install -d -m 0700 '${REMOTE_SESSIONS_DIR}'"
  state="$(remote_shard_state "${peer}" "${rels}")"
  verdict="$(classify_shards "${json}" "${state}")"
  run_copy push "${peer}" "${json}" "${verdict}"
  record_handoff push "${peer}" "${json}"
  print_hint push "${peer}" "${json}"
  if (( OPEN_AFTER )); then
    open_session push "${peer}" "${id}" "$(json_field "${json}" cwd)"
  fi
}

cmd_pull() {
  parse_options "$@"
  selection_args
  local peer helper json id rels state verdict
  peer="$(resolve_peer "${PEER_ARG}")" || exit 1
  verify_same_home "${peer}"
  helper="$(ensure_remote_helper "${peer}")"
  json="$(remote_meta "${peer}" "${helper}" "$(printf '%q ' "${SELECTION[@]}")")"
  id="$(json_field "${json}" id)"
  rels="$(shard_rels "${json}")"
  echo "codex-handoff pull: ${peer} -> $(this_host)   ${id}"
  install -d -m 0700 "${SESSIONS_DIR}"
  state="$(local_shard_state "${rels}")"
  verdict="$(classify_shards "${json}" "${state}")"
  if [[ "${verdict}" == "differs" ]] && (( ! REPLACE )); then
    echo "codex-handoff: ${id} already exists here with different content" >&2
    echo "It may have advanced locally — that is a fork point, not an error." >&2
    echo "  keep the local copy:  do nothing" >&2
    echo "  take the peer's:      codex-handoff pull ${id} --replace" >&2
    echo "                        (the local copy is backed up in place as *.bak-<utc>)" >&2
    exit 3
  fi
  run_copy pull "${peer}" "${json}" "${verdict}"
  record_handoff pull "${peer}" "${json}"
  print_hint pull "${peer}" "${json}"
  if (( OPEN_AFTER )); then
    open_session pull "${peer}" "${id}" "$(json_field "${json}" cwd)"
  fi
}

cmd_list() {
  parse_options "$@"
  local peer helper
  if [[ -n "${PEER_ARG}" ]]; then
    peer="$(resolve_peer "${PEER_ARG}")" || exit 1
    verify_same_home "${peer}"
    helper="$(ensure_remote_helper "${peer}")"
    list_args
    if (( ! JSON_OUT )); then
      echo "# ${peer}: sessions under ${REMOTE_SESSIONS_DIR}"
    fi
    ssh_peer "${peer}" "python3 '${helper}' list $(printf '%q ' "${LIST_SELECTION[@]}")"
    return 0
  fi
  list_args
  if (( ! JSON_OUT )); then
    echo "# $(this_host): sessions under ${SESSIONS_DIR}"
  fi
  python3 "${HELPER}" list "${LIST_SELECTION[@]}"
}

cmd_status() {
  local peer count
  echo "host:     $(this_host)"
  echo "user:     ${USER_NAME}"
  echo "codex:    ${CODEX_HOME_DIR}"
  echo "helper:   ${HELPER}"
  peer="$(resolve_peer 2>/dev/null || true)"
  if [[ -n "${peer}" ]]; then
    echo "peer:     ${peer}"
    if ssh -o BatchMode=yes -o ConnectTimeout=8 -o ServerAliveInterval=5 \
      -o ServerAliveCountMax=2 "${USER_NAME}@${peer}" true 2>/dev/null; then
      echo "peer ssh: ok"
    else
      echo "peer ssh: not answering — if it printed a login.tailscale.com URL,"
      echo "          approve that 24 h check (RUNBOOK 7.4) and re-run"
    fi
  else
    echo "peer:     unavailable (other machine not in the tailnet yet?)"
  fi
  count="$(python3 "${HELPER}" list --all --include-non-interactive --json 2>/dev/null |
    python3 -c 'import json,sys; print(len(json.load(sys.stdin)))' 2>/dev/null || true)"
  echo "sessions: ${count:-0} local"
  if [[ -f "${STATE_DIR}/last.json" ]]; then
    echo "last handoff:"
    sed 's/^/  /' "${STATE_DIR}/last.json"
  else
    echo "last handoff: none recorded"
  fi
  if [[ -f "${STATE_DIR}/handoff.jsonl" ]]; then
    echo "ledger (last 3):"
    tail -3 "${STATE_DIR}/handoff.jsonl" | sed 's/^/  /'
  fi
}

main() {
  [[ $# -ge 1 ]] || { usage; exit 2; }
  local command="$1"
  shift
  case "${command}" in
    push) cmd_push "$@" ;;
    pull) cmd_pull "$@" ;;
    list|ls) cmd_list "$@" ;;
    status) cmd_status ;;
    -h|--help|help) usage ;;
    *) usage; exit 2 ;;
  esac
}

# Sourcing the file (tests) defines the helpers without running a command.
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  main "$@"
fi