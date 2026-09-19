#!/usr/bin/env bash
# agy-handoff — CodexPro execute/watch-handoff executor for the Antigravity CLI.
#
# Contract (called by `codexpro execute-handoff --agent custom --command
# "... agy-handoff {{plan_file}} {{root}}"`):
#
#   agy-handoff <plan_file> <root>
#
# CodexPro captures this script's stdout into .ai-bridge/agent-status.md
# (bounded by its --max-output-bytes) and marks handoff-run-state.json
# failed when the script exits non-zero.
#
# Output policy:
#   stdout  the agy response text only (what ChatGPT reviews)
#   stderr  diagnostics
#   /work/logs/agy-handoff.log        one raw JSON envelope line per run
#   /work/logs/agy-handoff.stderr.log agy diagnostics, timestamped
#
# Security: agy runs under the scoped allowlist in
# ~/.gemini/antigravity-cli/settings.json (see
# config/agy-settings.example.json). Anything outside the allowlist is
# soft-denied in headless mode — that is this box's guard for this
# worker. Never add --dangerously-skip-permissions here.
set -u

if [[ $# -ne 2 ]]; then
  echo "usage: agy-handoff <plan_file> <root>" >&2
  exit 2
fi
plan_file=$1
root=$2

die() { echo "agy-handoff: $*" >&2; exit 1; }

[[ -f "${plan_file}" ]] || die "plan file not found: ${plan_file}"
[[ -d "${root}" ]] || die "root is not a directory: ${root}"
command -v agy >/dev/null 2>&1 || die "agy not on PATH (install the Antigravity CLI)"
command -v jq >/dev/null 2>&1 || die "jq not on PATH"

log_dir="${AGY_HANDOFF_LOG_DIR:-/work/logs}"
log_file="${log_dir}/agy-handoff.log"
err_file="${log_dir}/agy-handoff.stderr.log"
if ! mkdir -p "${log_dir}" 2>/dev/null; then
  log_file="${HOME}/agy-handoff.log"
  err_file="${HOME}/agy-handoff.stderr.log"
fi

cd "${root}" || die "cannot cd ${root}"

plan_text="$(cat "${plan_file}")"
[[ -n "${plan_text}" ]] || die "plan file is empty: ${plan_file}"

# CodexPro's execute-handoff default timeout is 600000 ms; agy must give
# up first so the failure lands in agent-status.md instead of a silent
# process kill.
print_timeout="${AGY_PRINT_TIMEOUT:-9m}"

rc=0
envelope="$(agy -p "${plan_text}" --output-format json --print-timeout "${print_timeout}" 2>>"${err_file}")" || rc=$?

if [[ -n "${envelope}" ]]; then
  printf '%s\n' "${envelope}" >> "${log_file}"
fi

if (( rc != 0 )); then
  die "agy exited rc=${rc} (diagnostics: ${err_file})"
fi

status="$(printf '%s' "${envelope}" | jq -r '.status // "ERROR"' 2>/dev/null || echo ERROR)"
if [[ "${status}" != "SUCCESS" ]]; then
  error="$(printf '%s' "${envelope}" | jq -r '.error // "no error field"' 2>/dev/null || echo unknown)"
  die "agy run status=${status}: ${error}"
fi

printf '%s' "${envelope}" | jq -rj '.response // ""'
