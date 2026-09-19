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
    "${REPO_DIR}/systemd/devspace.service" \
    "${REPO_DIR}/systemd/goal-supervisor.service" \
    "${REPO_DIR}/systemd/goal-supervisor.timer" \
    "${REPO_DIR}/systemd/spectre-worker-state.service" \
    "${REPO_DIR}/systemd/spectre-continuity.service" \
    "${REPO_DIR}/systemd/spectre-continuity.timer" \
    "${REPO_DIR}/systemd/qoder-goal-watch.service" \
    "${REPO_DIR}/systemd/qoder-goal-watch.timer" \
    "${PERSON_HOME}/.config/systemd/user/"
  USER_UID="$(id -u "${PERSON_USER}")"
  install -d -m 0700 -o "${PERSON_USER}" -g "${PERSON_USER}" "/run/user/${USER_UID}"
  sudo -u "${PERSON_USER}" XDG_RUNTIME_DIR="/run/user/${USER_UID}" \
    systemctl --user daemon-reload 2>/dev/null || true
  sudo -u "${PERSON_USER}" XDG_RUNTIME_DIR="/run/user/${USER_UID}" \
    systemctl --user enable tmux-work.service worker-health.timer \
      spectre-worker-state.service spectre-continuity.timer \
      qoder-goal-watch.timer 2>/dev/null || true
  # Retired consumer paths (RUNBOOK 7.17): after the worker-state cutover these
  # classify worker occupancy from session jsonl or TUI screen text on their own
  # and could type a second, contradictory /goal or resume. A box that predates
  # the cutover still has them enabled; a fresh bootstrap never installs them.
  for legacy in qoder-nudge.timer qoder-continuity.timer \
                codex-goal-healer.timer grokbot-goal-event.timer \
                grokbot-goal-event.path; do
    sudo -u "${PERSON_USER}" XDG_RUNTIME_DIR="/run/user/${USER_UID}" \
      systemctl --user disable --now "${legacy}" 2>/dev/null || true
  done
  # Slack community units: only once slack.env was written (RUNBOOK 7.10 C).
  if [[ -f "${PERSON_HOME}/.config/remote-agent/slack.env" ]]; then
    sudo -u "${PERSON_USER}" XDG_RUNTIME_DIR="/run/user/${USER_UID}" \
      systemctl --user enable slack-bridge.service slack-brief.timer 2>/dev/null || true
  fi
  # devspace connector unit: enable only once the binary is actually
  # installed (RUNBOOK 7.14) — a unit that cannot exec is boot noise.
  if command -v devspace >/dev/null 2>&1; then
    sudo -u "${PERSON_USER}" XDG_RUNTIME_DIR="/run/user/${USER_UID}" \
      systemctl --user enable devspace.service 2>/dev/null || true
  fi
  # Goal supervisor (RUNBOOK 7.15): enabled only when the reviewer can actually
  # run — grok present AND its isolated GROK_HOME profile installed. Otherwise
  # every tick would fail closed for nothing; the timer stays installed-off and
  # the RUNBOOK enable step is the switch.
  if [[ -x "${PERSON_HOME}/.local/bin/grok" && \
        -f "${PERSON_HOME}/.local/share/remote-agent/grok-supervisor/config.toml" ]]; then
    sudo -u "${PERSON_USER}" XDG_RUNTIME_DIR="/run/user/${USER_UID}" \
      systemctl --user enable goal-supervisor.timer 2>/dev/null || true
  fi
  # If the user bus was not up yet (fresh boot), start the timer on next login.
  if command -v loginctl >/dev/null 2>&1; then
    loginctl enable-linger "${PERSON_USER}" 2>/dev/null || true
  fi
fi

install -m 0755 "${REPO_DIR}/scripts/status.sh" /usr/local/bin/spectre-status
install -m 0755 "${REPO_DIR}/scripts/pull-operator-keys.sh" /usr/local/bin/spectre-pull-keys
install -m 0755 "${REPO_DIR}/scripts/doctor.sh" /usr/local/bin/spectre-doctor
# DevSpace tilde-path patch (RUNBOOK 7.14): `npm install -g @waishnav/devspace`
# overwrites dist/, so the box keeps the re-apply tool beside the doctor.
install -m 0755 "${REPO_DIR}/scripts/patch-devspace-tilde.sh" /usr/local/bin/spectre-patch-devspace-tilde
install -m 0755 "${REPO_DIR}/scripts/session-sync.sh" /usr/local/bin/session-sync
# Codex session handoff (RUNBOOK 7.15): the CLI for the user, the rollout
# helper where codex-handoff.sh and its remote helper lookup expect it.
install -m 0755 "${REPO_DIR}/scripts/codex-handoff.sh" /usr/local/bin/codex-handoff
install -d -m 0755 /usr/local/lib/spectre-codex
install -m 0644 "${REPO_DIR}/scripts/codex_rollout.py" \
  /usr/local/lib/spectre-codex/codex_rollout.py

