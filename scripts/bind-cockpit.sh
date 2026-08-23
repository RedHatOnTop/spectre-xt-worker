#!/bin/bash
# Bind Cockpit to loopback + Tailscale IPv4. Run after `tailscale up`.
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "sudo bash $0" >&2
  exit 1
fi

ts_ip="$(tailscale ip -4 2>/dev/null || true)"
if [[ -z "${ts_ip}" ]]; then
  echo "tailscale is not up. run: sudo tailscale up --ssh --hostname=spectre" >&2
  exit 1
fi

install -d -m 0755 /etc/systemd/system/cockpit.socket.d
cat >/etc/systemd/system/cockpit.socket.d/override.conf <<EOF
[Socket]
ListenStream=
ListenStream=127.0.0.1:9090
ListenStream=${ts_ip}:9090
EOF

systemctl daemon-reload
systemctl restart cockpit.socket
echo "cockpit on https://${ts_ip}:9090"
