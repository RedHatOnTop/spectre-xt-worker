#!/bin/bash
# Pin Qoder CLI to Efficient (0.0x promo as of 2026-09-03).
# Spectre is 2C/4T: at most two qodercli processes. No cargo/kernel builds.
set -euo pipefail
exec "${HOME}/.local/bin/qodercli" --model efficient "$@"
