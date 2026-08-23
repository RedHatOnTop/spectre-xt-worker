# Spectre XT 24/7 agent worker

Target: HP Spectre XT TouchSmart (13-2000 series, 2012), i7-3517U
(2C/4T, 17 W), 12 GB, 256 GB mSATA + 128 GB SATA, battery replaced years ago.
Not an HP ZBook. Not the 15-inch ENVY Spectre XT. The machine named
`fedora` is the ASUS Zenbook Duo, which stays the daily driver.

The box is an agent runtime. LLM calls wait on the network; the CPU is the
bottleneck only when a session compiles. Budget **one ZCode window, at most
two sessions**. Do not build Zetile, do not run GNOME, do not run a second
Electron app.

**Window: now through Thursday 2026-08-27.** Install today. The Zenbook
proxy stays down for that whole stretch so the Spectre owns the key pool.

Lid closed is the operating position. Closing it must never suspend.
A glance at the desk must look like a closed, charging laptop — no panel
glow, no keyboard light. The lid HP logo does not light on this chassis.
Remaining firmware LEDs stay.

Main machine today: Fedora 44 Workstation (`fedora`, Tailscale
`100.64.11.53`). Phone: Galaxy Z Fold 7 (`z-fold7`, already in the same
tailnet). Proxy already exists as
`distribution-project/round-robin/hardened-zai-proxy` on `:18088`.

---

## 1. Hardware before the ISO

Do these with the machine off. Skipping paste on a 2012 ultrabook is how
the 24-hour window dies at 3 a.m.

1. **Repaste.** 2012 HP paste is dead. Clean the heat pipes, Arctic MX-4 or
   similar, even pressure on the i7-3517U + HD 4000 die.
2. **Elevate.** 2 cm stand or rubber feet so the intake is not on fabric.
   Lid stays closed after install.
3. **HDMI dummy plug** (~$5, 1080p EDID). Required, not optional. Electron
   41 needs a display; stealth turns the internal panel **off**. Without
   the dummy, ZCode dies when the lid closes. Dummy plugs emit no light.
4. **LEDs.** The lid HP logo stays dark on this chassis. Remaining
   power/charge pips are firmware — leave them.
5. **Always AC, charge cap 60%.** The replaced battery is a UPS. The
   one-click writes 50/60 into TLP and into
   `charge_control_end_threshold` if the kernel exposes it. 2012 HP EC
   often has no such node — then it sits at 100% on AC. If BIOS has
   Battery Health Manager / Conservation, turn that on too. After
   bootstrap: `cat /sys/class/power_supply/BAT*/charge_control_end_threshold`
   — `60` means it took; no such file means firmware will not cap.
6. **USB Ethernet** if you have a dongle. Onboard Wi-Fi is Intel Centrino
   Advanced-N 6235 (802.11n). Fine for API traffic, flaky with power
   save. Prefer wired for this window.
7. **BIOS:** disable Bluetooth, webcam, fingerprint if present, Rapid Start,
   Wake-on-LAN (unless you want it). Enable VT-x. Disable Secure Boot if
   the firmware even has it. Set lid-close to "Do nothing" if the option
   exists. Confirm the firmware is **64-bit UEFI** at the setup screen —
   a few 2012 ultrabooks shipped 32-bit UEFI; Debian 13 netinst will not
   boot that. If `ls /sys/firmware/efi/fw_platform_size` later prints
   `32`, stop and use a 32-bit GRUB image; do not improvise.

Ivy Bridge is x86-64-v2 (SSE4.2, AVX, no AVX2). Fedora 44 and Debian 13
both still install. Chromium/Electron 41 does not require AVX2.

---

## 2. OS: Debian 13 (Trixie) netinst + XFCE

Not Fedora Workstation. The Zenbook already runs Fedora 44 + GNOME; cloning
that onto 2 cores and HD 4000 wastes 2 GB RAM and a 6-month upgrade clock
on a machine whose job is "stay up".

