#!/bin/bash
# Debian 13 first-boot for the Spectre XT worker. Idempotent enough to re-run.
# Does not touch secrets, Tailscale login, or ZCode credentials.
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "run as root: sudo bash scripts/bootstrap.sh" >&2
  exit 1
fi

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PERSON_USER="${SUDO_USER:-person}"

export DEBIAN_FRONTEND=noninteractive

# shellcheck disable=SC1091  # os-release exists on every Debian target
. /etc/os-release
if [[ "${ID}" != "debian" ]]; then
  echo "this script is for Debian 13. OS_ID=${ID}" >&2
  exit 1
fi

for src in /etc/apt/sources.list.d/debian.sources /etc/apt/sources.list.d/*.sources; do
  [[ -f "${src}" ]] || continue
  sed -i -E 's/^Components: .*/Components: main contrib non-free non-free-firmware/' "${src}"
done
if [[ -f /etc/apt/sources.list ]]; then
  sed -i -E '/^deb / { /non-free-firmware/! s/ main([[:space:]]|$)/ main contrib non-free non-free-firmware / }' /etc/apt/sources.list
fi

apt-get update
apt-get install -y \
  ca-certificates curl gnupg jq git tmux \
  tlp thermald lm-sensors \
  cockpit \
  unattended-upgrades apt-listchanges \
  acpid upower alsa-utils x11-xserver-utils iw \
  xfce4 lightdm lightdm-gtk-greeter openssh-server dbus-x11 sudo \
  rsync sqlite3 python3 parted e2fsprogs udev mosh locales
apt-get install -y intel-microcode firmware-iwlwifi firmware-linux cockpit-pcp \
  xserver-xorg-video-intel smartmontools || true

systemctl enable --now ssh.service
systemctl enable lightdm.service
systemctl set-default graphical.target

install -d -m 0755 /etc/ssh/sshd_config.d
cat >/etc/ssh/sshd_config.d/spectre.conf <<'EOF'
ClientAliveInterval 15
ClientAliveCountMax 4
TCPKeepAlive yes
EOF
systemctl reload ssh.service || systemctl reload sshd.service || true

bash "${REPO_DIR}/scripts/locale-and-time.sh"

apt-get purge -y light-locker xfce4-screensaver 2>/dev/null || true

# Node 22 — Debian 13 ships 20; the proxy and ZCode tooling expect 22.
if ! command -v node >/dev/null 2>&1 || ! node -e 'process.exit(Number(process.versions.node.split(".")[0]) < 22)'; then
  curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
  apt-get install -y nodejs
fi

# Tailscale
if ! command -v tailscale >/dev/null 2>&1; then
  curl -fsSL https://tailscale.com/install.sh | sh
fi

install -d -m 0755 /etc/systemd/logind.conf.d /etc/systemd/sleep.conf.d
install -m 0644 "${REPO_DIR}/config/logind-ignore-lid.conf" /etc/systemd/logind.conf.d/ignore-lid.conf
install -m 0644 "${REPO_DIR}/config/no-sleep.conf" /etc/systemd/sleep.conf.d/no-sleep.conf

install -d -m 0755 /etc/sysctl.d
install -m 0644 "${REPO_DIR}/config/99-worker-sysctl.conf" /etc/sysctl.d/99-worker.conf
sysctl --system >/dev/null

install -d -m 0755 /etc/tlp.d
install -m 0644 "${REPO_DIR}/config/01-spectre-ac.tlp.conf" /etc/tlp.d/01-spectre-ac.conf

install -d -m 0755 /etc/cockpit
install -m 0644 "${REPO_DIR}/config/cockpit.conf" /etc/cockpit/cockpit.conf

# No surprise reboot during a free-endpoint window.
cat >/etc/apt/apt.conf.d/52-no-auto-reboot <<'EOF'
Unattended-Upgrade::Automatic-Reboot "false";
EOF

# Journal and log caps — the proxy log has outgrown its disk before.
install -d -m 0755 /etc/systemd/journald.conf.d
install -m 0644 "${REPO_DIR}/config/journald-caps.conf" /etc/systemd/journald.conf.d/caps.conf
install -m 0644 "${REPO_DIR}/config/logrotate-work.conf" /etc/logrotate.d/work-logs

systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target
systemctl enable --now tlp.service thermald.service cockpit.socket
# Debian ships the daemon as smartmontools.service (smartd.service is
# only an alias on some releases, and enabling the alias can fail with
# "Refusing to operate on linked unit file"). Prefer the real name.
if systemctl list-unit-files smartmontools.service >/dev/null 2>&1; then
  systemctl enable --now smartmontools.service
