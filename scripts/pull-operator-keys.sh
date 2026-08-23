#!/bin/bash
# After Tailscale is up, copy operator pubkeys from fedora so Termius and warp
# can SSH without a password. Public keys only.
set -euo pipefail

PEER="${1:-fedora}"
PERSON_USER="${SUDO_USER:-${USER:-person}}"
HOME_DIR="$(getent passwd "${PERSON_USER}" | cut -d: -f6)"
AUTH="${HOME_DIR}/.ssh/authorized_keys"

install -d -m 0700 -o "${PERSON_USER}" -g "${PERSON_USER}" "${HOME_DIR}/.ssh"
touch "${AUTH}"
chmod 0600 "${AUTH}"
chown "${PERSON_USER}:${PERSON_USER}" "${AUTH}"

tmp="$(mktemp)"
ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=8 \
  "${PERSON_USER}@${PEER}" \
  'cat ~/.ssh/machismo_phone.pub ~/.ssh/id_ed25519.pub ~/.ssh/id_rsa.pub 2>/dev/null || true' \
  >"${tmp}" || {
  echo "could not reach ${PEER}. run: sudo tailscale up --ssh --hostname=spectre" >&2
  rm -f "${tmp}"
  exit 1
}

added=0
while IFS= read -r line; do
  [[ -z "${line}" || "${line}" == \#* ]] && continue
  if grep -qxF "${line}" "${AUTH}"; then
    continue
  fi
  printf '%s\n' "${line}" >>"${AUTH}"
  added=$((added + 1))
done <"${tmp}"
rm -f "${tmp}"
chown "${PERSON_USER}:${PERSON_USER}" "${AUTH}"
echo "authorized_keys: +${added} from ${PEER}"
