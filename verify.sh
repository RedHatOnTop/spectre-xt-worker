#!/bin/bash
# Single verification entry point for this repo (Verification Protocol).
# Runs: shell lint, python compile + unit tests. Exits non-zero on any failure.
set -uo pipefail

cd "$(dirname "$0")"

failed=0
section() { printf '\n== %s ==\n' "$1"; }

section "shell syntax (bash -n)"
for f in scripts/*.sh install.sh verify.sh; do
  if bash -n "${f}"; then
    echo "ok    ${f}"
  else
    echo "FAIL  ${f}"
    failed=1
  fi
done

section "shellcheck"
if command -v shellcheck >/dev/null 2>&1; then
  if shellcheck -S style scripts/*.sh install.sh; then
    echo "ok    shellcheck clean"
  else
    echo "FAIL  shellcheck findings above"
    failed=1
  fi
else
  echo "SKIP  shellcheck not installed (apt/dnf install shellcheck)"
fi

section "python compile"
if python3 -m py_compile scripts/*.py scripts/worker_state/*.py scripts/control_plane/*.py scripts/dsh-clinepass scripts/mimo-clinepass scripts/spectre-astra; then
  echo "ok    py_compile"
else
  echo "FAIL  py_compile"
  failed=1
fi

section "node (slack bridge)"
if command -v node >/dev/null 2>&1; then
  if node --check scripts/slack-bridge.mjs; then
    echo "ok    node --check slack-bridge.mjs"
  else
    echo "FAIL  node --check slack-bridge.mjs"
    failed=1
  fi
  bridge_out="$(node --test tests/*.test.mjs 2>&1)"
  rc=$?
  if [[ ${rc} -ne 0 ]]; then
    echo "${bridge_out}"
  else
    echo "${bridge_out}" | tail -8
  fi
  if [[ ${rc} -ne 0 ]]; then
    echo "FAIL  slack bridge tests"
    failed=1
  else
    echo "ok    slack bridge tests"
  fi
else
  echo "SKIP  node not installed (bridge runtime)"
fi

section "node (devcodex, vendored)"
if command -v node >/dev/null 2>&1; then
  dcx_out="$(node --test devcodex/test/*.test.js 2>&1)"
  rc=$?
  if [[ ${rc} -ne 0 ]]; then
    echo "${dcx_out}"
  else
    echo "${dcx_out}" | tail -8
  fi
  if [[ ${rc} -ne 0 ]]; then
    echo "FAIL  devcodex tests"
    failed=1
  else
    echo "ok    devcodex tests"
  fi
else
  echo "SKIP  node not installed (devcodex runtime)"
fi

section "unit tests"
test_out="$(python3 -m unittest discover -s tests 2>&1)"
rc=$?
if [[ ${rc} -ne 0 ]]; then
  echo "${test_out}"
  echo "FAIL  unit tests"
  failed=1
elif ! echo "${test_out}" | grep -q "^OK"; then
  echo "FAIL  unit tests did not report OK"
  failed=1
else
  echo "ok    $(echo "${test_out}" | grep -oE 'Ran [0-9]+ tests')"
fi

printf '\n'
if (( failed )); then
  echo "verify: FAILED"
  exit 1
fi
echo "verify: all gates passed"