elif systemctl list-unit-files smartd.service >/dev/null 2>&1; then
  systemctl enable --now smartd.service
else
  echo "WARNING: no smart monitoring unit found; disk health checks will WARN in spectre-doctor" >&2
fi
systemctl restart systemd-journald.service
systemctl restart systemd-logind.service

loginctl enable-linger "${PERSON_USER}"

bash "${REPO_DIR}/scripts/setup-disks.sh" || true
install -d -m 0755 /work
install -d -m 0755 -o "${PERSON_USER}" -g "${PERSON_USER}" /work/person /work/logs /work/npm-cache /work/hardened-zai-proxy
install -m 0755 "${REPO_DIR}/scripts/healthcheck.py" /usr/local/bin/spectre-healthcheck
install -m 0755 "${REPO_DIR}/scripts/stealth.sh" /usr/local/bin/spectre-stealth
install -m 0755 "${REPO_DIR}/scripts/lid-event.sh" /usr/local/bin/spectre-lid-event

install -d -m 0755 /etc/acpi/events
install -m 0644 "${REPO_DIR}/config/acpi/spectre-lid" /etc/acpi/events/spectre-lid
# Debian/acpi-support lid handlers suspend. Ours is the only lid action.
for f in /etc/acpi/events/lidbtn /etc/acpi/events/lid /etc/acpi/lid.sh; do
  [[ -e "${f}" ]] || continue
  mv -f "${f}" "${f}.disabled" || true
done

if [[ -f /etc/UPower/UPower.conf ]]; then
  if grep -q '^#\?IgnoreLid=' /etc/UPower/UPower.conf; then
    sed -i 's/^#\?IgnoreLid=.*/IgnoreLid=true/' /etc/UPower/UPower.conf
  else
    printf '\nIgnoreLid=true\n' >>/etc/UPower/UPower.conf
  fi
fi

install -d -m 0755 /etc/xdg/xfce4/xfconf/xfce-perchannel-xml
install -m 0644 "${REPO_DIR}/config/xfce4-power-manager.xml" \
  /etc/xdg/xfce4/xfconf/xfce-perchannel-xml/xfce4-power-manager.xml
PERSON_HOME="$(getent passwd "${PERSON_USER}" | cut -d: -f6)"
if [[ -n "${PERSON_HOME}" ]]; then
  install -d -m 0755 -o "${PERSON_USER}" -g "${PERSON_USER}" \
    "${PERSON_HOME}/.config/xfce4/xfconf/xfce-perchannel-xml"
  install -m 0644 -o "${PERSON_USER}" -g "${PERSON_USER}" \
    "${REPO_DIR}/config/xfce4-power-manager.xml" \
    "${PERSON_HOME}/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-power-manager.xml"
fi

install -m 0644 "${REPO_DIR}/systemd/lid-inhibit.service" /etc/systemd/system/lid-inhibit.service
install -m 0644 "${REPO_DIR}/systemd/stealth-blank.service" /etc/systemd/system/stealth-blank.service
install -m 0755 "${REPO_DIR}/scripts/charge-limit.sh" /usr/local/bin/spectre-charge-limit
install -m 0644 "${REPO_DIR}/systemd/charge-limit.service" /etc/systemd/system/charge-limit.service
install -d -m 0755 /etc/udev/rules.d
install -m 0644 "${REPO_DIR}/config/99-charge-limit.rules" /etc/udev/rules.d/99-charge-limit.rules
systemctl daemon-reload
systemctl enable --now acpid.service lid-inhibit.service
systemctl enable stealth-blank.service
systemctl enable --now charge-limit.service || true
systemctl restart acpid.service || true
systemctl restart upower.service || true
udevadm control --reload || true
/usr/local/bin/spectre-charge-limit || true

# Wi-Fi power save off if the 6235 is up.
if command -v iw >/dev/null 2>&1; then
  for iface in $(iw dev | awk '/Interface/ {print $2}'); do
    iw dev "${iface}" set power_save off || true
  done
fi

# LightDM autologin if lightdm is the display manager.
if [[ -d /etc/lightdm ]]; then
  install -d -m 0755 /etc/lightdm/lightdm.conf.d
  cat >/etc/lightdm/lightdm.conf.d/01-autologin.conf <<EOF
[Seat:*]
autologin-user=${PERSON_USER}
autologin-user-timeout=0
EOF
fi

