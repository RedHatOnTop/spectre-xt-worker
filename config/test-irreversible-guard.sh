#!/bin/bash
# Smoke test for irreversible-guard.mjs profiles. Pipes PreToolUse-style
# payloads at the guard and checks allow (exit 0) / block (exit 2).
#
#   bash test-irreversible-guard.sh [guard.mjs]
set -u
GUARD="${1:-$HOME/.claude/hooks/irreversible-guard.mjs}"
RUNNER="$(mktemp /tmp/guard-runner-XXXXXX.mjs)"
cat >"${RUNNER}" <<'EOF'
import { spawnSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
const guard = process.argv[2];
const payload = JSON.stringify({
  tool_name: 'Bash',
  tool_input: { command: readFileSync(0, 'utf8') },
});
const r = spawnSync('node', [guard], { input: payload });
process.exit(r.status ?? 0);
EOF

# Write-class payloads, which take a file_path instead of a command.
WRITE_RUNNER="$(mktemp /tmp/guard-write-runner-XXXXXX.mjs)"
cat >"${WRITE_RUNNER}" <<'EOF'
import { spawnSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
const guard = process.argv[2];
const payload = JSON.stringify({
  tool_name: 'Write',
  tool_input: { file_path: readFileSync(0, 'utf8').trim() },
});
const r = spawnSync('node', [guard], { input: payload });
process.exit(r.status ?? 0);
EOF

FAILED=0
run_case() {
  local profile="$1" expect="$2" desc="$3" cmd="$4"
  local rc verdict
  if [[ -n "${profile}" ]]; then
    printf '%s' "${cmd}" | SPECTRE_WORKER_PROFILE="${profile}" \
      node "${RUNNER}" "${GUARD}" >/dev/null 2>&1
  else
    printf '%s' "${cmd}" | node "${RUNNER}" "${GUARD}" >/dev/null 2>&1
  fi
  rc=$?
  verdict="ALLOW"
  [[ ${rc} -eq 2 ]] && verdict="BLOCK"
  if [[ "${verdict}" == "${expect}" ]]; then
    echo "PASS  [${profile:-desktop}] ${desc}: ${verdict}"
  else
    echo "FAIL  [${profile:-desktop}] ${desc}: expected ${expect}, got ${verdict} (rc=${rc})"
    FAILED=1
  fi
}

run_write_case() {
  local profile="$1" expect="$2" desc="$3" path="$4"
  local rc verdict
  if [[ -n "${profile}" ]]; then
    printf '%s' "${path}" | SPECTRE_WORKER_PROFILE="${profile}" \
      node "${WRITE_RUNNER}" "${GUARD}" >/dev/null 2>&1
  else
    printf '%s' "${path}" | node "${WRITE_RUNNER}" "${GUARD}" >/dev/null 2>&1
  fi
  rc=$?
  verdict="ALLOW"
  [[ ${rc} -eq 2 ]] && verdict="BLOCK"
  if [[ "${verdict}" == "${expect}" ]]; then
    echo "PASS  [${profile:-desktop}] ${desc}: ${verdict}"
  else
    echo "FAIL  [${profile:-desktop}] ${desc}: expected ${expect}, got ${verdict} (rc=${rc})"
    FAILED=1
  fi
}

echo "== desktop profile (unchanged behavior) =="
run_case ""      BLOCK "rm -rf"                    "rm -rf /home/person/tmp/x"
run_case ""      BLOCK "git push --force"          "git push --force origin main"
run_case ""      BLOCK "mkfs"                      "sudo mkfs.ext4 /dev/sdb1"
run_case ""      ALLOW "normal build"              "cargo build --release"

echo
echo "== box profile =="
run_case box     ALLOW "rm -rf allowed"            "rm -rf /work/npm-cache"
run_case box     ALLOW "mkfs/parted allowed"       "sudo mkfs.ext4 -F -L SPECTREWORK /dev/sda2"
run_case box     ALLOW "docker prune allowed"      "docker system prune -a -f"
run_case box     ALLOW "shred allowed"             "shred /work/logs/old.log"
run_case box     BLOCK "force push still blocked"  "git push --force origin main"
run_case box     BLOCK "remote ref delete blocked" "git push origin --delete feature-x"
run_case box     BLOCK "merge blocked by policy"   "git merge feature-x"
run_case box     BLOCK "rebase blocked"            "git rebase main"
run_case box     BLOCK "branch -D blocked"         "git branch -D feature-x"
run_case box     BLOCK "branch -d blocked"         "git branch -d old-thing"
run_case box     BLOCK "tag deletion blocked"      "git tag -d v1.2"
run_case box     BLOCK "reset --hard blocked"      "git reset --hard HEAD~1"
run_case box     BLOCK "gh pr close blocked"       "gh pr close 42"
run_case box     BLOCK "publish still blocked"     "npm publish"
run_case box     ALLOW "commit allowed"            "git commit -m 'fix the thing'"
run_case box     ALLOW "push allowed"              "git push origin worker/feature"
run_case box     ALLOW "branch create allowed"     "git switch -c worker/new-task"
run_case box     ALLOW "PR create allowed"         "gh pr create --title t --body b"
run_case box     ALLOW "PR review allowed"         "gh pr review 7 --approve"
run_case box     ALLOW "plain pull allowed"        "git pull --ff-only"
run_case box     BLOCK "dd whole-disk blocked"     "dd if=img.iso of=/dev/nvme0n1"
run_case box     ALLOW "dd to partition ok"        "sudo dd if=boot.img of=/dev/sda3"
run_case box     BLOCK "shutdown blocked"          "sudo shutdown -h now"
run_case box     BLOCK "poweroff via systemctl"    "systemctl poweroff"
run_case box     ALLOW "systemctl restart service" "systemctl restart nginx"
run_case box     BLOCK "guard bypass still blocked" "claude --dangerously-skip-permissions"
run_case box     BLOCK "config write still blocked" "echo x > ~/.claude/settings.json"
run_case box     ALLOW "warp push is normal work"  "warp push"

echo
echo "== write-class tools =="
run_write_case box  BLOCK "claude settings write blocked" "$HOME/.claude/settings.json"
run_write_case box  BLOCK "hooks write blocked"           "$HOME/.claude/hooks/x.mjs"
run_write_case box  ALLOW "normal file write"             "/work/person/notes.md"

rm -f "${RUNNER}" "${WRITE_RUNNER}"
echo
if (( FAILED )); then
  echo "RESULT: FAILURES ABOVE"
  exit 1
fi
echo "RESULT: all cases passed"
