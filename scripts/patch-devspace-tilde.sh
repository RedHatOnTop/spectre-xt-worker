#!/bin/bash
# Patch the npm-installed @waishnav/devspace so its file tools expand a
# leading `~` instead of resolving it against the workspace root.
#
# Why (RUNBOOK 7.14): DevSpace advertises skill files tilde-formatted
# (`~/.agents/skills/<name>/SKILL.md`) and tells the model to `read` that
# exact advertised path, but `dist/roots.js` resolveAllowedPath() did
# `resolve(cwd, inputPath)`. A tilde path is not absolute, so it landed
# under the workspace — `<workspace>/~/.agents/skills/devcodex/SKILL.md` —
# and the read failed with ENOENT; the model had to guess the expanded path.
# The gate is the only resolver on that path that missed it: DevSpace's own
# skill loader (dist/skills.js) and the pi-coding-agent tools underneath both
# expand `~`. `read`/`write`/`edit` use the value this function returns, so
# the same bug also mis-wrote tilde paths under the workspace.
#
#   sudo bash scripts/patch-devspace-tilde.sh        # auto-detect, apply
#        bash scripts/patch-devspace-tilde.sh --check # probe only; exit 1 if unpatched
#        bash scripts/patch-devspace-tilde.sh --dist-dir /usr/lib/node_modules/@waishnav/devspace/dist
#   sudo bash scripts/patch-devspace-tilde.sh --restore
#
# Idempotent. The edit is one anchored line: a build whose code moved fails
# loudly instead of being half-patched. `npm install -g @waishnav/devspace`
# overwrites dist/, so re-run this after any upgrade.
set -euo pipefail

DIST_DIR=""
MODE="apply"
while (( $# > 0 )); do
  case "$1" in
    --dist-dir)
      DIST_DIR="${2:-}"
      shift 2
      ;;
    --check)
      MODE="check"
      shift
      ;;
    --restore)
      MODE="restore"
      shift
      ;;
    -h|--help)
      sed -n '2,25p' "${BASH_SOURCE[0]}"
      exit 0
      ;;
    *)
      echo "unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

BROKEN='    const absolutePath = resolve(cwd, inputPath);'
FIXED='    const absolutePath = resolve(cwd, expandHomePath(inputPath));'

if [[ -z "${DIST_DIR}" ]]; then
  devspace_bin="$(command -v devspace || true)"
  if [[ -z "${devspace_bin}" ]]; then
    echo "devspace is not on PATH — pass --dist-dir <dir> (the directory holding roots.js)" >&2
    exit 1
  fi
  DIST_DIR="$(dirname "$(readlink -f "${devspace_bin}")")"
fi
ROOTS="${DIST_DIR}/roots.js"
if [[ ! -f "${ROOTS}" ]]; then
  echo "not a DevSpace dist: ${ROOTS} does not exist" >&2
  exit 1
fi

# Behaviour probe, not a text match: resolve `~/probe` from an unrelated
# workspace and require an absolute home path outside that workspace. An
# unpatched build throws AccessDeniedError there (the workspace-relative
# `<cwd>/~/probe` is not inside the "~" root) — that is the failure case.
probe() {
  node --input-type=module -e "
import { resolveAllowedPath } from '${ROOTS}'
let probePath
try {
  probePath = resolveAllowedPath('~/probe', '/nonexistent-workspace', ['~'])
} catch {
  process.exit(1)
}
process.exit(probePath.startsWith('/') && probePath.endsWith('/probe') && !probePath.startsWith('/nonexistent-workspace') ? 0 : 1)
"
}

if [[ "${MODE}" == "check" ]]; then
  if probe; then
    echo "patched: ${ROOTS}"
    exit 0
  fi
  echo "unpatched: ${ROOTS} still resolves '~' against the workspace root" >&2
  exit 1
fi

if [[ "${MODE}" == "restore" ]]; then
  # Backups are date-suffixed, so the last glob match is the newest.
  shopt -s nullglob
  backups=("${ROOTS}".bak-*)
  shopt -u nullglob
  if (( ${#backups[@]} == 0 )); then
    echo "no backup next to ${ROOTS}" >&2
    exit 1
  fi
  backup="${backups[${#backups[@]} - 1]}"
  cp -a "${backup}" "${ROOTS}"
  echo "restored ${ROOTS} from ${backup}"
  exit 0
fi

if grep -qF "${BROKEN}" "${ROOTS}"; then
  broken_count="$(grep -cF "${BROKEN}" "${ROOTS}")"
  if [[ "${broken_count}" != "1" ]]; then
    echo "unexpected ${ROOTS}: ${broken_count} anchor lines, expected exactly 1" >&2
    exit 1
  fi
  backup="${ROOTS}.bak-$(date +%Y%m%d)"
  if [[ ! -e "${backup}" ]]; then
    cp -a "${ROOTS}" "${backup}"
    echo "backup: ${backup}"
  fi
  # Literal, single replacement through node — no regex quoting to get wrong.
  node -e '
const { readFileSync, writeFileSync } = require("node:fs")
const [file, broken, fixed] = process.argv.slice(1)
const text = readFileSync(file, "utf8")
if (!text.includes(broken)) {
  console.error("anchor line vanished before the rewrite")
  process.exit(1)
}
writeFileSync(file, text.replace(broken, fixed))
' "${ROOTS}" "${BROKEN}" "${FIXED}"
  echo "patched: ${ROOTS}"
elif grep -qF "${FIXED}" "${ROOTS}"; then
  echo "already patched: ${ROOTS}"
else
  echo "unexpected ${ROOTS}: neither the original nor the patched resolveAllowedPath line is present" >&2
  echo "this DevSpace build moved the code — read dist/roots.js and re-anchor the patch" >&2
  exit 1
fi

if ! probe; then
  echo "PROBE FAILED after patching ${ROOTS} — restore with --restore and inspect" >&2
  exit 1
fi
echo "probe ok: '~' expands to \$HOME, not to the workspace root"
echo "restart the connector to load it: systemctl --user restart devspace.service (startup ~20 s)"