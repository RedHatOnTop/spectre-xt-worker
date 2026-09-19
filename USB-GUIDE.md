# Spectre XT worker — USB install guide

This directory (`spectre-xt-worker/`) is a full snapshot of the worker
tooling. No GitHub needed. Target machine: HP Spectre XT TouchSmart,
fresh Debian 13 (Trixie) netinst, SSH server on, no desktop selected.

## On the Spectre

Plug the USB in. Find the mount point (`lsblk` — it appears under
`/run/media/person/...`; quote the path, it has spaces). Then:

    cd "/run/media/person/Sandisk 64G/spectre-xt-worker"
    sudo bash install.sh

`install.sh` detects the repo sitting next to it and runs bootstrap
from the USB directly — no copy to disk, no GitHub. Bootstrap does:
apt packages, lid/sleep lockdown, /work disk setup, systemd units,
ZCode download (needs internet for apt + the ZCode CDN), warp install.

Reboot when it finishes.

## After reboot — the wizard

    cd "/run/media/person/Sandisk 64G/spectre-xt-worker"
    sudo bash scripts/setup-wizard.sh

Ten interactive stages, in order, each printed with instructions:

    1  bootstrap        (skipped — already ran)
    2  tailscale        prints a login URL; open it on any tailnet device
    3  keys             pulls your pubkeys from fedora (passwordless SSH)
    4  harden           ufw default-deny + key-only SSH + fail2ban
    5  cockpit          binds the web UI to loopback + tailscale IP
    6  proxy            rsyncs hardened-zai-proxy from fedora into /work
                         -> it pauses: hand-copy env.json (API keys)
                         -> then enables glm-proxy and health-checks it
    7  health env       asks for your ntfy topic, enables the 60s timer
    8  ZCode pin        start ZCode once, add the Hardened provider
                         (http://127.0.0.1:18088/v1), then it pins settings
    9  stealth test     blanks the panel; close the lid; verify from
                         fedora (ping + health) while it is closed
    10 verify           runs spectre-doctor over every gate

Re-run the wizard any time — finished stages are skipped. A failed
stage does not mean starting over.

## After the wizard

Both boxes run the same single model against the shared pool — nothing
to stop. (If rate limits ever hit both machines at once, give the pool
to the Spectre: on fedora run `systemctl --user stop glm-proxy.service`.)

`warp` is already installed on both machines. Projects live at the same
absolute path on both, so from the project directory:

    warp push     # this machine -> the other one
    warp pull     # the other one -> this machine

Repo files and ZCode chat history move together. ZCode can stay open on
the receiving side — the session import waits for it to quit (up to
10 min) and applies itself. Nothing else to remember.

## Chat-history sync when the other machine is off (optional)

`session-sync` mirrors chat sessions through a private GitHub repo:

    session-sync push     # snapshot this project's sessions
    session-sync pull     # import from the repo — always creates NEW
                          # sessions, never overwrites existing ones

Setup: copy `config/session-sync.env.example` to
`~/.config/remote-agent/session-sync.env`, set SESSION_SYNC_REPO to a
private repo you control. The repo must stay private — it holds
transcripts.

## Agent git policy on the box

The irreversible-guard runs in "box" profile here. Allowed without
asking: commit, push, branch creation, PR create/review. Never allowed:
merge, rebase, branch/tag deletion, force push, history rewriting,
closing or deleting PRs/issues/releases. Those stay human actions.

## About the dark screen (this is normal)

After bootstrap, XFCE starts and immediately blanks itself: internal
panel off, LEDs off. That is the stealth feature doing its job — the
box must look powered-off on the desk.

- To see the screen again: `sudo spectre-stealth open` over SSH.
  It goes dark again on reboot or lid close — by design.
- If a real monitor is plugged into HDMI instead of a dummy plug, it
  stays ON when the lid closes (killing the last display output can
  wedge ZCode/Electron). To blank it too, run once as root:
      echo 'STEALTH_EXTERNAL=dpms' >> /etc/default/spectre-stealth
  The monitor then turns off with the lid via DPMS while the output
  stays alive underneath. For the permanent setup, replace the monitor
  with an HDMI dummy plug and everything is dark with no extra config.
- Sanity check from another machine: `ssh person@<spectre-ip>` works,
  `systemctl is-active lid-inhibit.service` prints active.

## Full operations manual

`spectre-xt-worker/RUNBOOK.md` — disk layout, verification gates,
capacity limits, mobile control paths. `spectre-xt-worker/verify.sh`
re-runs the repo's own quality gates.