PERSON_HOME="$(getent passwd "${PERSON_USER}" | cut -d: -f6)"
if [[ -n "${PERSON_HOME}" ]]; then
  install -d -m 0755 -o "${PERSON_USER}" -g "${PERSON_USER}" \
    "${PERSON_HOME}/.config/autostart" \
    "${PERSON_HOME}/.config/systemd/user"
  install -m 0644 -o "${PERSON_USER}" -g "${PERSON_USER}" \
    "${REPO_DIR}/desktop/stealth-session.desktop" \
    "${PERSON_HOME}/.config/autostart/stealth-session.desktop"
  install -m 0644 -o "${PERSON_USER}" -g "${PERSON_USER}" \
    "${REPO_DIR}/systemd/tmux-work.service" \
    "${PERSON_HOME}/.config/systemd/user/tmux-work.service"
  install -m 0644 -o "${PERSON_USER}" -g "${PERSON_USER}" \
    "${REPO_DIR}/systemd/worker-health.service" \
    "${REPO_DIR}/systemd/worker-health.timer" \
    "${PERSON_HOME}/.config/systemd/user/"
  install -m 0644 -o "${PERSON_USER}" -g "${PERSON_USER}" \
    "${REPO_DIR}/systemd/slack-bridge.service" \
    "${REPO_DIR}/systemd/slack-brief.service" \
    "${REPO_DIR}/systemd/slack-brief.timer" \
    "${PERSON_HOME}/.config/systemd/user/"
  USER_UID="$(id -u "${PERSON_USER}")"
  install -d -m 0700 -o "${PERSON_USER}" -g "${PERSON_USER}" "/run/user/${USER_UID}"
  sudo -u "${PERSON_USER}" XDG_RUNTIME_DIR="/run/user/${USER_UID}" \
    systemctl --user daemon-reload 2>/dev/null || true
  sudo -u "${PERSON_USER}" XDG_RUNTIME_DIR="/run/user/${USER_UID}" \
    systemctl --user enable tmux-work.service worker-health.timer 2>/dev/null || true
  # Slack community units: only once slack.env was written (RUNBOOK 7.10 C).
  if [[ -f "${PERSON_HOME}/.config/remote-agent/slack.env" ]]; then
    sudo -u "${PERSON_USER}" XDG_RUNTIME_DIR="/run/user/${USER_UID}" \
      systemctl --user enable slack-bridge.service slack-brief.timer 2>/dev/null || true
  fi
  # If the user bus was not up yet (fresh boot), start the timer on next login.
  if command -v loginctl >/dev/null 2>&1; then
    loginctl enable-linger "${PERSON_USER}" 2>/dev/null || true
  fi
fi

install -m 0755 "${REPO_DIR}/scripts/status.sh" /usr/local/bin/spectre-status
install -m 0755 "${REPO_DIR}/scripts/pull-operator-keys.sh" /usr/local/bin/spectre-pull-keys
install -m 0755 "${REPO_DIR}/scripts/doctor.sh" /usr/local/bin/spectre-doctor
install -m 0755 "${REPO_DIR}/scripts/session-sync.sh" /usr/local/bin/session-sync

# Slack agent community (RUNBOOK 7.10). Install only; the bridge unit is
# enabled below once ~/.config/remote-agent/slack.env exists.
install -d -m 0755 /usr/local/share/remote-agent
install -m 0755 "${REPO_DIR}/scripts/slack-notify.py" /usr/local/bin/spectre-slack-notify
install -m 0755 "${REPO_DIR}/scripts/slack-brief.py" /usr/local/bin/spectre-slack-brief
install -m 0755 "${REPO_DIR}/scripts/slack-bridge.mjs" /usr/local/bin/spectre-slack-bridge
install -m 0644 "${REPO_DIR}/config/slack-agents.json" /usr/local/share/remote-agent/slack-agents.json
# Executor settings are operator-tuned once installed: never clobber; union
# the repo's deny list into the live file so new denies still land.
settings_dest=/usr/local/share/remote-agent/slack-claude-settings.json
settings_src="${REPO_DIR}/config/slack-claude-settings.example.json"
if [[ ! -f "${settings_dest}" ]]; then
  install -m 0644 "${settings_src}" "${settings_dest}"
elif command -v jq >/dev/null 2>&1; then
  if jq -s '.[0] as $old | .[1] as $new | $old | .permissions.deny = (($old.permissions.deny // []) + ($new.permissions.deny // []) | unique)' \
    "${settings_dest}" "${settings_src}" > "${settings_dest}.tmp"; then
    install -m 0644 "${settings_dest}.tmp" "${settings_dest}" \
      || { rm -f "${settings_dest}.tmp"; exit 1; }
  fi
  rm -f "${settings_dest}.tmp"
