#!/bin/bash
# Install `warp` on this machine (fedora or spectre). No secrets. Idempotent.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
if [[ ${EUID} -eq 0 ]]; then
  LIB=/usr/local/lib/spectre-warp
  BIN=/usr/local/bin
else
  LIB="${HOME}/.local/lib/spectre-warp"
  BIN="${HOME}/.local/bin"
fi

install -d -m 0755 "${LIB}" "${BIN}"
install -m 0755 "${HERE}/warp.sh" "${LIB}/warp.sh"
install -m 0644 "${HERE}/warp_zcode.py" "${LIB}/warp_zcode.py"
if [[ -f "${HERE}/../config/warp-excludes.txt" ]]; then
  install -m 0644 "${HERE}/../config/warp-excludes.txt" "${LIB}/warp-excludes.txt"
fi
ln -sfn "${LIB}/warp.sh" "${BIN}/warp"
ln -sfn "${LIB}/warp.sh" "${BIN}/spectre-warp"
# Codex session handoff rides along: same peer, same paths (RUNBOOK 7.15).
# The rollout helper is installed beside the script, which is where
# codex-handoff.sh looks first.
if [[ -f "${HERE}/codex-handoff.sh" && -f "${HERE}/codex_rollout.py" ]]; then
  install -m 0755 "${HERE}/codex-handoff.sh" "${LIB}/codex-handoff.sh"
  install -m 0644 "${HERE}/codex_rollout.py" "${LIB}/codex_rollout.py"
  ln -sfn "${LIB}/codex-handoff.sh" "${BIN}/codex-handoff"
  echo "codex-handoff installed: ${BIN}/codex-handoff"
fi
echo "warp installed: ${BIN}/warp"
echo "try: warp status"
echo "     warp to spectre"
echo "     warp from spectre"
echo "     warp to fedora"
echo "     warp from fedora"
echo "     codex-handoff push     # hand a Codex session to the other machine"