| | Debian 13 + XFCE | Fedora 44 Workstation |
|---|---|---|
| Idle RAM | ~0.7 GB | ~2.5 GB |
| Upgrade clock | years | 6 months |
| Old Intel | default | works, heavier |
| Electron / Node | AppImage + Node 22 | already proven on the Zenbook |

Install from the official netinst ISO (skip the desktop — the one-click
script installs XFCE):

https://cdimage.debian.org/debian-cd/current/amd64/iso-cd/debian-13.6.0-amd64-netinst.iso

- Hostname: `spectre`
- Username: same `person` as the Zenbook (keeps systemd user unit paths)
- Desktop: **none**. Do not tick GNOME/KDE/XFCE in the installer.
- Disk (see §3). Separate `/home` is unnecessary; separate `/work` is.
- SSH server: yes. Non-free firmware: yes.
- No root password; sudo for `person`.

On first boot:

```bash
curl -fsSL https://raw.githubusercontent.com/RedHatOnTop/spectre-xt-worker/main/install.sh | sudo bash
```

If the installer cannot see both SSDs: the missing one is usually the
256 GB mSATA (BIOS Device Configuration). The 128 GB is SATA.

After first boot, do **not** enable GNOME later. If XFCE feels wrong,
`openbox` + `lightdm` is the fallback, not GNOME.

---

## 3. Disk layout — what you click in the installer

Two SSDs. Identify them by **size**, never by `sda`/`sdb`.

| What the installer shows | This is |
|---|---|
| ~238 GiB / 256 GB | mSATA. OS. Install Debian here. |
| ~119 GiB / 128 GB | SATA. Leave unused. The one-click script takes it. |

If only the 128 GB SATA appears, the 256 GB mSATA is still off in BIOS.

### Installer screens (Debian 13 netinst)

1. Reach **Partition disks**.
2. Choose **Guided - use entire disk**.
   Not LVM. Not encrypted. Not "manual" unless you know you need it.
3. **Select disk to partition** — the **256 GB mSATA** one only.
   The 128 GB SATA line stays untouched.
4. Scheme: **All files in one partition (recommended)**.
   That is ESP + `/`. No separate `/home`. Warp needs
   `/home/person/Projects/...` on this disk, same path as the Zenbook.
5. **Finish partitioning and write changes to disk** → **Yes**.
   This wipes the 256 GB mSATA. It must not mention writing to the 128 GB
   SATA disk. If it does, go back.

After first boot the one-click `setup-disks.sh`:

- refuses to touch the disk that holds `/`
- **if you already partitioned the 128 GB SATA, it adopts that layout
  and does not format.** Swap partitions get `swapon`. The largest
  ext4/xfs/btrfs on that disk is mounted at `/work` unless the installer
  already mounted it somewhere (`/home`, etc.) — then it is left alone.
- empty disk only → GPT, 8 GB swap, rest ext4 labelled `SPECTREWORK`

`/work` is swap, npm-cache, proxy logs, bulky clones. Projects stay on `/`.
ext4, not btrfs.

### If you already chose Manual

On the 256 GB disk only:

```
512 MB  ESP   fat32  /boot/efi  bootable
rest    ext4  /
```

128 GB SATA: leave as free space. Do not create `/home` or swap there in
the installer.

---

## 4. First-boot hardening (copy-paste)

From this repo, as `person`:

```bash
sudo bash scripts/bootstrap.sh
```

What it does, in order:

1. Installs Node 22 (NodeSource), `tailscale`, `cockpit`, `tlp`,
   `thermald`, `lm-sensors`, `intel-microcode`, `firmware-iwlwifi`,
   `acpid`, `upower`, `tmux`, `git`, `jq`, `curl`, `unattended-upgrades`.
   Purges `light-locker` and `xfce4-screensaver` (both will suspend on lid).
2. Drops in logind lid-ignore, `sleep.conf.d` AllowSuspend=no, sysctl,
   TLP quiet-AC, UPower `IgnoreLid=true`, XFCE lid-action=nothing.
