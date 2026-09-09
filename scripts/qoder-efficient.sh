#!/bin/bash
# Pin Qoder CLI to Efficient (0.0x promo as of 2026-09-03).
# Spectre is 2C/4T: at most two qodercli processes. No cargo/kernel builds.
# Refuse to start if Efficient is billing or the kill-switch sentinel is set.
set -euo pipefail
GUARD="${HOME}/.local/bin/qoder-efficient-guard"
if [[ ! -x "${GUARD}" ]]; then
  echo "qoder-efficient: kill-switch missing; refusing to start" >&2
  exit 75
fi
"${GUARD}" allow-start
exec "${HOME}/.local/bin/qodercli" --model efficient "$@"
