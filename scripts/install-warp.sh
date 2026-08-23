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
echo "warp installed: ${BIN}/warp"
echo "try: warp status"
echo "     warp to spectre"
echo "     warp from spectre"
echo "     warp to fedora"
echo "     warp from fedora"
