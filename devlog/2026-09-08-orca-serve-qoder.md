# 2026-09-08 — Orca ADE serve + Qoder CLI replace the ZCode worker

## What changed on the box

- Installed `orca-ide` 1.4.198 (deb) plus headless deps (xvfb,
  build-essential, python3, Electron shared libs via apt).
- Systemd **user** unit `orca-serve.service`: headless
  `orca serve --port 6768 --pairing-address 100.119.252.88 --json`,
  linger on, Restart=on-failure, RestartPreventExitStatus=3.
- Systemd **system** unit `orca-port-guard.service` applies
  `/etc/nftables/orca-port-guard.nft`: port 6768 answers only from `lo`
  and `tailscale0`. Sources kept in `config/` in this repo.
- Installed Qoder CLI (`curl -fsSL https://qoder.com/install | bash`,
  script reviewed before running) → `~/.local/bin/qodercli`.
  Browser device-flow login done (account: RedHatOnTop, Pro plan, expires
  2026-10-02). Autoupdate pinned off in `~/.qoder/settings.json`.
- Kicked off session 1 of the orca-rust rewrite in tmux `qoder`
  (`qodercli -m Efficient --dangerously-skip-permissions`). First smoke
  reply on Efficient left Plan Credits at 0/2000 — the promo multiplier
  is effectively 0. Add-on credits were already 1791/2000 from IDE use.
- `health.env`: `REQUIRE_PROXY=false`, `REQUIRE_ZCODE=false`
  (proxy/zcode are no longer required services).
- Disabled (not removed) the always-dead `glm-proxy.service` user unit;
  doctor now warns instead of failing when it is off.

## Why

The headless zcode CLI never cleared Z.AI risk control (captcha 3007 on
every gateway call; device-flow login expired twice). The staged
orca-rust task had no runner. Qoder CLI on the Efficient tier is the
replacement worker, and Orca ADE `serve` provides desktop, browser, and
mobile control over the tailnet without keeping a GUI ZCode window
alive.

## The hang (root cause, bisected)

`orca serve` under the systemd user unit started, used ~7 s CPU, then
sat at 0 CPU forever without binding 6768 — while the exact same
command from an SSH shell was ready in ~30 s. Bisect matrix:

| probe | result |
|---|---|
| manual, bare | ready |
| manual, `</dev/null` stdin | ready |
| manual, `env -i` | ready |
| manual + ibus/IM vars | ready |
| manual + `DESKTOP_SESSION`/`XDG_CURRENT_DESKTOP`/`XDG_SESSION_TYPE` | **hang** |

Cause: the user manager exports the lightdm/XFCE autologin session
variables into every user unit; Electron sees them, attaches to the
(real, Xorg-on-panel) desktop session instead of starting its own Xvfb,
and stalls. Fix: `UnsetEnvironment=` of DISPLAY, WAYLAND_DISPLAY,
XAUTHORITY and the whole DESKTOP_SESSION/XDG_* session set in
`orca-serve.service`. After that the unit binds in ~10 s.

## Repo changes

- `scripts/doctor.sh`: new "orca serve + qoder (worker stack)" gates
  (unit active, :6768 listening + HTTP 200, port-guard service + rule,
  qoder tmux session, autoupdate pinned off); proxy and zcode gates are
  now conditional/soft.
- `config/orca-port-guard.nft`, `config/orca-port-guard.service`: box
  sources for the guard.
- `AGENTS.md`: role rewritten around the orca-serve control plane +
  Qoder worker; orca serve is the one allowed Electron exception.
- `RUNBOOK.md`: new §7.8 (worker stack), verification-gate additions.

Doctor on the box after migration: 25 PASS / 0 FAIL (after disabling the
dead glm-proxy unit).

## Unverified / follow-ups

- Orca pairing from a client was handed to the user as the web-client
  URL (tailnet only); desktop AppImage staged on fedora at
  `~/Applications/orca-linux.AppImage` but not launched there yet.
- Mobile pairing QR not minted (needs a one-off
  `orca serve --mobile-pairing` run).
- `healthcheck.py` has no orca-serve probe (doctor covers manual
  verification); candidate follow-up if unattended death alerts are
  wanted.
- Pre-existing (not from today): ssh still allows password auth and
  ufw/fail2ban were never applied — `scripts/harden-network.sh` remains
  unrun on this box.
