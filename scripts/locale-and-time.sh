#!/bin/bash
# Match the Zenbook: en_US.UTF-8, Asia/Seoul, US keymap, Korean IME available.
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "sudo bash $0" >&2
  exit 1
fi

apt-get install -y locales fonts-noto-cjk fonts-noto-color-emoji \
  ibus ibus-hangul ibus-gtk3 ibus-gtk4 2>/dev/null || apt-get install -y locales fonts-noto-cjk ibus ibus-hangul

sed -i -E 's/^# ?en_US.UTF-8 UTF-8/en_US.UTF-8 UTF-8/; s/^# ?ko_KR.UTF-8 UTF-8/ko_KR.UTF-8 UTF-8/' /etc/locale.gen
grep -q '^en_US.UTF-8 UTF-8' /etc/locale.gen || echo 'en_US.UTF-8 UTF-8' >>/etc/locale.gen
grep -q '^ko_KR.UTF-8 UTF-8' /etc/locale.gen || echo 'ko_KR.UTF-8 UTF-8' >>/etc/locale.gen
locale-gen
update-locale LANG=en_US.UTF-8 LC_TIME=en_US.UTF-8 LANGUAGE=en_US:en

timedatectl set-timezone Asia/Seoul
timedatectl set-ntp true || true
hostnamectl set-hostname spectre || true

if command -v localectl >/dev/null 2>&1; then
  localectl set-locale LANG=en_US.UTF-8
  localectl set-keymap us || true
  localectl set-x11-keymap us || true
fi

PERSON_USER="${SUDO_USER:-person}"
PERSON_HOME="$(getent passwd "${PERSON_USER}" | cut -d: -f6)"
if [[ -n "${PERSON_HOME}" ]]; then
  grep -q GTK_IM_MODULE "${PERSON_HOME}/.profile" 2>/dev/null || cat >>"${PERSON_HOME}/.profile" <<'EOF'

export GTK_IM_MODULE=ibus
export QT_IM_MODULE=ibus
export XMODIFIERS=@im=ibus
EOF
  chown "${PERSON_USER}:${PERSON_USER}" "${PERSON_HOME}/.profile"
fi

echo "locale: en_US.UTF-8  timezone: Asia/Seoul  hangul: ibus"
