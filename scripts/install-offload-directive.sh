#!/bin/bash
# Install the "offload heavy work" directive into an agent's global memory file.
#
# The Spectre is an agent runtime, not a compile farm (AGENTS.md). That rule is
# enforced by spectre-thermal-guard (RUNBOOK 7.20) and the sanctioned alternative
# is spectre-offload (RUNBOOK 7.21). Every agent session on the box must carry
# that instruction, including agents working in other repositories, so it goes
# into the *global* memory files:
#
#   ~/.claude/CLAUDE.md   every Claude Code session
#   ~/.codex/AGENTS.md    every Codex session
#
# The block is marker-delimited and replaced in place, so this is idempotent and
# safe to re-run after an install script rewrites the file.
#
#   scripts/install-offload-directive.sh                      # default targets
#   scripts/install-offload-directive.sh ~/.claude/CLAUDE.md  # explicit
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TEXT="${OFFLOAD_DIRECTIVE:-$ROOT/config/offload-directive.md}"
BEGIN='<!-- spectre-offload:begin -->'
END='<!-- spectre-offload:end -->'

if [[ $# -gt 0 ]]; then
  targets=("$@")
else
  targets=("$HOME/.claude/CLAUDE.md" "$HOME/.codex/AGENTS.md")
fi
[[ -f "${TEXT}" ]] || { echo "directive text missing: ${TEXT}" >&2; exit 2; }

block="$(mktemp)"
trap 'rm -f "${block}"' EXIT
{
  printf '%s\n' "${BEGIN}"
  printf '%s\n' '<!-- Generated from config/offload-directive.md by scripts/install-offload-directive.sh; edit that file. -->'
  printf '\n'
  cat "${TEXT}"
  printf '%s\n' "${END}"
} >"${block}"

status=0
for target in "${targets[@]}"; do
  if [[ ! -f "${target}" ]]; then
    echo "skip  ${target} (not present)"
    status=1
    continue
  fi
  backup="${target}.bak-offload-$(date +%H%M%S)"
  cp -p "${target}" "${backup}"
  chmod 600 "${backup}" 2>/dev/null || true
  tmp="${target}.tmp"
  if grep -qF "${BEGIN}" "${target}"; then
    # Replace the existing block, keeping everything else byte for byte.
    env -u PYTHONHOME -u PYTHONPATH python3 - "$target" "$block" "$tmp" <<'PY'
import sys
src, block, out = sys.argv[1], sys.argv[2], sys.argv[3]
text = open(src, encoding='utf-8').read()
begin, end = '<!-- spectre-offload:begin -->', '<!-- spectre-offload:end -->'
start = text.index(begin)
stop = text.index(end, start) + len(end)
replacement = open(block, encoding='utf-8').read().rstrip('\n')
open(out, 'w', encoding='utf-8').write(text[:start] + replacement + text[stop:])
PY
    mv "${tmp}" "${target}"
    echo "update ${target} (backup $(basename "${backup}"))"
  else
    {
      printf '\n'
      cat "${block}"
    } >>"${target}"
    echo "append ${target} (backup $(basename "${backup}"))"
  fi
done

exit "${status}"
