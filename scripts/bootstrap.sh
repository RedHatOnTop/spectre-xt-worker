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
  rsync sqlite3 python3 parted e2fsprogs udev
apt-get install -y intel-microcode firmware-iwlwifi firmware-linux cockpit-pcp \
  xserver-xorg-video-intel || true

systemctl enable --now ssh.service
systemctl enable lightdm.service
systemctl set-default graphical.target

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

systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target
systemctl enable --now tlp.service thermald.service cockpit.socket
systemctl restart systemd-logind.service

loginctl enable-linger "${PERSON_USER}"

bash "${REPO_DIR}/scripts/setup-disks.sh" || true
install -d -m 0755 /work
install -d -m 0755 -o "${PERSON_USER}" -g "${PERSON_USER}" /work/person /work/logs /work/npm-cache /work/hardened-zai-proxy
install -m 0755 "${REPO_DIR}/scripts/healthcheck.sh" /usr/local/bin/spectre-healthcheck
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
    "${PERSON_HOME}/.config/autostart"
  install -m 0644 -o "${PERSON_USER}" -g "${PERSON_USER}" \
    "${REPO_DIR}/desktop/stealth-session.desktop" \
    "${PERSON_HOME}/.config/autostart/stealth-session.desktop"
fi

bash "${REPO_DIR}/scripts/install-zcode.sh" "${PERSON_USER}"
install -m 0755 "${REPO_DIR}/scripts/bind-cockpit.sh" /usr/local/bin/spectre-bind-cockpit
bash "${REPO_DIR}/scripts/install-warp.sh"
if tailscale ip -4 >/dev/null 2>&1; then
  bash "${REPO_DIR}/scripts/bind-cockpit.sh" || true
fi

echo
echo "bootstrap done. reboot, then:"
echo "  sudo tailscale up --ssh --hostname=spectre"
echo "  sudo spectre-bind-cockpit"
echo "  copy hardened-zai-proxy into /work/hardened-zai-proxy (no node_modules)"
echo "  systemctl --user enable --now glm-proxy.service"
echo "  sudo spectre-stealth closed && close the lid"
echo "  warp to fedora / warp from fedora   (same path as on the Zenbook)"
echo "  verify: systemctl is-active lid-inhibit.service"
echo "  verify: cat /sys/class/power_supply/BAT*/charge_control_end_threshold   # 60 or no such file"