3. Enables `lid-inhibit.service` (`systemd-inhibit` forever), `acpid`
   lid handler → `spectre-stealth closed`, lingering, autostart stealth.
4. Masks sleep/hibernate/hybrid-sleep targets.
5. Creates `/work/person` owned by `person`.

Then, **not** in the script (needs your account):

```bash
sudo tailscale up --ssh --accept-routes=false --hostname=spectre
# login URL → same tailnet as fedora / z-fold7
sudo hostnamectl set-hostname spectre
```

Verify:

```
tailscale status          # spectre + fedora + z-fold7
loginctl show-session $XDG_SESSION_ID -p Linger
systemctl status sleep.target hibernate.target   # masked
cat /sys/firmware/efi/fw_platform_size           # must be 64
sensors                                          # after sensors-detect
```

### 4.1 LightDM auto-login + linger

`/etc/lightdm/lightdm.conf.d/01-autologin.conf`:

```
[Seat:*]
autologin-user=person
autologin-user-timeout=0
```

User linger (already in bootstrap):

```
sudo loginctl enable-linger person
```

Bootstrap already writes XFCE power manager to "lid = do nothing" and
installs `~/.config/autostart/stealth-session.desktop`. Do not open
Settings → Power afterwards and pick Suspend. Screensaver and
light-locker are purged.

### 4.2 Unattended upgrades, no surprise reboot

Leave security updates on. Turn **automatic reboot off**. A 04:00 reboot
kills the window.

`/etc/apt/apt.conf.d/50unattended-upgrades` must contain:

```
Unattended-Upgrade::Automatic-Reboot "false";
```

### 4.3 Lid close is ignored, and the box looks off

Four independent layers. Any one failing must still leave the machine
awake. All four are installed by bootstrap.

| Layer | What it does |
|---|---|
| `systemd/sleep.conf.d/no-sleep.conf` | `AllowSuspend=no` and friends |
| masked `sleep.target` `suspend.target` `hibernate.target` | even `systemctl suspend` refuses |
| logind `HandleLidSwitch=ignore` + UPower `IgnoreLid=true` + XFCE lid-action 0 | no component treats lid as sleep |
| `lid-inhibit.service` | `systemd-inhibit --what=handle-lid-switch:sleep:idle` held forever |

On lid close, acpid does **not** sleep. It runs `spectre-stealth closed`:

- every `/sys/class/backlight/*/brightness` → 0
- every `/sys/class/leds/*/brightness` → 0 (keyboard, capslock, wifi)
- Master sink muted
- if HDMI dummy is connected: internal `eDP`/`LVDS` `--off` (no IPS glow through the lid). Dummy stays, so Electron keeps a display
- Linux console blanked

`stealth-blank.service` and `~/.config/autostart/stealth-session.desktop`
do the same after autologin, so the box is dark even before the first
lid event.

Power button still shuts down. That is the last on-chassis stop once
the panel is off.

**Looks-off check, 30 seconds:** dummy in, autologin done, `sudo spectre-stealth closed`, close the lid. From a metre away: no panel glow, no keyboard glow. Firmware power/charge pips may remain. Then from the Zenbook:

```
ping spectre
ssh person@spectre 'curl -fsS http://127.0.0.1:18088/health'
```

If either fails after the lid click, something still slept. `journalctl -b -u systemd-logind -u acpid -u lid-inhibit` and fix that before leaving the room. Do not "see how it goes overnight".

---

## 5. Proxy (Hardened) on the Spectre only

Copy `round-robin/hardened-zai-proxy` to `/work/hardened-zai-proxy`.
Do **not** rsync `node_modules`, `log-viewer/src-tauri/target`, or
`hardened-proxy.log`.

`env.json` / `.env` move by hand, never through git. Key loading order
is `REMOTE_CONFIG_URL` > encrypted bundle > `env.json`.

Install the user unit:

