#!/bin/bash
# Sync ZCode chat sessions through a PRIVATE GitHub repo, as an
# asynchronous fallback for warp (works when the other machine is off).
#
#   session-sync push     snapshot local sessions for cwd into the repo
#   session-sync pull     import everything new from the repo as NEW
#                         sessions — existing sessions are never touched
#   session-sync status
#
# Repo: settings come from ~/.config/remote-agent/session-sync.env:
#   SESSION_SYNC_REPO=git@github.com:<user>/<private-repo>.git
#
# Safety model:
# - push never force-pushes and only appends commits on one branch.
# - pull always imports with --as-new: fresh ids, so nothing already in
#   the local ZCode db can be overwritten. Worst case is a duplicate.
set -euo pipefail

ENV_FILE="${SESSION_SYNC_ENV:-${HOME}/.config/remote-agent/session-sync.env}"
SYNC_ROOT="${SESSION_SYNC_ROOT:-${HOME}/.local/share/zcode-session-sync}"
BRANCH="main"

# warp_zcode.py lives next to this script in the repo, or beside the
# installed copy under /usr/local/lib/spectre-warp.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -f "${SCRIPT_DIR}/warp_zcode.py" ]]; then
  WARP_ZCODE="${SCRIPT_DIR}/warp_zcode.py"
elif [[ -f /usr/local/lib/spectre-warp/warp_zcode.py ]]; then
  WARP_ZCODE=/usr/local/lib/spectre-warp/warp_zcode.py
else
  echo "warp_zcode.py not found (repo checkout or install-warp.sh)" >&2
  exit 1
fi

usage() {
  cat <<'EOF'
session-sync push      commit + push a snapshot of this directory's sessions
session-sync pull      fetch the repo and import new sessions (always as-new)
session-sync status    show repo, last push, unpushed changes

One-time setup:
  cp config/session-sync.env.example ~/.config/remote-agent/session-sync.env
  # set SESSION_SYNC_REPO to your private repo, then:
  session-sync push
EOF
}

load_env() {
  if [[ ! -f "${ENV_FILE}" ]]; then
    echo "missing ${ENV_FILE}" >&2
    echo "copy config/session-sync.env.example there and set" >&2
    echo "SESSION_SYNC_REPO (the repo must be PRIVATE)." >&2
    exit 1
  fi
  # shellcheck disable=SC1090
  source "${ENV_FILE}"
  if [[ -z "${SESSION_SYNC_REPO:-}" ]]; then
    echo "SESSION_SYNC_REPO not set in ${ENV_FILE}" >&2
    exit 1
  fi
}

ensure_repo() {
  if [[ ! -d "${SYNC_ROOT}/.git" ]]; then
    install -d -m 0700 "$(dirname "${SYNC_ROOT}")"
    if [[ -d "${SYNC_ROOT}" ]]; then
      mv "${SYNC_ROOT}" "${SYNC_ROOT}.old.$(date +%s)"
    fi
    git clone --depth 1 "${SESSION_SYNC_REPO}" "${SYNC_ROOT}"
    git -C "${SYNC_ROOT}" config user.name "session-sync"
    git -C "${SYNC_ROOT}" config user.email "session-sync@${HOSTNAME}"
  fi
}

cmd_status() {
  load_env
  echo "repo:     ${SESSION_SYNC_REPO}"
  echo "local:    ${SYNC_ROOT}"
  if [[ -d "${SYNC_ROOT}/.git" ]]; then
    git -C "${SYNC_ROOT}" log --oneline -3 || true
    echo "uncommitted snapshots: $(git -C "${SYNC_ROOT}" status --porcelain | wc -l)"
  else
    echo "(repo not cloned yet — run 'session-sync push' once)"
  fi
}

cmd_push() {
  load_env
  ensure_repo
  local path
  path="$(realpath -m "${1:-$(pwd -P)}")"
  local hash
  hash="$(printf '%s' "${path}" | md5sum | cut -c1-10)"
  local out
  out="${SYNC_ROOT}/snapshots/$(hostname -s)/${hash}"
  install -d -m 0700 "${out}"

  python3 "${WARP_ZCODE}" export \
    --db "${HOME}/.zcode/cli/db/db.sqlite" \
    --directory "${path}" \
    --out "${out}/sessions.db" >/dev/null \
    || { echo "no sessions exported for ${path}"; exit 0; }
  [[ -s "${out}/sessions.db" ]] || { echo "no sessions for ${path}; nothing to push"; exit 0; }

  printf '%s\n' "${path}" >"${out}/source-path.txt"
  printf '%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >"${out}/exported-at.txt"

  git -C "${SYNC_ROOT}" add snapshots/
  if git -C "${SYNC_ROOT}" diff --cached --quiet; then
    echo "nothing changed since the last push"
    return 0
  fi
  git -C "${SYNC_ROOT}" commit -q -m "sessions: $(hostname -s) $(basename "${path}") $(date -u +%Y%m%dT%H%M%SZ)"
  # Plain append-only push; the guard blocks any forced variant on the box.
  git -C "${SYNC_ROOT}" push -q origin "${BRANCH}"
  echo "pushed $(basename "${path}") sessions -> ${SESSION_SYNC_REPO}"
}

cmd_pull() {
  load_env
  ensure_repo
  git -C "${SYNC_ROOT}" pull -q --ff-only origin "${BRANCH}" ||
    { echo "pull diverged — resolve by hand in ${SYNC_ROOT}" >&2; exit 1; }

  local imported_total=0 bundle db
  while IFS= read -r db; do
    bundle="$(mktemp /tmp/sessionsync-XXXXXX.db)"
    cp "${db}" "${bundle}"
    if zcode_running_check; then
      echo "ZCode is running — quit it, then re-run 'session-sync pull'" >&2
      rm -f "${bundle}"
      exit 2
    fi
    summary="$(python3 "${WARP_ZCODE}" import \
      --db "${HOME}/.zcode/cli/db/db.sqlite" \
      --from "${bundle}" \
      --as-new)"
    echo "imported from ${db#"${SYNC_ROOT}"/}: ${summary}"
    imported_total=$((imported_total + 1))
    rm -f "${bundle}"
  done < <(find "${SYNC_ROOT}/snapshots" -name sessions.db 2>/dev/null)

  (( imported_total > 0 )) || echo "no snapshots found in the repo"
  echo "done. imports are always additive — nothing existing was replaced."
}

zcode_running_check() {
  pgrep -f '/zcode|[/ ]ZCode' >/dev/null 2>&1
}

case "${1:-}" in
  push)   shift; cmd_push "$@" ;;
  pull)   shift; cmd_pull "$@" ;;
  status) cmd_status ;;
  ""|-h|--help|help) usage ;;
  *) usage; exit 2 ;;
esac
