#!/bin/bash
# Move a live project + its ZCode sessions between fedora and spectre.
# Same absolute path on both machines.
#
#   warp push     this project -> the other machine
#   warp pull     the other machine's project -> here
#   warp status
#
# No peer argument: exactly two machines exist, so the peer is the
# machine this one is not. "to/from spectre|fedora" still work.
set -euo pipefail

SOURCE="${BASH_SOURCE[0]}"
while [[ -L "${SOURCE}" ]]; do
  dir="$(cd -P "$(dirname "${SOURCE}")" && pwd)"
  SOURCE="$(readlink "${SOURCE}")"
  [[ ${SOURCE} != /* ]] && SOURCE="${dir}/${SOURCE}"
done
HERE="$(cd -P "$(dirname "${SOURCE}")" && pwd)"
PY="${HERE}/warp_zcode.py"
EXCLUDES="${HERE}/warp-excludes.txt"
if [[ ! -f "${EXCLUDES}" && -f "${HERE}/../config/warp-excludes.txt" ]]; then
  EXCLUDES="${HERE}/../config/warp-excludes.txt"
fi

USER_NAME="${WARP_USER:-${USER:-person}}"
STATE_DIR="${XDG_STATE_HOME:-${HOME}/.local/state}/spectre-warp"
ZCODE_DB="${HOME}/.zcode/cli/db/db.sqlite"
ZCODE_ARTIFACTS="${HOME}/.zcode/cli/artifacts"
ZCODE_AGENTS="${HOME}/.zcode/cli/agents"

usage() {
  cat <<'EOF'
warp push      send this project + its ZCode sessions to the other machine
warp pull      bring the other machine's project + sessions here
warp status

No arguments needed: the peer is whichever of fedora/spectre this box
is not. Run inside the project directory; both machines must use the
same absolute path (/home/person/Projects/...). Build dirs (target,
node_modules) are skipped.

Sessions move only when the receiving machine still runs ZCode (the
box retired it 2026-09-15): there, files land immediately and the
session import waits in the background for ZCode to quit (10 min),
then applies itself.
EOF
}

zcode_running() {
  pgrep -f '/zcode|[/ ]ZCode' >/dev/null 2>&1
}

this_host() {
  hostname -s | tr '[:upper:]' '[:lower:]'
}

# The peer is the other machine. Accept legacy explicit peers too.
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
        echo "pass it explicitly: warp push fedora" >&2
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
  if command -v tailscale >/dev/null 2>&1; then
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

resolve_path() {
  local p="${1:-}"
  if [[ -z "${p}" ]]; then
    p="$(pwd -P)"
  fi
  realpath -m "${p}"
}

ssh_peer() {
  local peer="$1"
  shift
  # Non-login ssh shells often miss ~/.local/bin, where install-warp.sh
  # puts the binary on user installs (fedora). Prefix PATH explicitly.
  ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=8 \
    "${USER_NAME}@${peer}" "PATH=\"\${HOME}/.local/bin:\${PATH}\"; export PATH; $*"
}

rsync_to() {
  local peer="$1" src="$2" dest="$3"
  rsync -aH --partial --info=stats2 \
    --exclude-from="${EXCLUDES}" \
    -e "ssh -o StrictHostKeyChecking=accept-new" \
    "${src%/}/" "${USER_NAME}@${peer}:${dest%/}/"
}

# The Spectre retired ZCode on 2026-09-15; a peer without its db gets
# files only, no session bundle and no import scheduling.
peer_has_zcode() {
  ssh_peer "$1" "test -f '${ZCODE_DB}'" >/dev/null 2>&1
}

session_ids_from_bundle() {
  sqlite3 "$1" 'SELECT id FROM session' 2>/dev/null || true
}

cmd_status() {
  echo "host:     $(this_host)"
  if peer="$(resolve_peer 2>/dev/null)"; then
    echo "peer:     ${peer}"
  else
    echo "peer:     unavailable (other machine not in the tailnet yet?)"
  fi
  echo "user:     ${USER_NAME}"
  echo "cwd:      $(pwd -P)"
  if command -v tailscale >/dev/null 2>&1; then
    echo "tailscale:"
    tailscale status | awk 'NR==1 || /fedora|spectre|z-fold/'
  fi
  if [[ -f "${STATE_DIR}/last.json" ]]; then
    echo "last warp:"
    cat "${STATE_DIR}/last.json"
  fi
}

send_sessions() {
  local peer="$1" path="$2" remote_tmp="$3"
  if [[ ! -f "${ZCODE_DB}" ]]; then
    echo "no local ZCode db; sending files only"
    return 0
  fi
  local bundle="${remote_tmp}/sessions.db"
  mkdir -p "${STATE_DIR}"
  local local_bundle="${STATE_DIR}/sessions.db"
  python3 "${PY}" export --db "${ZCODE_DB}" --directory "${path}" --out "${local_bundle}" || true
  if [[ ! -s "${local_bundle}" ]]; then
    echo "no ZCode sessions for ${path}"
    return 0
  fi
  scp -o StrictHostKeyChecking=accept-new "${local_bundle}" "${USER_NAME}@${peer}:${bundle}"
  local id
  while read -r id; do
    [[ -z "${id}" ]] && continue
    if [[ -d "${ZCODE_ARTIFACTS}/${id}" ]]; then
      rsync -a -e "ssh -o StrictHostKeyChecking=accept-new" \
        "${ZCODE_ARTIFACTS}/${id}/" \
        "${USER_NAME}@${peer}:${HOME}/.zcode/cli/artifacts/${id}/"
    fi
    if [[ -d "${ZCODE_AGENTS}/${id}" ]]; then
      rsync -a -e "ssh -o StrictHostKeyChecking=accept-new" \
        "${ZCODE_AGENTS}/${id}/" \
        "${USER_NAME}@${peer}:${HOME}/.zcode/cli/agents/${id}/"
    fi
  done < <(session_ids_from_bundle "${local_bundle}")
}

apply_sessions() {
  local bundle="$1"
  if [[ ! -f "${bundle}" ]]; then
    return 0
  fi
  if [[ ! -f "${ZCODE_DB}" ]]; then
    echo "open ZCode once on this machine so ${ZCODE_DB} exists, then rerun warp apply" >&2
    return 2
  fi
  python3 "${PY}" import --db "${ZCODE_DB}" --from "${bundle}"
}

# Schedule the session import on the receiving machine: wait for ZCode
# to quit (max 10 min), then apply. Runs detached so the ssh session
# can close. The sender never needs to think about it.
schedule_remote_apply() {
  local peer="$1" bundle="$2" path="$3"
  ssh_peer "${peer}" "nohup sh -c '
    for i in \$(seq 1 60); do
      pgrep -f \"/zcode|[/ ]ZCode\" >/dev/null 2>&1 || break
      sleep 10
    done
    if pgrep -f \"/zcode|[/ ]ZCode\" >/dev/null 2>&1; then
      echo \"\$(date -u +%FT%TZ) zcode still open after 10m; bundle kept at ${bundle}\" >>\${HOME}/.cache/spectre-warp/apply.log
    else
      PATH=\${HOME}/.local/bin:\$PATH warp apply \"${bundle}\" \"${path}\" >>\${HOME}/.cache/spectre-warp/apply.log 2>&1 \
        && echo \"\$(date -u +%FT%TZ) applied ${bundle}\" >>\${HOME}/.cache/spectre-warp/apply.log
    fi
  ' >/dev/null 2>&1 &"
}

write_last() {
  mkdir -p "${STATE_DIR}"
  if command -v jq >/dev/null 2>&1; then
    jq -n \
      --arg direction "$1" \
      --arg peer "$2" \
      --arg path "$3" \
      --arg at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
      '{direction:$direction, peer:$peer, path:$path, at:$at}' \
      >"${STATE_DIR}/last.json"
  else
    printf '{"direction":"%s","peer":"%s","path":"%s"}\n' "$1" "$2" "$3" \
      >"${STATE_DIR}/last.json"
  fi
}

cmd_push() {
  local peer path remote_tmp
  peer="$(resolve_peer "${1:-}")"
  path="$(resolve_path "${2:-}")"
  [[ -d "${path}" ]] || { echo "not a directory: ${path}" >&2; exit 1; }
  echo "warp push: $(this_host) -> ${peer}   ${path}"
  ssh_peer "${peer}" "mkdir -p '${path}' '${HOME}/.cache/spectre-warp'"
  rsync_to "${peer}" "${path}" "${path}"
  if peer_has_zcode "${peer}"; then
    ssh_peer "${peer}" "mkdir -p '${HOME}/.zcode/cli/artifacts' '${HOME}/.zcode/cli/agents'"
    remote_tmp="$(ssh_peer "${peer}" "mktemp -d '${HOME}/.cache/spectre-warp/in.XXXXXX'")"
    send_sessions "${peer}" "${path}" "${remote_tmp}"
    schedule_remote_apply "${peer}" "${remote_tmp}/sessions.db" "${path}"
    write_last "push" "${peer}" "${path}"
    echo "pushed. sessions import on ${peer} as soon as ZCode there is closed."
  else
    write_last "push" "${peer}" "${path}"
    echo "pushed (files only). ${peer} has no ZCode — sessions skipped."
  fi
}

cmd_export_bundle() {
  local path out
  path="$(resolve_path "${1:-}")"
  mkdir -p "${HOME}/.cache/spectre-warp"
  out="$(mktemp "${HOME}/.cache/spectre-warp/out.XXXXXX.db")"
  if [[ ! -f "${ZCODE_DB}" ]]; then
    echo "${out}"
    return 0
  fi
  python3 "${PY}" export --db "${ZCODE_DB}" --directory "${path}" --out "${out}" >/dev/null || true
  printf '%s\n' "${out}"
}

cmd_pull() {
  local peer path remote_bundle local_bundle id
  peer="$(resolve_peer "${1:-}")"
  path="$(resolve_path "${2:-}")"
  echo "warp pull: ${peer} -> $(this_host)   ${path}"
  mkdir -p "${path}" "${STATE_DIR}"
  rsync -aH --partial --info=stats2 \
    --exclude-from="${EXCLUDES}" \
    -e "ssh -o StrictHostKeyChecking=accept-new" \
    "${USER_NAME}@${peer}:${path%/}/" "${path%/}/"
  local_bundle="${STATE_DIR}/from-sessions.db"
  remote_bundle=""
  if [[ ! -f "${ZCODE_DB}" ]]; then
    echo "files only — no ZCode on this machine (retired), sessions not pulled"
  elif ! peer_has_zcode "${peer}"; then
    echo "files only — ${peer} has no ZCode, nothing to pull"
  else
    remote_bundle="$(ssh_peer "${peer}" "warp export-bundle '${path}'")"
    if [[ -n "${remote_bundle}" ]]; then
      scp -o StrictHostKeyChecking=accept-new "${USER_NAME}@${peer}:${remote_bundle}" "${local_bundle}" || true
    fi
    if [[ -s "${local_bundle}" ]]; then
      while read -r id; do
        [[ -z "${id}" ]] && continue
        mkdir -p "${ZCODE_ARTIFACTS}/${id}" "${ZCODE_AGENTS}/${id}"
        rsync -a -e "ssh -o StrictHostKeyChecking=accept-new" \
          "${USER_NAME}@${peer}:${HOME}/.zcode/cli/artifacts/${id}/" \
          "${ZCODE_ARTIFACTS}/${id}/" 2>/dev/null || true
        rsync -a -e "ssh -o StrictHostKeyChecking=accept-new" \
          "${USER_NAME}@${peer}:${HOME}/.zcode/cli/agents/${id}/" \
          "${ZCODE_AGENTS}/${id}/" 2>/dev/null || true
      done < <(session_ids_from_bundle "${local_bundle}")
      if zcode_running; then
        # Same deal as push: files are here; import when ZCode quits.
        schedule_local_apply "${local_bundle}" "${path}"
        echo "pulled. ZCode is open here — sessions import automatically when it quits."
      else
        apply_sessions "${local_bundle}" || true
      fi
    fi
  fi
  write_last "pull" "${peer}" "${path}"
  echo "pull done"
}

schedule_local_apply() {
  local bundle="$1" path="$2"
  mkdir -p "${HOME}/.cache/spectre-warp"
  nohup sh -c "
    for i in \$(seq 1 60); do
      pgrep -f '/zcode|[/ ]ZCode' >/dev/null 2>&1 || break
      sleep 10
    done
    if pgrep -f '/zcode|[/ ]ZCode' >/dev/null 2>&1; then
      echo \"\$(date -u +%FT%TZ) zcode still open after 10m; bundle kept at ${bundle}\" >>${HOME}/.cache/spectre-warp/apply.log
    else
      PATH=${HOME}/.local/bin:\$PATH warp apply \"${bundle}\" \"${path}\" >>${HOME}/.cache/spectre-warp/apply.log 2>&1 \
        && echo \"\$(date -u +%FT%TZ) applied ${bundle}\" >>${HOME}/.cache/spectre-warp/apply.log
    fi
  " >/dev/null 2>&1 &
  disown || true
}

cmd_apply() {
  local bundle="${1:-}"
  [[ -n "${bundle}" ]] || { echo "warp apply <bundle.db>" >&2; exit 2; }
  apply_sessions "${bundle}"
}

main() {
  [[ $# -ge 1 ]] || { usage; exit 2; }
  case "$1" in
    -h|--help|help) usage ;;
    status) cmd_status ;;
    push|to)
      cmd_push "${2:-}"
      ;;
    pull|from)
      cmd_pull "${2:-}"
      ;;
    apply)
      cmd_apply "${2:-}"
      ;;
    export-bundle)
      cmd_export_bundle "${2:-}"
      ;;
    *)
      usage
      exit 2
      ;;
  esac
}

main "$@"