```bash
mkdir -p ~/.config/systemd/user
cp systemd/glm-proxy.service ~/.config/systemd/user/
# edit WorkingDirectory= if you did not use /work/hardened-zai-proxy
systemctl --user daemon-reload
systemctl --user enable --now glm-proxy.service
```

Bind it to Tailscale, not `0.0.0.0`. The unit already sets
`HOST=127.0.0.1`. Phone and Zenbook reach it via `ssh -L` or by running
ZCode **on the Spectre** (the intended path). Do not publish `:18088` to
LAN or CGNAT.

From the moment the Spectre proxy is up until Thursday, **stop the
Zenbook proxy** so both machines do not drain the same key pool:

```
# on fedora
systemctl --user stop glm-proxy.service
# if it was started from a terminal instead of systemd:
ss -tlnp | grep 18088
```

Health:

```
curl -sS http://127.0.0.1:18088/health | jq .
```

Expect `status=ok` and a non-zero `activeKeys`. If `activeKeys` is 0 the
window is already over for that pool.

---

## 6. ZCode on the Spectre

ZCode 3.7.7 is Electron 41. It has to keep a desktop window alive —
Remote Control and Bot Channel both drive that window, they are not a
headless server.

1. Install the official Linux build into `~/.local/share/zcode` the same
   way the Zenbook did. Do not copy `~/.zcode/v2/credentials.json` or
   `config.json` wholesale — they contain provider keys.
2. Autostart: copy `desktop/zcode-worker.desktop` to
   `~/.config/autostart/`.
3. In ZCode Settings, set these (also applied by
   `scripts/pin-zcode-settings.sh` against `~/.zcode/v2/setting.json`):

   | key | value | why |
   |---|---|---|
   | `keepAwakeWhileRunning` | `true` | otherwise the session sleeps |
   | `desktopChromiumHardwareAccelerationEnabled` | `false` | HD 4000 + Electron 41 |
   | `closeToTrayOnWindows` | `true` | if a close-to-tray flag exists on Linux, use it |
   | `autoDownloadAndInstallUpdates` | `false` | no mid-window Electron swap |
   | `zcodeInteractionBehavior` | `queue` | already your default |
   | model | **Hardened / `glm-5.3`** (or the one free model you actually want) | one model; the proxy rotates keys |

4. Add a custom OpenAI-compatible provider named `Hardened`:
   - base URL: `http://127.0.0.1:18088/v1`
   - dummy API key (the proxy does not auth localhost)
   - models: only the one you will pin. Do not list five free SKUs and
     let the agent hop.
5. Open the workspaces the phone will be allowed to see **before** you
   leave. Remote Control cannot browse arbitrary directories. Bot Channel
   can start tasks inside already-registered workspaces.
6. Bot Channel: Telegram. `~/.zcode/v2/bot-state.v2.json` is currently
   `bots: {}` on the Zenbook — it is not set up there either. Do it on
   the Spectre, once. This is the durable phone path. QR Remote Control
   is the visual path and expires.
7. Click the phone icon, generate Remote Control, scan once from the
   Fold 7 so the Z.ai relay device is stored. Then **do not** treat QR as
   the 24-hour control plane.

Software rendering will cost CPU. That is acceptable: the agent is
network-bound. Hardware accel on HD 4000 is how Electron 41 wedges.

### 6.1 What the phone can and cannot do

From ZCode's own Remote Control docs:

- Phone is a control surface. It never runs commands itself.
- Only one phone page at a time.
- Phone cannot create a new SSH/Docker workspace. It can reconnect one
  the desktop already registered.
- Phone cannot open a folder the desktop window has not opened.
- Closing the QR dialog does **not** stop Remote Control. Press Stop.
- Anyone with the link operates the window. Do not forward it. Refresh
  the QR if it leaked.

That is why Telegram Bot Channel + Tailscale SSH exist. Remote Control
alone is not "perfect control".

---

## 7. Mobile control (Fold 7) — what is actually reliable

