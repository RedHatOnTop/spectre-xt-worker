#!/bin/bash
# Interactive stage runner: takes a fresh Debian 13 + XFCE box to full
# worker operation, one stage at a time. Safe to re-run — completed
# stages are detected and skipped.
#
#   sudo bash scripts/setup-wizard.sh
#
# Stages (in order):
#   1 bootstrap        packages, sleep lockdown, disks, units, zcode, warp
#   2 tailscale        login + tailscale SSH
#   3 keys             pull operator pubkeys from fedora
#   4 harden           ufw + key-only sshd + fail2ban (refuses without keys)
#   5 cockpit          bind cockpit to loopback + tailscale IP
#   6 proxy            copy hardened-zai-proxy from fedora, enable unit
#   7 health-env       create health.env, ask for ntfy topic, start timer
#   8 zcode-pin        remind to start ZCode once, then pin settings
#   9 stealth-test     stealth closed + lid check instructions
#  10 verify           run spectre-doctor
#
# Anything needing interactive auth (tailscale login URL, ZCode first
# start) prints exactly what to do and waits.
set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PERSON_USER="${SUDO_USER:-${USER:-person}}"
STATE_DIR="/var/lib/spectre-wizard"
STATE_FILE="${STATE_DIR}/stages.done"

if [[ ${EUID} -ne 0 ]]; then
  echo "sudo bash $0" >&2
  exit 1
fi

install -d -m 0755 "${STATE_DIR}"
touch "${STATE_FILE}"

done_has() { grep -qxF "$1" "${STATE_FILE}"; }
mark_done() { grep -qxF "$1" "${STATE_FILE}" || echo "$1" >>"${STATE_FILE}"; }

banner() {
  printf '\n\033[1m== %s ==\033[0m\n' "$1"
}

confirm() {
  local prompt="$1"
  local answer
  read -r -p "${prompt} [y/N] " answer
  [[ "${answer}" == "y" || "${answer}" == "Y" ]]
}

# ---------------------------------------------------------------------------
stage_bootstrap() {
  banner "stage 1: bootstrap (packages, lockdown, disks, units)"
  if done_has bootstrap; then echo "already done, skipping"; return 0; fi
  echo "runs scripts/bootstrap.sh: apt packages, lid/sleep lockdown, /work"
  echo "disk, systemd units, ZCode download, warp install. Reboot advised"
  echo "afterwards but not required to continue."
  if ! confirm "run bootstrap now?"; then
    echo "bootstrap is required before every other stage. aborting." >&2
    exit 1
  fi
  bash "${REPO_DIR}/scripts/bootstrap.sh"
  mark_done bootstrap
  echo
  if confirm "reboot now to load the new units cleanly? (recommended)"; then
    echo "rebooting. after it comes back, run: sudo bash $0"
    systemctl reboot
    exit 0
  fi
}

stage_tailscale() {
  banner "stage 2: tailscale"
  if tailscale status --json >/dev/null 2>&1 &&
     [[ "$(tailscale status --json 2>/dev/null | jq -r '.BackendState // empty')" == "Running" ]]; then
    echo "tailscale already running: $(tailscale ip -4 | head -1)"
    mark_done tailscale
    return 0
  fi
  echo "bringing tailscale up with SSH enabled. A login URL will print;"
  echo "open it from any device already in the tailnet (or the phone)."
  tailscale up --ssh --accept-routes=false --hostname=spectre || {
    echo "tailscale up failed. fix network/login, then re-run this wizard." >&2
    return 1
  }
  mark_done tailscale
}

