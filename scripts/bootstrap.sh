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

apt-get update
apt-get install -y \
  ca-certificates curl gnupg jq git tmux \
  tlp thermald lm-sensors intel-microcode \
  firmware-iwlwifi firmware-linux \
  cockpit cockpit-pcp \
  unattended-upgrades apt-listchanges \
  xserver-xorg-video-intel

# Node 22 — Debian 13 ships 20; the proxy and ZCode tooling expect 22.
if ! command -v node >/dev/null 2>&1 || ! node -e 'process.exit(Number(process.versions.node.split(".")[0]) < 22)'; then
  curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
  apt-get install -y nodejs
fi

# Tailscale
if ! command -v tailscale >/dev/null 2>&1; then
  curl -fsSL https://tailscale.com/install.sh | sh
fi

install -d -m 0755 /etc/systemd/logind.conf.d
install -m 0644 "${REPO_DIR}/config/logind-ignore-lid.conf" /etc/systemd/logind.conf.d/ignore-lid.conf

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

install -d -m 0755 /work
install -d -m 0755 -o "${PERSON_USER}" -g "${PERSON_USER}" /work/person /work/logs /work/npm-cache /work/hardened-zai-proxy
install -m 0755 "${REPO_DIR}/scripts/healthcheck.sh" /usr/local/bin/spectre-healthcheck

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

echo
echo "bootstrap done."
echo "next:"
echo "  sudo tailscale up --ssh --hostname=spectre"
echo "  copy hardened-zai-proxy into /work/hardened-zai-proxy (no node_modules)"
echo "  systemctl --user enable --now glm-proxy.service"
echo "  bind cockpit: see config/cockpit.socket.d/override.conf.example"
echo "  verify: cat /sys/firmware/efi/fw_platform_size   # must be 64"
