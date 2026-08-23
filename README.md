# Spectre XT TouchSmart agent worker

Lid-closed Debian appliance for the idle **HP Spectre XT TouchSmart**
(13-2000, 2012, i7-3517U, 12 GB, 256 GB + 120 GB). Not a ZBook. Not the
15-inch ENVY Spectre XT. The daily driver remains the ASUS Zenbook Duo
(`fedora`).

## One-click

**1. Debian 13 netinst** — at Partition disks pick **Guided - use entire
disk**, then the **256 GB / ~238 GiB** disk, scheme **all files in one
partition**. Leave the 120 GB unused. SSH on, no desktop. Charge cap
is 60% if the EC exposes a threshold (2012 HP often does not).

https://cdimage.debian.org/debian-cd/current/amd64/iso-cd/debian-13.6.0-amd64-netinst.iso

**2. First boot:**

```bash
curl -fsSL https://raw.githubusercontent.com/RedHatOnTop/spectre-xt-worker/main/install.sh | sudo bash
```

Direct file: https://raw.githubusercontent.com/RedHatOnTop/spectre-xt-worker/main/install.sh

The script formats the unused 120 GB disk as 8 GB swap + `/work`,
installs XFCE, ignores the lid, blanks the panel, and puts `warp` on
`$PATH`.

**3. After reboot:**

```bash
sudo tailscale up --ssh --hostname=spectre
sudo spectre-bind-cockpit
```

On the Zenbook, once:

```bash
git clone https://github.com/RedHatOnTop/spectre-xt-worker.git
bash spectre-xt-worker/scripts/install-warp.sh
```

## Warp

Same absolute path on both machines. From the project you are in:

```bash
warp to spectre      # Zenbook → Spectre
warp from spectre    # Spectre → Zenbook
warp to fedora       # Spectre → Zenbook
warp from fedora     # Zenbook → Spectre
```

Sends the tree (minus `target/`, `node_modules/`, …) and the ZCode
sessions for that directory. Close ZCode on the machine that is
**receiving** before the session import.

Full ops: [RUNBOOK.md](RUNBOOK.md).
