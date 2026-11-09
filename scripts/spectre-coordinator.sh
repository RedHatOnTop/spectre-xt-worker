#!/bin/bash
set -euo pipefail

if [[ $# -eq 1 && "$1" == /* && -f "$1" && ! -L "$1" ]]; then
  args=("$(cat "$1")")
elif [[ $# -eq 2 && "$1" == resume && "$2" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ ]]; then
  args=(resume "$2" --model gpt-6.1-sol)
else
  printf '%s\n' 'Usage: spectre-coordinator /absolute/prompt.md | resume <session-uuid>' >&2
  exit 2
fi
if [[ -z "${ORCA_TERMINAL_HANDLE:-}" || -z "${ORCA_PANE_KEY:-}" ]]; then
  printf '%s\n' 'Launch this coordinator inside a registered Orca terminal.' >&2
  exit 2
fi
state="${HOME}/.local/state/remote-agent/coordinators"
umask 077
mkdir -p "${state}"
identity="$(python3 -c 'import hashlib,os;print(hashlib.sha256(os.getcwd().encode()).hexdigest())')"
# Direct Agentrouter B. The overrides drop inherited ChatGPT-account routing
# and desktop surfaces (apps, browser, computer use). The mount is visible only
# to this process: the global AGENTS.md tells the model to answer in Korean and
# advertises a browser CDP endpoint, and either one is enough for the relay to
# reject the next turn. Do not put a proxy back on this command.
cmd=(
  flock --nonblock --conflict-exit-code 75 "${state}/${identity}.lock"
  "${HOME}/.local/bin/codex" --no-daemon --profile orca-agentrouter-b
  -c check_for_update_on_startup=false
  -c forced_login_method='"api"'
  -c cli_auth_credentials_store='"ephemeral"'
  -c service_tier='""'
  -c project_doc_max_bytes=0
  -c include_apps_instructions=false
  -c include_collaboration_mode_instructions=false
  -c features.multi_agent=false
  -c features.apps=false
  -c features.browser_use=false
  -c features.browser_use_external=false
  -c features.browser_use_full_cdp_access=false
  -c features.computer_use=false
  -c features.in_app_browser=false
  -c features.in_app_chat=false
  -c features.in_app_dictation=false
  -c features.in_app_local_automation=false
  -c features.in_app_updates=false
  -c features.image_generation=false
  -c features.goals=false
  -c mcp_servers.context7.enabled=false
  -c mcp_servers.exa.enabled=false
  -c mcp_servers.obscura.enabled=false
  -c mcp_servers.sequential-thinking.enabled=false
  "${args[@]}"
)
agents="${HOME}/.codex/AGENTS.md"
overlay="${HOME}/.codex/AGENTS.agentrouter-b.md"
if [[ -f "${agents}" ]]; then
  if [[ ! -s "${overlay}" ]]; then
    printf '%s\n' 'Missing the Agentrouter B AGENTS overlay.' >&2
    exit 2
  fi
  exec unshare --user --map-root-user --mount bash -c \
    'set -euo pipefail; mount --bind -- "$1" "$2"; shift 2; exec "$@"' \
    bash "${overlay}" "${agents}" "${cmd[@]}"
fi
exec "${cmd[@]}"
