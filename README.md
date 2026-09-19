# Spectre XT TouchSmart agent worker

Lid-closed Debian appliance for the idle **HP Spectre XT TouchSmart**
(13-2000, 2012, i7-3517U, 12 GB, 256 GB mSATA + 128 GB SATA). Not a ZBook. Not the
15-inch ENVY Spectre XT. The daily driver remains the ASUS Zenbook Duo
(`fedora`).

## One-click

**1. Debian 13 netinst** — at Partition disks pick **Guided - use entire
disk**, then the **256 GB / ~238 GiB** disk, scheme **all files in one
partition**. Leave the 128 GB SATA unused. SSH on, no desktop. Charge cap
is 60% if the EC exposes a threshold (2012 HP often does not).

https://cdimage.debian.org/debian-cd/current/amd64/iso-cd/debian-13.6.0-amd64-netinst.iso

**2. First boot:**

```bash
curl -fsSL https://raw.githubusercontent.com/RedHatOnTop/spectre-xt-worker/main/install.sh | sudo bash
```

Direct file: https://raw.githubusercontent.com/RedHatOnTop/spectre-xt-worker/main/install.sh

The script formats the unused 128 GB SATA disk as 8 GB swap + `/work`,
installs XFCE, ignores the lid, blanks the panel, and puts `warp` on
`$PATH`.

**3. After reboot — one wizard to full operation:**

```bash
sudo bash /opt/spectre-xt-worker/scripts/setup-wizard.sh
```

The wizard walks every stage in order and skips what is already done,
so it is safe to re-run after a failed stage: bootstrap (if not done via
the one-click), tailscale login, key pull from fedora, ufw + key-only
SSH hardening, cockpit bind, proxy pull from fedora + unit enable,
ntfy health env + timer, ZCode pin, the stealth/lid looks-off check,
and a final `spectre-doctor` pass. It prints exactly what to do at each
interactive step (login URL, env.json hand-copy, lid-close check).

Prefer manual steps? The same order works one command at a time:

```bash
sudo tailscale up --ssh --hostname=spectre
sudo spectre-pull-keys fedora
sudo bash scripts/harden-network.sh   # ufw + key-only ssh (needs keys first)
sudo spectre-bind-cockpit
# copy hardened-zai-proxy into /work/hardened-zai-proxy (no node_modules)
#   + env.json by hand, then:
systemctl --user enable --now glm-proxy.service
cp config/health.env.example ~/.config/remote-agent/health.env  # fill NTFY_TOPIC
# start ZCode once, add the Hardened provider, then:
spectre-pin-zcode
sudo spectre-stealth closed && close the lid
```

**4. Verify in one pass:** `sudo spectre-doctor` — runs every gate from
RUNBOOK.md and fails loudly on any miss. `spectre-status` (or
`spectre-status --json`) shows the same facts on one screen.

Every agent on the box can browse: obscura (headless browser, no
Chromium) gives them a CLI, an MCP server and a CDP endpoint on
`127.0.0.1:9222` for Playwright/Puppeteer scripts — RUNBOOK §7.12.

Agents also get `devcodex` — durable task sessions, bounded code
navigation, workspace-scoped writes and commands, and evidence-backed
completion, runnable from any repo — RUNBOOK §7.13.

Worker occupancy is being centralized in `spectre-state` (UDS JSON API +
SQLite journal, RUNBOOK §7.17). The daemon is **shadow-only**: Slack
`/goal` still uses `qoder-goal-watch --probe`. Do not treat the new
snapshot as live dispatch authority until cutover.

A Codex CLI session can be handed to the other machine and picked up there:
`codex-handoff push` from the project directory, then `codex resume <uuid>`
where it landed (`pull` brings one back) — RUNBOOK §7.15.

Phone control that still works when Z.ai is down: Tailscale on the
Fold 7 → Termius → `tmux attach -t work`. Cockpit at
`https://spectre.tail1fa7c9.ts.net:9090`. ZCode Remote is a view, not
the control plane. Telegram Bot Channel is the durable agent surface.

On the Zenbook, once:

```bash
git clone https://github.com/RedHatOnTop/spectre-xt-worker.git
bash spectre-xt-worker/scripts/install-warp.sh
```

## Warp

Same absolute path on both machines. From the project you are in:

```bash
warp push     # this machine -> the other one
warp pull     # the other one -> this machine
```

No peer names, no direction juggling: each box knows the other. Repo
files and ZCode sessions for that directory move together; if ZCode is
still open on the receiving side, the session import waits in the
background for it to quit (10 min) and applies itself.

Full ops: [RUNBOOK.md](RUNBOOK.md).
