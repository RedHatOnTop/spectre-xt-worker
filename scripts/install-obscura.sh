#!/bin/bash
# Install obscura — a headless browser (Rust + V8, no Chromium) — for the
# worker user and open it to every agent on the box: binary on PATH, a CDP
# server on 127.0.0.1:9222 as a systemd user unit, and an `obscura` MCP entry
# in Claude Code, Qoder CLI and Codex. Idempotent.
#
#   sudo bash scripts/install-obscura.sh [person]
#
# The tarball is pinned by sha256. Upstream publishes no checksums, so the pin
# means "the artifact reviewed on 2026-09-14", not "proven upstream" — to move
# versions, download the new asset and update OBSCURA_VERSION and
# OBSCURA_SHA256 together.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PERSON_USER="${1:-${SUDO_USER:-person}}"
PERSON_HOME="$(getent passwd "${PERSON_USER}" | cut -d: -f6)"
if [[ -z "${PERSON_HOME}" ]]; then
  echo "no home for ${PERSON_USER}" >&2
  exit 1
fi

OBSCURA_VERSION="${OBSCURA_VERSION:-0.2.2}"
OBSCURA_ASSET="obscura-x86_64-linux-stealth.tar.gz"
OBSCURA_SHA256="${OBSCURA_SHA256:-faf46c28948c10c6d44d6f46faad577adba43d63bb19b83cdb92a5e22bdd5da1}"

if [[ "$(uname -m)" != "x86_64" ]]; then
  echo "obscura asset is pinned for x86_64; this box is $(uname -m)" >&2
  exit 1
fi

as_user() { runuser -u "${PERSON_USER}" -- "$@"; }
install_u() { install -o "${PERSON_USER}" -g "${PERSON_USER}" "$@"; }
user_systemctl() {
  as_user env XDG_RUNTIME_DIR="/run/user/$(id -u "${PERSON_USER}")" \
    systemctl --user "$@"
}

work="$(mktemp -d)"
trap 'rm -rf "${work}"' EXIT

curl -fL --retry 3 -o "${work}/${OBSCURA_ASSET}" \
  "https://github.com/h4ckf0r0day/obscura/releases/download/v${OBSCURA_VERSION}/${OBSCURA_ASSET}"
echo "${OBSCURA_SHA256}  ${work}/${OBSCURA_ASSET}" | sha256sum -c -
tar -xzf "${work}/${OBSCURA_ASSET}" -C "${work}"
# The parallel `scrape` command looks for obscura-worker next to obscura.
install -m 0755 "${work}/obscura" "${work}/obscura-worker" /usr/local/bin/
/usr/local/bin/obscura --version

# CDP server for Playwright/Puppeteer-style scripted use. Agents that only need
# to read a page use the CLI or their own stdio MCP instance.
install_u -d -m 0755 "${PERSON_HOME}/.config/systemd/user"
install_u -m 0644 "${REPO_DIR}/systemd/obscura-cdp.service" \
  "${PERSON_HOME}/.config/systemd/user/obscura-cdp.service"
user_systemctl daemon-reload
user_systemctl enable --now obscura-cdp.service

# MCP: each agent spawns its own stdio instance, so there is no shared port to
# race on. --allow-private-network is what lets an agent preview a local dev
# server; it does not widen the CDP listener, which stays on loopback.
MCP_ARGS=(/usr/local/bin/obscura mcp --stealth --allow-private-network)
if [[ -x /usr/local/bin/claude ]]; then
  as_user /usr/local/bin/claude mcp remove obscura >/dev/null 2>&1 || true
  as_user /usr/local/bin/claude mcp add -s user obscura -- "${MCP_ARGS[@]}"
fi
# Qoder CLI ships versioned binaries; take the newest.
qcli="$(ls -1 "${PERSON_HOME}/.qoder/bin/qodercli/qodercli-"* 2>/dev/null | sort -V | tail -n1 || true)"
if [[ -n "${qcli}" && -x "${qcli}" ]]; then
  as_user "${qcli}" mcp remove obscura >/dev/null 2>&1 || true
  as_user "${qcli}" mcp add -s user -t stdio obscura -- "${MCP_ARGS[@]}"
fi
cfg="${PERSON_HOME}/.codex/config.toml"
if [[ -f "${cfg}" ]] && ! grep -q '^\[mcp_servers\.obscura\]' "${cfg}"; then
  cat >>"${cfg}" <<'EOF'

# Headless browser (scripts/install-obscura.sh). The CLI form is
# `obscura fetch <url> --dump text`; this block is the MCP form.
[mcp_servers.obscura]
command = "/usr/local/bin/obscura"
args = ["mcp", "--stealth", "--allow-private-network"]
EOF
fi

# Tell Claude's box session the browser exists; the MCP tools advertise
# themselves, the CLI form does not. Codex gets the same note through its
# tracked AGENTS.md (config/codex-global-AGENTS.md).
claude_md="${PERSON_HOME}/.claude/CLAUDE.md"
if [[ -f "${claude_md}" ]] && ! grep -qF 'obscura:begin' "${claude_md}"; then
  cat >>"${claude_md}" <<'EOF'

<!-- obscura:begin (managed by scripts/install-obscura.sh) -->
## Browser

`obscura` — a headless browser, no Chromium — is on PATH: `obscura fetch <url> --dump text`,
`--eval "<js>"`, `-s page.png` for screenshots, `--allow-private-network` for local dev servers.
It is also registered as the `obscura` MCP server, and a CDP endpoint for Playwright/Puppeteer
scripts listens on `127.0.0.1:9222`.
<!-- obscura:end -->
EOF
  chown "${PERSON_USER}:${PERSON_USER}" "${claude_md}"
fi

if ! as_user timeout 60 /usr/local/bin/obscura fetch https://example.com \
  --eval "document.title" >/dev/null 2>&1; then
  echo "WARNING: fetch smoke test failed — check the box's network" >&2
fi
echo "obscura ${OBSCURA_VERSION} installed: CLI, CDP unit, MCP in claude/qoder/codex."
