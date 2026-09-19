# 2026-08-25 — git policy for the box + session-sync via private repo

## Hotfix: smartd crash in bootstrap (2026-08-25)

User hit it on the real box: install.sh died with a smartd error.
Root cause: my earlier log-caps pass added `smartd.service` to the
`systemctl enable --now` line but never added `smartmontools` to the
apt install list — that package was only installed later by
harden-network.sh (wizard stage 4). Fresh box + `set -e` = bootstrap
dead at line 95. Fixed both ways: smartmontools now in the apt list,
and smartd enablement is conditional (list-unit-files check) with a
loud warning instead of killing the remaining setup. USB copy
hot-patched; re-run of bootstrap is safe (idempotent by design).

## Ask

Two-part user decision:
1. Box agent git permissions, explicitly: commit/push/PR create+review/
   branch create allowed; merge and ALL deletion (branches, commits,
   history) forbidden.
2. Should ZCode sessions sync through a private GitHub repo too? Yes —
   but imports must NEVER overwrite; incoming history must always land
   as new sessions.

## Guard changes (BOX_DESTRUCTIVE)

Added by policy: git merge, rebase, reset --hard, clean -f,
branch/tag -d/-D, push --delete / :ref, filter-branch/repo,
reflog expire / gc --prune=now, gh pr/issue close|delete.
Allowed (tested): commit, push (non-force), switch -c, gh pr create,
gh pr review. Smoke suite grew to 29 cases; all pass.

Note: `git pull` stays allowed (fetch+ff merge is how the box tracks),
but `pull --rebase/--squash` is blocked.

## --as-new import (warp_zcode.py)

import_sessions gained `as_new`: every imported session gets
`<id8>-replay-<hex12>`, message ids get `-r<hex8>` suffixes so part →
message references stay consistent, and INSERT OR REPLACE therefore
only ever inserts new rows — existing local rows are unreachable by
construction. Test: importing over an identical session id leaves the
local copy's data intact and adds one replay session. 17/17 unit tests.

session-sync.sh: push = export snapshot for cwd into
~/.local/share/zcode-session-sync (private clone), append-only commit +
plain push. pull = ff-only pull, then import EVERY snapshot with
--as-new; refuses while ZCode is running (no background wait here —
explicit re-run). Config in ~/.config/remote-agent/session-sync.env
(SESSION_SYNC_REPO), example file added. Installed as
/usr/local/bin/session-sync by bootstrap; resolves warp_zcode.py from
the repo checkout or the spectre-warp lib dir.

## Honest limits

--as-new deduplicates nothing: pulling twice creates two replays.
Acceptable for a fallback channel; warp remains the primary path.
The guard's text matching still cannot catch indirection through a
script — same honest limit as before, unchanged.

## Docs

RUNBOOK §7.6 (session sync + explicit box git policy), USB-GUIDE section
for both. Guard vendored copy refreshed; USB re-synced (verified
bootstrap mentions session-sync and warp_zcode has as_new).
