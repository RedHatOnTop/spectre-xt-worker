#!/bin/bash
# Spectre XT worker — one-click appliance setup.
#
# On the Spectre, after Debian 13 first boot (SSH is enough, desktop optional):
#
#   curl -fsSL https://raw.githubusercontent.com/RedHatOnTop/spectre-xt-worker/main/install.sh | sudo bash
#
# Does not touch API keys, Tailscale login, or ZCode credentials.
set -euo pipefail

REPO_URL="${SPECTRE_REPO_URL:-https://github.com/RedHatOnTop/spectre-xt-worker.git}"
INSTALL_DIR="${SPECTRE_INSTALL_DIR:-/opt/spectre-xt-worker}"

if [[ ${EUID} -ne 0 ]]; then
  echo "run as root:" >&2
  echo "  curl -fsSL https://raw.githubusercontent.com/RedHatOnTop/spectre-xt-worker/main/install.sh | sudo bash" >&2
  exit 1
fi

src="${BASH_SOURCE[0]:-}"
local_root=""
if [[ -n "${src}" && "${src}" != "bash" && "${src}" != "-" ]]; then
  # shellcheck disable=SC2015  # pwd cannot fail once cd succeeded; keep || true as belt
  local_root="$(cd "$(dirname "${src}")" 2>/dev/null && pwd || true)"
fi

if [[ -n "${local_root}" && -f "${local_root}/scripts/bootstrap.sh" ]]; then
  exec bash "${local_root}/scripts/bootstrap.sh"
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y git ca-certificates curl

if [[ -d "${INSTALL_DIR}/.git" ]]; then
  git -C "${INSTALL_DIR}" fetch --depth 1 origin
  git -C "${INSTALL_DIR}" checkout -q -f FETCH_HEAD
else
  # Never delete an existing checkout we did not verify as ours; install
  # beside it and swap atomically instead.
  if [[ -d "${INSTALL_DIR}" ]]; then
    mv "${INSTALL_DIR}" "${INSTALL_DIR}.old.$(date +%s)"
  fi
  git clone --depth 1 "${REPO_URL}" "${INSTALL_DIR}"
fi

exec bash "${INSTALL_DIR}/scripts/bootstrap.sh"
