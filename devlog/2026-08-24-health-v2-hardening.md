# 2026-08-24 — healthcheck v2, network hardening, doctor

## What was attempted

Close the gap between the agreed worker-box plan (ufw, key-only SSH,
journald caps, logrotate, smartd) and what bootstrap actually did, plus
fix weaknesses found in the existing scripts.

## What landed

- `scripts/harden-network.sh`: ufw default-deny + tailscale allowances +
  key-only sshd + fail2ban. Refuses to run before `authorized_keys` has
  a key, so the lockout window (password off, no key on disk) cannot
  happen. Not wired into bootstrap on purpose; README/RUNBOOK sequence
  is: tailscale up -> pull keys -> harden.
- `scripts/healthcheck.py` replaces `scripts/healthcheck.sh` as
  `spectre-healthcheck`. New probes: MemAvailable floor, swap %, tmux
  `work` session presence (phone control plane), AC offline. Notification
  state machine extracted into pure functions (`decide`, `message_for`)
  and unit-tested in `tests/test_healthcheck.py` (16 tests): new failure
  pushes immediately, changed composition re-pushes but keeps streak
  start, unchanged set re-pushes after RENOTIFY_MIN (default 30 min) —
  the old script stayed silent forever on an unchanged failure, which
  meant a missed ntfy push was invisible until recovery.
- Heartbeat file touched every passing tick
  (`~/.local/state/remote-agent/heartbeat`); `spectre-status` prints its
  age. A frozen heartbeat + silent ntfy = box is dark (power loss),
  distinguishable from "healthy".
- Log caps: journald SystemMaxUse=200M via `config/journald-caps.conf`,
  `/work/logs/*.log` weekly logrotate via `config/logrotate-work.conf`
  (the proxy log outgrew its disk once before), smartd enabled.
- `scripts/doctor.sh` as `spectre-doctor`: one pass over the whole
  RUNBOOK verification-gate list, PASS/FAIL/WARN per gate, non-zero exit
  on any FAIL. WARN for gates that need human eyes or optional hardware
  (looks-off check, missing charge-threshold EC node).
- `warp.sh` ssh_peer now prefixes PATH with ~/.local/bin on remote
  commands — user installs of warp live there, and Debian non-login ssh
  shells do not include it, so `warp apply` / `warp export-bundle` could
  fail with "command not found" depending on which machine sent.
- `verify.sh` single gate entry point (bash -n all scripts, shellcheck
  at style level clean, py_compile, unittest must report OK — a zero-test
  pass fails). `.github/workflows/verify.yml` runs it on push.

## What failed / lessons

- First healthcheck.py draft had a typo'd helper mid-refactor; caught by
  py_compile before any test ran — compile-first ordering pays.
- Test fixtures initially used physically wrong meminfo values (654 MB
  free on a 12 GB box tripping the 800 MB floor while the test asserted
  healthy). Failures were the parser being right; fixed the fixtures,
  not the code.
- shellcheck SC2015/SC1091 remain as inline-disabled false positives
  (`pwd || true` idiom, sourcing /etc/os-release); documented rather
  than silenced blanket-style.

## Decisions

- State machine in Python, not bash: the notification rules (streaks,
  windows, recovery) are exactly the logic that silently rots when only
  testable by hand. bootstrap already depends on python3.
- `scripts/healthcheck.sh` is superseded by `healthcheck.py`; bootstrap
  installs the Python version as `spectre-healthcheck`, so installed
  boxes pick it up on re-run without unit changes.
- harden-network left out of bootstrap deliberately; the safe order
  requires interactive Tailscale auth in the middle.
