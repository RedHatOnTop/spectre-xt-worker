#!/bin/bash
# Reset a terminal that a dead TUI left in mouse-report / alt-screen mode.
#
# Symptom this fixes: a tab whose shell prompt is flooded with text like
# `35;96;1M35;92;1M…`. A full-screen app (Claude Code, qodercli, an editor)
# enables SGR mouse reporting (`?1003`/`?1006`) and usually turns it off on exit.
# When the app is SIGKILLed — a power loss, `terminal close`, the reaper — the
# terminal keeps reporting, and every mouse movement over the tab is typed into
# the shell as `ESC[<b;x;yM`, minus the leading `ESC[<`.
#
# The disabling sequence must be written by the *application* (the shell) as
# output, so this sends Ctrl-C to clear the polluted input line and then runs a
# printf that turns the modes off, fixes the tty and clears the screen.
#
# Only use it on a terminal that is sitting at a shell prompt: --interrupt would
# also interrupt a live TUI's current turn.
#
#   spectre-term-reset <handle> [<handle> ...]
set -euo pipefail

ORCA="${ORCA_IDE:-orca-ide}"
if [[ $# -eq 0 ]]; then
  echo "usage: spectre-term-reset <terminal-handle> [<handle> ...]" >&2
  exit 2
fi

# shellcheck disable=SC2016  # the printf is evaluated by the *remote* shell
RESET_LINE='printf "\033[?1000l\033[?1002l\033[?1003l\033[?1004l\033[?1006l\033[?1049l\033[?25h"; stty sane 2>/dev/null; tput sgr0 2>/dev/null; clear'

rc=0
for handle in "$@"; do
  if ! "${ORCA}" terminal send --terminal "${handle}" --interrupt >/dev/null 2>&1; then
    echo "spectre-term-reset: ${handle}: interrupt failed (dead tab?)" >&2
  fi
  sleep 0.3
  if "${ORCA}" terminal send --terminal "${handle}" --text "${RESET_LINE}" --enter >/dev/null 2>&1; then
    echo "reset ${handle}"
  else
    echo "spectre-term-reset: ${handle}: reset line failed" >&2
    rc=1
  fi
done
exit "${rc}"
