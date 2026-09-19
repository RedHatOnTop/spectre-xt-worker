# 2026-09-12 — Orca serve died quietly (exit 0); Restart=always fix

## Incident

- ~13:05 KST: Orca client shows connection fail. From fedora,
  `curl :6768` gets **connection refused in ~6 ms** — RST, so the box is
  up and nothing is listening. Confirmed alive: tailnet peer active,
  ping ok, `spectre-status` :9091 serving, tmux `qoder`/`pugc`/`work`
  all fine.
- `orca-serve.service`: `inactive (dead)` since **05:02:15 KST** — an
  8-hour silent death.

## Timeline (journald on the box)

- Sep 12 03:42:31 / 03:43:03 / 03:47:08 — three crash-restarts
  (status 1), restart counter 1..3. No OOM: kernel log and
  systemd-oomd are clean; the box was merely under load (load ~8,
  9.2/11 GiB used, 5× qodercli + two java processes).
- 03:47:08 start → ran 1 h 15 m → **05:02:15 exit status 0**. Clean
  self-exit: the `app-orca` scope and the service launcher both ended
  without error, no `Stopping`/SIGTERM in the journal, orca's own
  `~/.config/orca/logs/daemon.log` records nothing after 03:47:50.
- `Restart=on-failure` treats exit 0 as success → no restart. Unit
  stayed dead until noticed.

## Fix (box)

- `~/.config/systemd/user/orca-serve.service`: `Restart=on-failure` →
  `Restart=always` + `RestartSec=3`. Backup at
  `orca-serve.service.bak-20260912`. `RestartPreventExitStatus=3` kept
  (exit 3 = singleton conflict, must stay down). StartLimit 5/300 s
  unchanged.
- Verified on the Spectre: manual restart → ready in 27 s, `:6768`
  LISTEN, `web-index.html` 200. Then `systemctl --user kill
  orca-serve.service` → `activating (auto-restart)` → ready again in
  24 s. Same check from fedora: HTTP 200.
- Repo now carries the unit as `systemd/orca-serve.service` (was
  box-only); RUNBOOK §7.8 documents the always-restart rationale.

## Access: Tailscale SSH check re-auth

ssh to the box was blocked by Tailscale SSH check mode — a browser
re-approval is required every 24 h. Symptom: `ssh spectre` hangs after
printing a `login.tailscale.com/a/<token>` URL; approving that URL lets
the pending connection proceed. Documented in RUNBOOK §7 and AGENTS.md.

## Follow-ups

- Why the app self-exits (and the 03:42-03:47 crash loop) is not
  root-caused; no OOM, no external stop. Restart=always makes it
  self-healing; crash-loop protection remains.
- `qoder-efficient-guard.{service,timer}` and
  `qoder-nudge.{service,timer}` exist on the box (since Sep 9) but are
  not mirrored into this repo yet. The guard logged `free` all through
  the incident (not involved).
- `healthcheck.py` still has no orca-serve probe; an 8 h silent death
  argues for adding one.
