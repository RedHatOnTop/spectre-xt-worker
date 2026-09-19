# 2026-08-24 — box profile for irreversible-guard + final USB bundle

## Ask

User: the Spectre is a separate, disposable box — loosen agent
permissions so everything is allowed except genuinely irreversible
damage ("irreversible-guard가 리모트를 건드리는 등").

## What changed

`~/.claude/hooks/irreversible-guard.mjs` gained a profile switch:
`SPECTRE_WORKER_PROFILE=box` swaps the DESTRUCTIVE list for
BOX_DESTRUCTIVE. The guard's self-protection (BYPASS rules,
PROTECTED_SHAPE writes, override-window logic) is identical in both
profiles and evaluated before the switch — an env var can never become
a guard-off switch.

Box profile still blocks (true irreversibles):
- force push / remote ref deletion / history rewrite (the other machine
  and GitHub hold the only other copy)
- package publishes, gh repo/release deletion
- dd over a WHOLE disk device (`/dev/sda`, `/dev/nvme0n1`) — partitions
  stay writable because setup-disks legitimately formats /work; stray
  `> /dev/*` redirects stay blocked
- shutdown/poweroff/halt incl. via systemctl — a headless lid-closed
  box has nobody to press power afterwards
- curl|sh

Box profile now allows without override window: rm -rf anywhere,
mkfs/parted (setup-disks needs them), docker prune/volume rm, shred,
git reset --hard/clean/branch -D on local repos (git+warp mirror the
work), registry items.

Desktop behavior with the var unset is byte-identical to before
(verified by the same test cases against both profiles).

## Testing

New `hooks/test-irreversible-guard.sh` (16 cases): desktop blocks its
classics, box allows the appliance work, box still blocks remotes /
whole-disk / power-off / guard bypass / config writes. First run had a
runner bug (nested heredoc swallowed stdin → rc=1 masquerading as
ALLOW) — rewrote the runner to spawn the guard via a temp .mjs file.
Then two real regex bugs surfaced: `\bhalt\b\b?` (invalid quantifier,
syntax error) and the nvme whole-disk pattern missing `nvme0n1` (needs
`nvme\d+n\d+(?![a-z0-9])`, since `\b` after "nvme" fails at the digit).
Final: 16/16 PASS.

## Deployment path

- fedora (this machine): hook updated in place; env var NOT set here.
- Repo vendors `config/irreversible-guard.mjs` + the test +
  `config/spectre-claude-settings.json` (bypassPermissions + deny list
  of secret files + PreToolUse wiring to the vendored hook).
- bootstrap installs both into the user's ~/.claude/hooks and appends
  `SPECTRE_WORKER_PROFILE=box` to /etc/environment so every session on
  the box gets the profile. settings.json is only written if absent —
  never overwrites an existing config.

## USB

Re-synced twice: once after warp v2, once after the guard work.
`spectre-worker-guide.md` at stick root replaced the earlier
Korean-named copy; guide source of truth is now `USB-GUIDE.md` in-repo.
Stick also holds unrelated personal data — never touched.