ZCode Remote Control is a bonus view. It is **not** the control plane.
It depends on Z.ai's relay, a live Electron window, one phone page at a
time, and a QR/link that is a capability token. When Z.ai blips, the
phone goes blind while the box is still up.

The reliable path does not go through Z.ai.

| Rank | What | Survives Z.ai outage | Survives lid closed |
|---|---|---|---|
| 1 | Tailscale + Termius → `tmux attach -t work` | yes | yes |
| 2 | Cockpit `https://spectre.tail1fa7c9.ts.net:9090` | yes | yes |
| 3 | ntfy push from `worker-health.timer` | yes | yes |
| 4 | ZCode Telegram Bot Channel | no (ZCode + Z.ai) | yes if ZCode is up |
| 5 | ZCode Remote Control QR | no | yes if ZCode is up |

Fold 7 is already in the tailnet as `z-fold7`. After

```
sudo tailscale up --ssh --hostname=spectre
sudo spectre-bind-cockpit
spectre-pull-keys fedora
```

Termius:

- host: `spectre` or `spectre.tail1fa7c9.ts.net`
- user: `person`
- remote command: `tmux attach -t work || tmux new -s work`

SSH keepalive is 15 s. `mosh person@spectre` is there for subway Wi-Fi.
On login, `spectre-status` prints proxy/zcode/temp/battery.

Also on the Zenbook: `sudo tailscale set --ssh` so Warp can move
without extra keys.

Telegram Bot Channel is the durable **agent** surface (start a task at
01:00). Pair once in ZCode → phone icon → Bot Channel. QR Remote is
only for looking at a live session.

Cockpit: reboot, journal, units, disk. Self-signed cert warning is
expected. Bind (already in bootstrap if Tailscale is up):

```
sudo mkdir -p /etc/systemd/system/cockpit.socket.d
sudo cp config/cockpit.socket.d/override.conf.example \
        /etc/systemd/system/cockpit.socket.d/override.conf
# put `tailscale ip -4` in ListenStream=
sudo systemctl daemon-reload
sudo systemctl restart cockpit.socket
```

RustDesk / VNC: no. HD 4000 plus H.264 encode will melt the 17 W part
and buy you nothing Bot Channel does not.

---

## 7.5 Warp (Zenbook ↔ Spectre)

`warp` sends the current project and its ZCode sessions to the other
machine at the **same absolute path**. That is why Projects stay under
`/home/person/Projects` on the Spectre instead of `/work`.

On the Zenbook, once:

```bash
bash scripts/install-warp.sh
```

```bash
cd /home/person/Projects/whatever
warp to spectre          # this machine → Spectre
warp from spectre        # Spectre → this machine
```

On the Spectre the inverse is `warp to fedora` / `warp from fedora`.

`target/`, `node_modules/`, `.next/`, and the rest of
`config/warp-excludes.txt` stay behind. Uncommitted files go; this is
the point.

Close ZCode on the **destination** first. The source can stay open —
sessions are snapshotted from WAL. If the dest still has ZCode open,
files still land and you run `warp apply ~/.cache/spectre-warp/in.*/sessions.db`
after quitting.

`warp status` shows last direction, peer, and path.

---

## 8. Monitoring

User timer every 60 s, `scripts/healthcheck.sh`:

- `curl` `:18088/health` — fail if not `ok` or `activeKeys==0`
- `pgrep -f '/zcode'` — fail if the Electron process is gone
- `sensors` — fail if package temp ≥ 85 °C
- disk — fail if `/` or `/work` ≥ 90 %
- `tailscale status --json` — fail if backend not Running

On fail: append one line to `/work/logs/health.log` and POST to ntfy
(`NTFY_TOPIC` in `~/.config/remote-agent/health.env`, never committed).

Install (bootstrap already put the script at
`/usr/local/bin/spectre-healthcheck`):

