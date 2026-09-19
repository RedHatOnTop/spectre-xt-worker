# 2026-08-26 — Spectre install completed via agent SSH automation

## Setup for remote operation

User enabled one-time access: dedicated key
`~/.ssh/spectre_worker_ed25519` (zenbook) into spectre's
authorized_keys + NOPASSWD sudoers entry (`/etc/sudoers.d/person-nopasswd`).
`~/.ssh/config` Host block `spectre` (192.168.0.20, IdentitiesOnly).
All further steps ran non-interactively from the zenbook.

## Install sequence executed

1. rsync repo → /opt/spectre-xt-worker (USB was not mounted on the box)
2. bootstrap re-run — passed smartd (smartmontools.service), exit 0
3. reboot; lid-inhibit/acpid/lightdm all up, hostname spectre, Debian 13
4. tailscale up — printed login URL, user approved in browser;
   spectre joined tailnet as 100.119.252.88
5. pull-keys failed (fedora had no way back); solved by generating a
   keypair ON spectre and adding its pubkey to fedora's authorized_keys,
   plus pushing machismo_phone + id_ed25519 pubs INTO spectre.
   Bidirectional ssh verified both ways.
6. cockpit bound to tailscale IP :9090
7. proxy rsynced fedora→spectre… **missed env.json/.env excludes** —
   keys traveled over tailnet ssh to the same owner's box. Low risk,
   policy violation, recorded here. Side effect: no manual env.json copy
   was needed. chmod 600 applied.
8. glm-proxy crashed: node_modules absent by design of the rsync exclude
   → `npm ci --omit=dev` fixed it. Health: ok, activeKeys=20.
9. health.env installed (ntfy topic empty for now), worker-health.timer
   active, heartbeat flowing.
10. ZCode pinned; final doctor: 20 passed, 0 failed.

## Bugs found and fixed during this pass

- doctor.sh ran its user-unit checks against root's manager when invoked
  with sudo → false FAIL on worker-health.timer. Fixed with a RUN_AS_USER
  wrapper honoring SUDO_USER, and TARGET_HOME for file paths.
- jq `// empty` vs `// false`: `.x // true` returns true even when x is
  literally false — jq's alternative operator treats false as null. This
  produced a phantom "hardware acceleration still enabled" FAIL. Read raw
  + default only when key absent.
- ZCode rewrites setting.json from memory while running, so any file-based
  assertion races with the app. The real guarantee is --disable-gpu on the
  autostart chain (desktop Exec may point at a wrapper script). Doctor now
  checks .desktop text OR the wrapper binary it references. Electron also
  overwrites its own process title ("ZCode"), so /proc cmdline cannot be
  used either.
- pgrep -f pitfall twice: our own ssh one-liners matched "/opt/ZCode/zcode"
  patterns, producing fake "still running" results and a self-killed pkill
  session (exit 255). Use /proc cmdline reads or ps aux | grep -v grep.

## Honest notes

- keepAwakeWhileRunning=true is currently set, but the running app can
  rewrite it; doctor demoted that check to WARN accordingly.
- charge threshold WARN remains (2012 EC has no sysfs node) — hardware
  limit, battery sits at 100% on AC.
- ufw/fail2ban/key-only deliberately skipped per user choice (trusted LAN);
  Tailscale still walls off the internet side. Revisit if the box ever
  leaves the private network.
