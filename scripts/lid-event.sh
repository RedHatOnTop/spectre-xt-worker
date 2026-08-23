#!/bin/bash
# acpid hands us: button/lid LID close|open
set -u
LOG="${STEALTH_LOG:-/work/logs/stealth.log}"
install -d -m 0755 "$(dirname "${LOG}")" 2>/dev/null || true
printf '%s lid-event args=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >>"${LOG}" 2>/dev/null || true

line="$*"
case "${line}" in
  *open*)  exec /usr/local/bin/spectre-stealth open ;;
  *)       exec /usr/local/bin/spectre-stealth closed ;;
esac
