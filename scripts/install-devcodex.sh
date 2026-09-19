#!/bin/bash
# Install devcodex — the single-agent workspace companion (durable task
# sessions, code navigation, evidence-backed completion) — for the worker
# user: the vendored tree under /usr/local/share/devcodex and `devcodex`
# on PATH, plus a note for Claude Code. Idempotent.
#
#   sudo bash scripts/install-devcodex.sh [person]
#
# The tree is vendored from the ChatGPT-built project on the daily driver
# (~/Projects/devspace-demo/devcodex), version 1.0.0-rc.1, vendored
# 2026-09-14; edited in-repo 2026-09-16 and 2026-09-17 for the box posture
# (every permission action allows — named explicitly, unnamed via the allow
# fallback — completion passes without configured gates, and the
# write_file/edit_file/run_command tools join the core surface; see
# devcodex/src/permission.js, devcodex/src/orchestrator.js,
# devcodex/src/fsops.js). Upstream is not edited: the box tree and the
# daily-driver tree now differ on purpose, so re-vendoring must re-apply this
# posture and re-pin.
# DEVDCODEX_TREE_SHA256 pins the vendored file set — it is the
# sha256 of `find . -type f -print0 | LC_ALL=C sort -z | xargs -0 sha256sum
# | sha256sum` inside devcodex/. The C collation is deliberate: a locale
# sort made the same tree hash differently on the box (POSIX) than on the
# daily driver (UTF-8) on first deploy. Re-vendoring and editing the tree
# in-repo both update the pin together, so the installer fails on an
# unrecorded drift instead of shipping it.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PERSON_USER="${1:-${SUDO_USER:-person}}"
PERSON_HOME="$(getent passwd "${PERSON_USER}" | cut -d: -f6)"
if [[ -z "${PERSON_HOME}" ]]; then
  echo "no home for ${PERSON_USER}" >&2
  exit 1
fi

DEVDCODEX_VERSION="1.0.0-rc.1"
DEVDCODEX_TREE_SHA256="df491a0e9f97f026bd50f77e972e50aec5dc0bc5e1fd904671daa7f43fefba46"
DEVCODEX_DIR="/usr/local/share/devcodex"

SRC="${REPO_DIR}/devcodex"
if [[ ! -f "${SRC}/package.json" ]]; then
  echo "vendored tree missing at ${SRC} — run from a full repo checkout" >&2
  exit 1
fi

tree_hash="$(cd "${SRC}" && find . -type f -print0 | LC_ALL=C sort -z | xargs -0 sha256sum | sha256sum | cut -d' ' -f1)"
if [[ "${tree_hash}" != "${DEVDCODEX_TREE_SHA256}" ]]; then
  echo "vendored tree hash mismatch:" >&2
  echo "  expected ${DEVDCODEX_TREE_SHA256}" >&2
  echo "  actual   ${tree_hash}" >&2
  echo "re-vendoring? update DEVDCODEX_TREE_SHA256 in this script together." >&2
  exit 1
fi

command -v node >/dev/null 2>&1 || { echo "node not found — run bootstrap first" >&2; exit 1; }
node -e 'const major = Number(process.versions.node.split(".")[0]); process.exit(major >= 20 ? 0 : 1)' || {
  echo "node >= 20 required, found $(node --version)" >&2
  exit 1
}

as_user() { runuser -u "${PERSON_USER}" -- "$@"; }

# Stage and swap so a failed copy never leaves a half-written tree in place.
stage="$(mktemp -d)"
trap 'rm -rf "${stage}"' EXIT
install -d -m 0755 "${stage}/devcodex"
cp -a "${SRC}/src" "${SRC}/docs" "${SRC}/test" "${SRC}/package.json" "${SRC}/README.md" \
  "${stage}/devcodex/"
chown -R root:root "${stage}/devcodex"
rm -rf "${DEVCODEX_DIR}"
mv "${stage}/devcodex" "${DEVCODEX_DIR}"
chmod 0755 "${DEVCODEX_DIR}"

cat > "${stage}/devcodex-bin" <<'EOF'
#!/bin/sh
exec node /usr/local/share/devcodex/src/cli.js "$@"
EOF
install -m 0755 "${stage}/devcodex-bin" /usr/local/bin/devcodex

# The CLI form works from any workspace cwd; the MCP form is one server per
# workspace (`devcodex mcp --root <path>`) and is registered per repo, not
# here (RUNBOOK 7.13).
claude_md="${PERSON_HOME}/.claude/CLAUDE.md"
if [[ -f "${claude_md}" ]] && ! grep -qF 'devcodex:begin' "${claude_md}"; then
  cat >>"${claude_md}" <<'EOF'

<!-- devcodex:begin (managed by scripts/install-devcodex.sh) -->
## Workspace companion

`devcodex` — durable task sessions, code navigation, workspace-scoped
writes and commands, and evidence-backed completion. Run it from the repo
you are working in (or `--root <path>`): `devcodex bootstrap "<task>"`
starts/reuses a task session, `devcodex session-note <id> <type> <message>`
records decisions, `devcodex write` / `edit` / `run` change and exercise the
workspace, `devcodex verify` runs the repo's gates from `.devcodex.json`,
`devcodex complete --session <id>` closes the task once review passes (gates
run when the repo configures them). State lives in `.devcodex/` inside the
workspace.
<!-- devcodex:end -->
EOF
  chown "${PERSON_USER}:${PERSON_USER}" "${claude_md}"
fi

# Smoke as the worker user: a bootstrap against a scratch git workspace
# proves node, the deployed tree, and user-writable runtime state in one
# shot. bootstrap reads git state, so the scratch dir needs a repo (one
# empty commit is enough) — without it bootstrap hard-fails on `git diff`.
smoke_dir="$(as_user mktemp -d)"
if as_user bash -c "cd '${smoke_dir}' && git init -q && git -c user.email=smoke@local -c user.name=smoke commit -q --allow-empty -m smoke && timeout 60 devcodex bootstrap 'install smoke' --root '${smoke_dir}'" >/dev/null; then
  echo "smoke: bootstrap ok"
else
  echo "WARNING: devcodex bootstrap smoke failed — inspect /usr/local/share/devcodex" >&2
fi
as_user rm -rf "${smoke_dir}"

echo "devcodex ${DEVDCODEX_VERSION} installed: /usr/local/share/devcodex + /usr/local/bin/devcodex."
