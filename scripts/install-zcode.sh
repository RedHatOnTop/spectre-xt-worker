#!/bin/bash
# Install official ZCode Linux build for the worker user. Idempotent.
set -euo pipefail

PERSON_USER="${1:-${SUDO_USER:-person}}"
PERSON_HOME="$(getent passwd "${PERSON_USER}" | cut -d: -f6)"
if [[ -z "${PERSON_HOME}" ]]; then
  echo "no home for ${PERSON_USER}" >&2
  exit 1
fi

ZCODE_VERSION="${ZCODE_VERSION:-3.8.1}"
DEB_URL="${ZCODE_DEB_URL:-https://cdn-zcode.z.ai/zcode/electron/releases/${ZCODE_VERSION}/linux-x64/ZCode-${ZCODE_VERSION}-linux-x64.deb}"
APPIMAGE_URL="${ZCODE_APPIMAGE_URL:-https://cdn-zcode.z.ai/zcode/electron/releases/${ZCODE_VERSION}/linux-x64/ZCode-${ZCODE_VERSION}-linux-x64.AppImage}"

install -d -m 0755 /tmp/spectre-zcode
deb=/tmp/spectre-zcode/ZCode.deb
app=/tmp/spectre-zcode/ZCode.AppImage
bin=""

if curl -fL --retry 3 -o "${deb}" "${DEB_URL}"; then
  apt-get install -y "${deb}" || dpkg -i "${deb}" || apt-get install -f -y
  pkg="$(dpkg-deb -f "${deb}" Package)"
  bin="$(dpkg -L "${pkg}" 2>/dev/null | awk '/\/zcode$/ {print; exit}')"
fi

if [[ -z "${bin}" || ! -x "${bin}" ]]; then
  curl -fL --retry 3 -o "${app}" "${APPIMAGE_URL}"
  chmod +x "${app}"
  extract=/tmp/spectre-zcode/squashfs-root
  rm -rf "${extract}"
  (cd /tmp/spectre-zcode && "${app}" --appimage-extract)
  dest="${PERSON_HOME}/.local/share/zcode"
  rm -rf "${dest}"
  mv "${extract}" "${dest}"
  chown -R "${PERSON_USER}:${PERSON_USER}" "${dest}"
  bin="${dest}/zcode"
fi

if [[ ! -x "${bin}" ]]; then
  echo "ZCode binary not found after install" >&2
  exit 1
fi

install -d -m 0755 -o "${PERSON_USER}" -g "${PERSON_USER}" "${PERSON_HOME}/.local/bin"
cat >"${PERSON_HOME}/.local/bin/zcode-worker" <<EOF
#!/bin/bash
exec '${bin}' --disable-gpu "\$@"
EOF
chown "${PERSON_USER}:${PERSON_USER}" "${PERSON_HOME}/.local/bin/zcode-worker"
chmod 0755 "${PERSON_HOME}/.local/bin/zcode-worker"

autostart="${PERSON_HOME}/.config/autostart/zcode-worker.desktop"
install -d -m 0755 -o "${PERSON_USER}" -g "${PERSON_USER}" "${PERSON_HOME}/.config/autostart"
cat >"${autostart}" <<EOF
[Desktop Entry]
Type=Application
Name=ZCode worker
Exec=${PERSON_HOME}/.local/bin/zcode-worker
X-GNOME-Autostart-enabled=true
StartupNotify=false
Terminal=false
EOF
chown "${PERSON_USER}:${PERSON_USER}" "${autostart}"

echo "zcode installed at ${bin}"
