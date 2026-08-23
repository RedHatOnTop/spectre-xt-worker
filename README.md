# Spectre XT agent worker

Lid-closed Debian appliance for the idle HP Spectre XT. Looks off, never
sleeps, runs ZCode + `hardened-zai-proxy` from today through Thursday.

## One-click (this is the whole install)

**1. Debian 13 netinst** (no desktop needed — the script installs XFCE):

https://cdimage.debian.org/debian-cd/current/amd64/iso-cd/debian-13.6.0-amd64-netinst.iso

Installer: hostname `spectre`, user `person`, tick **SSH server**, tick
**non-free firmware**. Skip the desktop environment.

**2. On first boot, as `person`:**

```bash
curl -fsSL https://raw.githubusercontent.com/RedHatOnTop/spectre-xt-worker/main/install.sh | sudo bash
```

Direct file: https://raw.githubusercontent.com/RedHatOnTop/spectre-xt-worker/main/install.sh

Repo: https://github.com/RedHatOnTop/spectre-xt-worker

**3. Reboot, then the only interactive bits:**

```bash
sudo tailscale up --ssh --hostname=spectre
sudo spectre-bind-cockpit
# copy proxy env.json into /work/hardened-zai-proxy by hand (keys)
systemctl --user enable --now glm-proxy.service
sudo spectre-stealth closed   # then close the lid
```

The lid HP logo does not light on this chassis. Remaining power/charge
LEDs are firmware. Leave them.

Full ops: [RUNBOOK.md](RUNBOOK.md).
