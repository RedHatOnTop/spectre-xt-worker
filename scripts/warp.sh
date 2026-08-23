#!/bin/bash
# Move a live project + its ZCode sessions between fedora and spectre.
# Same absolute path on both machines. Push-oriented:
#   warp to spectre [path]
#   warp from spectre [path]
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
warp to <peer> [path]     send this workspace to the other machine
warp from <peer> [path]   pull that workspace onto this machine
warp status

Peers are Tailscale MagicDNS names. fedora <-> spectre.

Path defaults to the current directory. Keep the same absolute path on
both machines (/home/person/Projects/...). Build dirs (target, node_modules)
are skipped. Close ZCode on the destination before a receive.
EOF
}

zcode_running() {
  pgrep -f '/zcode|[/ ]ZCode' >/dev/null 2>&1
}

resolve_peer() {
  local peer="$1"
  case "${peer}" in
    spectre|Spectre|SPECTRE) peer=spectre ;;
    fedora|Fedora|FEDORA|zenbook|duo) peer=fedora ;;
  esac
  if command -v tailscale >/dev/null 2>&1; then
    if ! tailscale status --json 2>/dev/null | jq -e --arg h "${peer}" '
          .Peer[]? | select(.HostName==$h or .DNSName==($h+".") or (.DNSName|startswith($h+".")))
        ' >/dev/null; then
      echo "peer '${peer}' is not in this tailnet. tailscale status:" >&2
      tailscale status >&2 || true
      exit 1
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
  ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=8 "${USER_NAME}@${peer}" "$@"
}

rsync_to() {
  local peer="$1" src="$2" dest="$3"
  rsync -aH --partial --info=stats2 \
    --exclude-from="${EXCLUDES}" \
    -e "ssh -o StrictHostKeyChecking=accept-new" \
    "${src%/}/" "${USER_NAME}@${peer}:${dest%/}/"
}

session_ids_from_bundle() {
  sqlite3 "$1" 'SELECT id FROM session' 2>/dev/null || true
}

cmd_status() {
  echo "host:     $(hostname -s)"
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
  if zcode_running; then
    echo "ZCode is running here. Quit it, then: warp apply ${bundle}" >&2
    echo "files already arrived. sessions are waiting in ${bundle}" >&2
    return 2
  fi
  if [[ ! -f "${ZCODE_DB}" ]]; then
    echo "open ZCode once on this machine so ${ZCODE_DB} exists, then rerun warp apply" >&2
    return 2
  fi
  python3 "${PY}" import --db "${ZCODE_DB}" --from "${bundle}"
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

cmd_to() {
  local peer path remote_tmp
  peer="$(resolve_peer "$1")"
  path="$(resolve_path "${2:-}")"
  [[ -d "${path}" ]] || { echo "not a directory: ${path}" >&2; exit 1; }
  echo "warp to ${peer}: ${path}"
  ssh_peer "${peer}" "mkdir -p '${path}' '${HOME}/.zcode/cli/artifacts' '${HOME}/.zcode/cli/agents' '${HOME}/.cache/spectre-warp'"
  rsync_to "${peer}" "${path}" "${path}"
  remote_tmp="$(ssh_peer "${peer}" 'mktemp -d ${HOME}/.cache/spectre-warp/in.XXXXXX')"
  send_sessions "${peer}" "${path}" "${remote_tmp}"
  local rc=0
  ssh_peer "${peer}" "warp apply '${remote_tmp}/sessions.db' '${path}'" || rc=$?
  write_last "to" "${peer}" "${path}"
  if [[ ${rc} -eq 2 ]]; then
    echo "files are on ${peer}. close ZCode there and run: warp apply ${remote_tmp}/sessions.db"
    return 0
  fi
  echo "warp to ${peer} done"
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

cmd_from() {
  local peer path remote_bundle local_bundle id
  peer="$(resolve_peer "$1")"
  path="$(resolve_path "${2:-}")"
  echo "warp from ${peer}: ${path}"
  mkdir -p "${path}" "${ZCODE_ARTIFACTS}" "${ZCODE_AGENTS}"
  rsync -aH --partial --info=stats2 \
    --exclude-from="${EXCLUDES}" \
    -e "ssh -o StrictHostKeyChecking=accept-new" \
    "${USER_NAME}@${peer}:${path%/}/" "${path%/}/"
  remote_bundle="$(ssh_peer "${peer}" "warp export-bundle '${path}'")"
  local_bundle="${STATE_DIR}/from-sessions.db"
  mkdir -p "${STATE_DIR}"
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
    apply_sessions "${local_bundle}" || true
  fi
  write_last "from" "${peer}" "${path}"
  echo "warp from ${peer} done"
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
    to)
      [[ $# -ge 2 ]] || { usage; exit 2; }
      cmd_to "$2" "${3:-}"
      ;;
    from)
      [[ $# -ge 2 ]] || { usage; exit 2; }
      cmd_from "$2" "${3:-}"
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