# Slack agent community (RUNBOOK 7.10). Install only; the bridge unit is
# enabled below once ~/.config/remote-agent/slack.env exists.
install -d -m 0755 /usr/local/share/remote-agent
install -m 0755 "${REPO_DIR}/scripts/slack-notify.py" /usr/local/bin/spectre-slack-notify
install -m 0755 "${REPO_DIR}/scripts/slack-brief.py" /usr/local/bin/spectre-slack-brief
install -m 0755 "${REPO_DIR}/scripts/slack-bridge.mjs" /usr/local/bin/spectre-slack-bridge
install -m 0755 "${REPO_DIR}/scripts/worker-state-client.mjs" /usr/local/bin/worker-state-client.mjs
install -m 0644 "${REPO_DIR}/config/slack-agents.json" /usr/local/share/remote-agent/slack-agents.json
# Goal supervisor (RUNBOOK 7.15): the Grokbot reviewer. Installed off — the
# timer is enabled above only once grok and its isolated profile exist.
install -m 0755 "${REPO_DIR}/scripts/goal-supervisor.py" /usr/local/bin/spectre-goal-supervisor
# Authoritative worker-state daemon (RUNBOOK 7.17): the single resolver every
# consumer asks. Package lives next to the CLI so `serve` and the client share
# one resolver.
install -d -m 0755 /usr/local/lib/spectre-worker-state/worker_state
install -m 0644 "${REPO_DIR}/scripts/worker_state/"*.py \
  "${REPO_DIR}/scripts/worker_state/schema.sql" \
  /usr/local/lib/spectre-worker-state/worker_state/
install -m 0755 "${REPO_DIR}/scripts/spectre-state.py" /usr/local/bin/spectre-state
install -m 0755 "${REPO_DIR}/scripts/spectre-continuity.py" /usr/local/bin/spectre-continuity
install -m 0644 "${REPO_DIR}/config/goal-supervisor-prompt.md" \
  /usr/local/share/remote-agent/goal-supervisor-prompt.md
# The reviewer's isolated GROK_HOME profile: without it the grok CLI would scan
# vendor MCP/config sources and the read-only floor would leak (a live probe
# caught the model reaching for an MCP write tool).
# Marker-based, not "if absent": an empty GROK_HOME self-initializes on the first
# grok run and writes its own stub config.toml (probed 2026-09-15 — a 2-line
# [marketplace] block), which would otherwise shadow this profile forever. A file
# that already carries the marker is left alone, with a backup taken first.
if [[ -n "${PERSON_HOME}" ]]; then
  grok_profile_dir="${PERSON_HOME}/.local/share/remote-agent/grok-supervisor"
  install -d -m 0755 -o "${PERSON_USER}" -g "${PERSON_USER}" "${grok_profile_dir}"
  if ! grep -qs '^disabled_mcp_servers' "${grok_profile_dir}/config.toml" 2>/dev/null; then
    if [[ -f "${grok_profile_dir}/config.toml" ]]; then
      cp -a "${grok_profile_dir}/config.toml" \
        "${grok_profile_dir}/config.toml.bak-$(date -u +%Y%m%dT%H%M%SZ)"
    fi
    install -m 0644 -o "${PERSON_USER}" -g "${PERSON_USER}" \
      "${REPO_DIR}/config/grok-supervisor/config.toml" \
      "${grok_profile_dir}/config.toml.incoming"
    mv "${grok_profile_dir}/config.toml.incoming" "${grok_profile_dir}/config.toml"
  fi
fi
# The reviewer credential example (RUNBOOK 7.16 auth): reference only. The real
# ~/.config/remote-agent/grok.env is user-owned, written by hand, mode 0600 and
# never committed — nothing here writes it.
install -m 0644 "${REPO_DIR}/config/grok.env.example" \
  /usr/local/share/remote-agent/grok.env.example
