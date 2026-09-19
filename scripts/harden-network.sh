#!/bin/bash
# Firewall + key-only SSH for the Spectre worker.
#
# Order matters to avoid locking yourself out:
#   1. confirm an operator pubkey is already in authorized_keys
#   2. switch sshd to key-only
#   3. only then raise the ufw deny-by-default firewall
#
# Tailscale traffic is never blocked: the tailscale0 interface and the
# WireGuard UDP port are allowed explicitly; loopback is untouched.
# With Tailscale SSH enabled on the tailnet, even a total ufw mistake
# cannot lock you out of `tailscale ssh`.
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "sudo bash $0" >&2
  exit 1
fi

PERSON_USER="${SUDO_USER:-person}"
HOME_DIR="$(getent passwd "${PERSON_USER}" | cut -d: -f6)"
AUTH="${HOME_DIR}/.ssh/authorized_keys"

count_keys() {
  local n=0 line
  while IFS= read -r line; do
    case "${line}" in
      ''|'#'*) ;;
      *) n=$((n + 1)) ;;
    esac
  done <"$1"
  echo "${n}"
}

key_count=0
if [[ -f "${AUTH}" ]]; then
  key_count="$(count_keys "${AUTH}")"
fi
if (( key_count == 0 )); then
  echo "no public keys in ${AUTH} yet." >&2
  echo "run first:" >&2
  echo "  sudo tailscale up --ssh --hostname=spectre" >&2
  echo "  sudo spectre-pull-keys fedora" >&2
  echo "refusing to disable password login with no keys on disk." >&2
  exit 1
fi
echo "authorized_keys has ${key_count} key(s); proceeding"

apt-get update -qq
apt-get install -y -qq ufw fail2ban >/dev/null

# --- key-only SSH ---------------------------------------------------------
install -d -m 0755 /etc/ssh/sshd_config.d
cat >"/tmp/spectre-hardening.conf" <<EOF
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
MaxAuthTries 4
LoginGraceTime 20
ClientAliveInterval 15
ClientAliveCountMax 4
AllowUsers ${PERSON_USER}
EOF
install -m 0644 /tmp/spectre-hardening.conf /etc/ssh/sshd_config.d/spectre-hardening.conf
rm -f /tmp/spectre-hardening.conf
sshd -t
systemctl reload ssh.service || systemctl reload sshd.service
echo "ssh is now key-only for user ${PERSON_USER}"

# --- firewall -------------------------------------------------------------
# Idempotent: re-adding an existing rule errors; ignore that failure.
allow() {
  ufw allow "$@" >/dev/null 2>&1 || true
}

allow "in on lo"
if ufw allow in on tailscale0 >/dev/null 2>&1; then
  :
else
  echo "tailscale0 interface missing — run tailscale up first" >&2
  exit 1
fi
allow 41641/udp    # tailscale WireGuard port, keeps direct connections alive
allow 3478/udp     # tailscale DERP fallback discovery
allow "22/tcp comment 'ssh'"

ufw --force enable
uff_status="$(ufw status verbose)"
echo "${uff_status}"

# --- fail2ban -------------------------------------------------------------
cat >/etc/fail2ban/jail.local <<'EOF'
[DEFAULT]
bantime = 1h
findtime = 10m
maxretry = 5
backend = systemd

[sshd]
enabled = true
mode = aggressive
EOF
systemctl enable --now fail2ban.service
fail2ban-client status sshd || true

echo
echo "network hardened: ufw default-deny in, ssh key-only, fail2ban active."
echo "verify from fedora before closing the lid:"
echo "  ssh ${PERSON_USER}@spectre true && echo ok"