stage_keys() {
  banner "stage 3: operator keys"
  if done_has keys; then echo "already done, skipping"; return 0; fi
  echo "pulls your public keys from fedora so Termius/warp can log in"
  echo "without a password. Requires tailscale (stage 2)."
  echo "Optional: password SSH over the private LAN works without this."
  if ! confirm "pull keys from fedora now?"; then
    echo "skipped — password login stays as-is."
    mark_done keys
    return 0
  fi
  if ! spectre-pull-keys fedora; then
    echo "key pull failed (non-fatal). You can re-run stage 3 later:" >&2
    echo "  sudo spectre-pull-keys fedora" >&2
    mark_done keys
    return 0
  fi
  mark_done keys

  # GitHub: the same key family works for git push/pull if GitHub knows
  # this box's pubkey. No gh auth login needed on the box.
  banner "github access (for session-sync / git push)"
  local pub
  pub="$(cat "${HOME_DIR_GH:-$(getent passwd "${PERSON_USER}" | cut -d: -f6)}/.ssh/id_ed25519.pub" 2>/dev/null || true)"
  if [[ -n "${pub}" ]]; then
    echo "this box's public key:"
    echo "  ${pub}"
    if ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=8 \
         -i "$(getent passwd "${PERSON_USER}" | cut -d: -f6)/.ssh/id_ed25519" \
         -o IdentitiesOnly=yes -o BatchMode=yes git@github.com 2>&1 | grep -q "successfully authenticated"; then
      echo "github already accepts this box's key. nothing to do."
    else
      echo "add that key at github.com -> Settings -> SSH and GPG keys"
      echo "(or skip: only needed for session-sync and direct pushes)."
      confirm "added it to github?"
    fi
  else
    echo "no id_ed25519.pub on this box yet; generate one before using"
    echo "session-sync:  ssh-keygen -t ed25519  (as ${PERSON_USER})"
  fi
}

stage_harden() {
  banner "stage 4: network hardening"
  if done_has harden; then echo "already done, skipping"; return 0; fi
  echo "ufw default-deny + key-only sshd + fail2ban. Optional: on a"
  echo "trusted private LAN, password SSH alone is a defensible choice —"
  echo "Tailscale already walls the box off from the internet."
  if ! confirm "harden network now?"; then
    echo "skipped — password SSH stays enabled."
    mark_done harden
    return 0
  fi
  if ! bash "${REPO_DIR}/scripts/harden-network.sh"; then
    echo "hardening failed — do NOT close the lid until this passes." >&2
    return 1
  fi
  mark_done harden
}

stage_cockpit() {
  banner "stage 5: cockpit bind"
  if done_has cockpit; then echo "already done, skipping"; return 0; fi
  if ! confirm "bind cockpit to loopback + tailscale IP?"; then return 0; fi
  if ! spectre-bind-cockpit; then
    echo "cockpit bind failed (non-fatal). Re-run: sudo spectre-bind-cockpit" >&2
    return 0
  fi
  mark_done cockpit
}

stage_proxy() {
  banner "stage 6: hardened proxy"
  if systemctl --user is-active --quiet glm-proxy.service 2>/dev/null; then
    echo "glm-proxy already active"
    mark_done proxy
    return 0
  fi
  echo "copies round-robin/hardened-zai-proxy from fedora (rsync over"
  echo "tailscale, no node_modules) into /work/hardened-zai-proxy, then"
  echo "enables the user unit."
  if ! confirm "pull proxy from fedora now?"; then
    echo "skip for now — copy it by hand and run:"
    echo "  systemctl --user enable --now glm-proxy.service"
    return 0
  fi
  install -d -m 0755 -o "${PERSON_USER}" -g "${PERSON_USER}" /work/hardened-zai-proxy
  rsync -a --info=stats1 \
    --exclude node_modules --exclude 'log-viewer/src-tauri/target' \
    --exclude 'hardened-proxy.log' --exclude '.env' --exclude 'env.json' \
    "${PERSON_USER}@fedora:/home/person/Projects/distribution-project/round-robin/hardened-zai-proxy/" \
    "/work/hardened-zai-proxy/" || {
    echo "rsync from fedora failed. copy by hand, then enable the unit." >&2
    return 1
  }
  chown -R "${PERSON_USER}:${PERSON_USER}" /work/hardened-zai-proxy
  echo
  echo "env.json / .env hold the real API keys and never travel by script."
  echo "Copy them by hand now, e.g. from fedora:"
  echo "  scp fedora:.../hardened-zai-proxy/env.json /work/hardened-zai-proxy/"
  confirm "copied env.json into /work/hardened-zai-proxy?"
  sudo -u "${PERSON_USER}" XDG_RUNTIME_DIR="/run/user/$(id -u "${PERSON_USER}")" \
    systemctl --user enable --now glm-proxy.service || {
    echo "unit enable failed — check ~/.config/systemd/user/glm-proxy.service" >&2
    return 1
  }
  sleep 2
  if curl -fsS --max-time 5 http://127.0.0.1:18088/health | jq -e '.status=="ok"' >/dev/null 2>&1; then
    echo "proxy healthy"
    mark_done proxy
  else
    echo "proxy did not answer :18088/health — check:" >&2
    echo "  journalctl --user -u glm-proxy.service -n 50" >&2
    return 1
  fi
}