# #lobby debate (RUNBOOK 7.10): the example read-only agy profile. Installed
# to the share dir only — the live path ~/.gemini/antigravity-cli/settings.json
# is user-owned and hand-installed; nothing here ever writes ~/.gemini.
install -m 0644 "${REPO_DIR}/config/agy-slack-settings.example.json" \
  /usr/local/share/remote-agent/agy-slack-settings.example.json
# Executor settings: the repo is the source of truth for allow (so
# tightenings land on installed boxes) and defaultMode; the deny list is
# unioned so local hardening is never lost. Unknown local keys (a hand-added
# hooks section, say) are dropped — qodercli does not execute them anyway.
settings_dest=/usr/local/share/remote-agent/slack-executor-settings.json
settings_src="${REPO_DIR}/config/slack-executor-settings.example.json"
# The merge below only ever ADDS deny entries, so it can tighten but never
# widen — a repo deny removal does not reach an existing live file. The
# 2026-09-16 widened posture was applied by replacing the live file from the
# repo by hand; from then on the union is idempotent (RUNBOOK 7.10).
if [[ ! -f "${settings_dest}" ]]; then
  # same tmp + mv dance as the merge path below: a partial file must not
  # "exist" and shadow every future install
  install -m 0644 "${settings_src}" "${settings_dest}.tmp"
  mv "${settings_dest}.tmp" "${settings_dest}"
elif command -v jq >/dev/null 2>&1; then
  # tmp + mv within the same directory: replacing the live file must be
  # atomic — a truncated write would strand the deny floor.
  if jq -s '.[0] as $old | .[1] as $new | $new | .permissions.deny = ((($old.permissions.deny // []) + ($new.permissions.deny // [])) | unique)' \
    "${settings_dest}" "${settings_src}" > "${settings_dest}.tmp"; then
    chmod 0644 "${settings_dest}.tmp"
    mv "${settings_dest}.tmp" "${settings_dest}"
  else
    rm -f "${settings_dest}.tmp"
    echo "WARN: settings merge failed; ${settings_dest} left unchanged" >&2
  fi
else
  echo "WARN: jq not found; ${settings_dest} not refreshed from the repo" >&2
fi
# The executor itself (qoder-efficient wrapper -> qodercli) is box-local at
# ~/.local/bin; spectre-doctor reports when it is missing.

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

# Codex CLI (RUNBOOK 7.11). npm failure must not abort the rest of the
# bootstrap; the provider key is transferred from the daily driver later.
if ! bash "${REPO_DIR}/scripts/install-codex.sh" "${PERSON_USER}"; then
  echo "WARNING: install-codex.sh failed. Re-run it later:" >&2
  echo "  sudo bash ${REPO_DIR}/scripts/install-codex.sh ${PERSON_USER}" >&2
fi

# Headless browser for every agent (RUNBOOK 7.12). Needs the network for the
# release tarball; a CDN failure must not abort the rest of the bootstrap.
if ! bash "${REPO_DIR}/scripts/install-obscura.sh" "${PERSON_USER}"; then
  echo "WARNING: install-obscura.sh failed. Re-run it later:" >&2
  echo "  sudo bash ${REPO_DIR}/scripts/install-obscura.sh ${PERSON_USER}" >&2
fi

# Workspace companion for the agents (RUNBOOK 7.13). Vendored tree, no network;
# a failure must not abort the rest of the bootstrap.
if ! bash "${REPO_DIR}/scripts/install-devcodex.sh" "${PERSON_USER}"; then
  echo "WARNING: install-devcodex.sh failed. Re-run it later:" >&2
  echo "  sudo bash ${REPO_DIR}/scripts/install-devcodex.sh ${PERSON_USER}" >&2
fi
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
echo "  transfer codex provider registry + key from fedora (RUNBOOK 7.11)"
echo "  fill NTFY_TOPIC:  cp config/health.env.example ~/.config/remote-agent/health.env"
echo "  sudo spectre-stealth closed && close the lid"
echo "  warp to fedora / warp from fedora   (same path as on the Zenbook)"
echo "  verify everything:  sudo spectre-doctor"
echo "  verify: systemctl is-active lid-inhibit.service"
echo "  verify: cat /sys/class/power_supply/BAT*/charge_control_end_threshold   # 60 or no such file"