```bash
mkdir -p ~/.config/remote-agent /work/logs
cp config/health.env.example ~/.config/remote-agent/health.env
# fill NTFY_TOPIC
cp systemd/worker-health.service systemd/worker-health.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now worker-health.timer
```

On the Fold 7, subscribe to the same ntfy topic. You want a push when
the proxy dies, not a dashboard you forget to open.

Optional live view from the Zenbook, not the phone:

```
node /work/hardened-zai-proxy/stats-tui.js http://127.0.0.1:18088/health 2
```

That TUI is SSH-only. Do not expose it.

---

## 9. Same-day protocol (today → Thursday)

The window is already open. Debian install today, stealth+proxy today,
lid-closed today. Do not save a dress rehearsal for Wednesday.

1. Dummy HDMI in, AC, elevate. BIOS lid = do nothing.
2. Debian 13 netinst (SSH, no desktop). Reboot. Then:
   `curl -fsSL https://raw.githubusercontent.com/RedHatOnTop/spectre-xt-worker/main/install.sh | sudo bash`
3. `sudo tailscale up --ssh --hostname=spectre`.
4. Copy `hardened-zai-proxy` into `/work/hardened-zai-proxy`, enable
   `glm-proxy.service`, pin ZCode, Bot Channel, ntfy.
5. **Stop the Zenbook proxy now.** It stays down until Thursday.
6. `sudo spectre-stealth closed`, close the lid. Ping + health from the
   Fold 7. Looks-off check from a metre away.
7. Telegram test prompt. ntfy: stop proxy 10 s, confirm push, start it.
8. Walk away. Lid stays closed until Thursday.

TLP already caps `CPU_MAX_PERF_ON_AC=55` so the fan stays off under
agent-idle. If package temp still sits above 80 °C, drop that value,
do not raise it.

`intel-undervolt` is optional and off by default. If you undervolt,
start at `-70 mV` on the CPU, reboot, `stress-ng -c 4 -t 120` **before**
the lid closes for the week, not after.

---

## 10. Capacity (so the box is not asked for the wrong thing)

| Resource | Reality |
|---|---|
| CPU | 2C/4T @ 17 W. Agent I/O is fine. `cargo build -j2` of Zetile is not. |
| RAM | 12 GB. XFCE + ZCode + proxy + one session ≈ 4–6 GB. Two sessions if they stay idle. |
| Disk | 256 GB mSATA + 128 GB SATA. Clone only the repo being worked. |
| Net | 6235 N-Wi-Fi or USB Ethernet. API traffic is tiny. |
| Concurrency | 1 ZCode window. Queue (`zcodeInteractionBehavior=queue`), do not fan out. |

---

## Verification gates (must be run on the Spectre)

```
# firmware
cat /sys/firmware/efi/fw_platform_size          # 64

# sleep is dead
systemctl status sleep.target                   # masked
systemctl is-active lid-inhibit.service         # active
systemd-inhibit --list                          # spectre-worker AND ZCode
grep IgnoreLid /etc/UPower/UPower.conf          # IgnoreLid=true

# charge cap — 60 if the EC allows it, missing file if not
cat /sys/class/power_supply/BAT*/charge_control_end_threshold 2>/dev/null || echo 'no threshold sysfs'

# looks off (run, then close the lid, then ping from fedora)
sudo spectre-stealth closed
# from fedora, after lid close:
#   ping spectre && ssh person@spectre 'curl -fsS http://127.0.0.1:18088/health'

# proxy
curl -fsS http://127.0.0.1:18088/health | jq -e '.status=="ok" and .activeKeys>0'

# zcode
pgrep -a zcode | head
# setting.json
jq '.keepAwakeWhileRunning, .desktopChromiumHardwareAccelerationEnabled' ~/.zcode/v2/setting.json
# expect: true, false

# tailscale
tailscale status | grep -E 'spectre|z-fold7|fedora'

# health timer
systemctl --user is-active worker-health.timer
```

Until those commands have been run on the Spectre, this box is a plan,
not a worker.