stage_health_env() {
  banner "stage 7: health env + timer"
  local env_file="/home/${PERSON_USER}/.config/remote-agent/health.env"
  if done_has health-env && [[ -f "${env_file}" ]]; then
    echo "already done, skipping"
    return 0
  fi
  install -d -m 0700 -o "${PERSON_USER}" -g "${PERSON_USER}" "$(dirname "${env_file}")"
  if [[ ! -f "${env_file}" ]]; then
    install -m 0600 -o "${PERSON_USER}" -g "${PERSON_USER}" \
      "${REPO_DIR}/config/health.env.example" "${env_file}"
  fi
  local topic
  read -r -p "ntfy topic for push alerts (empty = skip, no pushes): " topic
  if [[ -n "${topic}" ]]; then
    sed -i "s|^NTFY_TOPIC=.*|NTFY_TOPIC=${topic}|" "${env_file}"
    chown "${PERSON_USER}:${PERSON_USER}" "${env_file}"
    echo "pushes will go to ${topic} on ntfy.sh — subscribe from the Fold 7."
  else
    echo "skipping ntfy; health.log still records failures."
  fi
  sudo -u "${PERSON_USER}" XDG_RUNTIME_DIR="/run/user/$(id -u "${PERSON_USER}")" \
    systemctl --user enable --now worker-health.timer 2>/dev/null || true
  mark_done health-env
}

stage_zcode_pin() {
  banner "stage 8: ZCode settings"
  if done_has zcode-pin; then echo "already done, skipping"; return 0; fi
  echo "start ZCode once now (autostart entry exists, or run"
  echo "'zcode-worker' from the menu) so ~/.zcode/v2/setting.json exists."
  echo "Then in ZCode: add the 'Hardened' OpenAI-compatible provider"
  echo "(base URL http://127.0.0.1:18088/v1, dummy key) and pin the model."
  confirm "ZCode has started at least once?"
  if sudo -u "${PERSON_USER}" -i spectre-pin-zcode; then
    mark_done zcode-pin
  else
    echo "pin failed — setting.json missing? Start ZCode, then re-run:" >&2
    echo "  sudo -i spectre-pin-zcode   (as ${PERSON_USER})" >&2
    return 1
  fi
}

stage_stealth_test() {
  banner "stage 9: stealth + looks-off check"
  if done_has stealth-test; then echo "already done, skipping"; return 0; fi
  echo "plugs in the HDMI dummy first if it is not in. Then:"
  confirm "run 'spectre-stealth closed' now?"
  spectre-stealth closed
  echo
  echo "close the lid. From a metre away: no panel glow, no keyboard glow."
  echo "Then from fedora verify it stayed awake:"
  echo "  ping spectre"
  echo "  ssh person@spectre 'curl -fsS http://127.0.0.1:18088/health'"
  confirm "did both checks pass from fedora with the lid closed?"
  mark_done stealth-test
}

stage_verify() {
  banner "stage 10: full verification"
  spectre-doctor
  local rc=$?
  if (( rc == 0 )); then
    mark_done verify
    echo
    echo "ALL STAGES COMPLETE. The box is a worker now. Lid stays closed."
  else
    echo
    echo "spectre-doctor reported failures above. Fix them, then re-run:" >&2
    echo "  sudo bash $0" >&2
  fi
  return "${rc}"
}

# ---------------------------------------------------------------------------
stage_bootstrap
stage_tailscale   || { echo "re-run the wizard after fixing tailscale." >&2; exit 1; }
stage_keys        || { echo "re-run the wizard after fixing key pull." >&2; exit 1; }
stage_harden      || { echo "re-run the wizard after fixing hardening." >&2; exit 1; }
stage_cockpit
stage_proxy       || { echo "re-run the wizard after fixing the proxy." >&2; exit 1; }
stage_health_env
stage_zcode_pin   || { echo "re-run the wizard to finish ZCode pinning." >&2; exit 1; }
stage_stealth_test
stage_verify