fi
install -m 0644 "${REPO_DIR}/config/irreversible-guard.mjs" /usr/local/share/remote-agent/slack-guard.mjs

# Agent guard: box profile. The worker is a disposable appliance, so the
# irreversible-guard narrows its block list to true irreversibles (remotes,
# whole-disk writes, power-off). The env var switches the profile for
# every session; the hook itself is installed per user.
if ! grep -qs "SPECTRE_WORKER_PROFILE" /etc/environment; then
  printf '\nSPECTRE_WORKER_PROFILE=box\n' >>/etc/environment
fi
PERSON_GUARD_HOME="$(getent passwd "${PERSON_USER}" | cut -d: -f6)"
if [[ -n "${PERSON_GUARD_HOME}" ]]; then
  install -d -m 0755 -o "${PERSON_USER}" -g "${PERSON_USER}" \
    "${PERSON_GUARD_HOME}/.claude/hooks"
  install -m 0644 "${REPO_DIR}/config/irreversible-guard.mjs" \
    "${PERSON_GUARD_HOME}/.claude/hooks/irreversible-guard.mjs"
  chown "${PERSON_USER}:${PERSON_USER}" \
    "${PERSON_GUARD_HOME}/.claude/hooks/irreversible-guard.mjs"
  if [[ ! -f "${PERSON_GUARD_HOME}/.claude/settings.json" ]]; then
    install -m 0644 -o "${PERSON_USER}" -g "${PERSON_USER}" \
      "${REPO_DIR}/config/spectre-claude-settings.json" \
      "${PERSON_GUARD_HOME}/.claude/settings.json"
  fi
fi
cat >/etc/profile.d/spectre-motd.sh <<'EOF'
if [ -n "${SSH_CONNECTION:-}" ] && [ -z "${SPECTRE_MOTD_DONE:-}" ]; then
  SPECTRE_MOTD_DONE=1
  export SPECTRE_MOTD_DONE
  command -v spectre-status >/dev/null && spectre-status
fi
EOF
chmod 0644 /etc/profile.d/spectre-motd.sh

# ZCode is re-runnable on its own; a flaky CDN must not abort the whole
# bootstrap (set -e) halfway through the remaining setup.
if ! bash "${REPO_DIR}/scripts/install-zcode.sh" "${PERSON_USER}"; then
  echo "WARNING: install-zcode.sh failed. Re-run it later:" >&2
  echo "  sudo bash ${REPO_DIR}/scripts/install-zcode.sh ${PERSON_USER}" >&2
fi
install -m 0755 "${REPO_DIR}/scripts/pin-zcode-settings.sh" /usr/local/bin/spectre-pin-zcode
install -m 0755 "${REPO_DIR}/scripts/bind-cockpit.sh" /usr/local/bin/spectre-bind-cockpit
bash "${REPO_DIR}/scripts/install-warp.sh"
if tailscale ip -4 >/dev/null 2>&1; then
  bash "${REPO_DIR}/scripts/bind-cockpit.sh" || true
fi

echo
echo "bootstrap done. easiest path — run the interactive wizard:"
echo "  sudo bash ${REPO_DIR}/scripts/setup-wizard.sh"
echo "it walks through: tailscale -> keys -> hardening -> proxy ->"
echo "health env -> ZCode pin -> stealth test -> full verification."
echo
echo "manual steps, if you prefer them one at a time:"
echo "  sudo tailscale up --ssh --hostname=spectre"
echo "  sudo spectre-pull-keys fedora"
echo "  sudo bash ${REPO_DIR}/scripts/harden-network.sh   # ufw + key-only ssh (needs keys pulled first)"
echo "  sudo spectre-bind-cockpit"
echo "  spectre-pin-zcode   # after starting ZCode once (creates setting.json)"
echo "  copy hardened-zai-proxy into /work/hardened-zai-proxy (no node_modules)"
echo "  systemctl --user enable --now glm-proxy.service"
echo "  fill NTFY_TOPIC:  cp config/health.env.example ~/.config/remote-agent/health.env"
echo "  sudo spectre-stealth closed && close the lid"
echo "  warp to fedora / warp from fedora   (same path as on the Zenbook)"
echo "  verify everything:  sudo spectre-doctor"
echo "  verify: systemctl is-active lid-inhibit.service"
echo "  verify: cat /sys/class/power_supply/BAT*/charge_control_end_threshold   # 60 or no such file"
