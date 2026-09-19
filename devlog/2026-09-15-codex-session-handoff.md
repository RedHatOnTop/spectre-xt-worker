# 2026-09-15 — Codex session handoff: hand the conversation to the Spectre

## Requirement

User: "필요 시 즉시 Codex 세션을 스펙터로 인계할 수 있도록 설정좀 — 이미 꽤나
설정 돼있었을건데 여러번 시간부족으로 중간에 중단했거든 네가 마저 끝내줘."
The Codex-on-the-box work (§7.11: pinned CLI, box config, codex-mode, key
transfer, read-guard) was already in place from 2026-09-14. What was
missing was the part that makes it *usable*: moving a session itself, so a
conversation started on the Zenbook continues on the 24/7 box with one
command. This finishes that.

## What a Codex session actually is (measured, not assumed)

Everything here rests on probes on the daily driver, codex-cli 0.154.0:

- **A session is one or more files.** `$CODEX_HOME/sessions/<Y>/<M>/<D>/
  rollout-<ts>-<uuid>.jsonl`, and a long session is *paginated* into
  `rollout-<ts>-<uuid>_<shard-uuid>.jsonl` shards whose first-line
  `ordinal` continues across files (local sample: a 2309-record session in
  3 files, ordinals 0 → 1026 → 1028). Copying only the newest file would
  silently truncate history, which is why the tool groups shards by id.
- **The rollout alone is enough for codex to find it.** Probe: copy one
  rollout into a fresh `CODEX_HOME` (no index, no DB, no config) and run
  `codex delete <uuid> --force` → "Deleted session <uuid>"; the same
  command with a bogus uuid → "failed to delete session". So a handoff
  needs no export format and no index merge — it is a file copy.
- **`session_index.jsonl` is not the source of truth** (61 entries for 216
  local sessions), so it is deliberately not copied or rewritten.
- **The first "user" record is usually injected context**, not something
  the user typed: 215 of 216 local rollouts open with `<recommended_plugins>`,
  `# AGENTS.md instructions`, or `<environment_context>`. The label in
  `list` therefore skips those and falls back to the session's recorded
  git branch (present in `session_meta.payload.git` for most sessions).
- **`codex exec` sessions are the majority** on the Zenbook (123 of 216)
  and codex hides them from the resume picker, so selection mirrors codex's
  own `--include-non-interactive` flag instead of inventing a new one.

## What was built

- `scripts/codex_rollout.py` — stdlib-only helper: groups shards per
  session, extracts id/cwd/originator/source/git branch/ordinal, hashes
  each shard, and resolves a session from an id/unique prefix (exact uuid
  beats a longer prefix match; an ambiguous prefix lists its candidates).
- `scripts/codex-handoff.sh` — `push` / `pull` / `list` / `status`, peer
  resolution and ssh conventions borrowed from `warp.sh`. Safety model
  copied from `session-sync`/warp: additive, `*.incoming` + `mv` so a
  dropped link never leaves a half-file, a differing copy of the same id is
  a fork point refused with exit 3 unless `--replace` (which backs the
  destination up in place), nothing is ever deleted, and the peer's `$HOME`
  is verified before any write. Prints the exact resume line, and `--open`
  runs it (tmux + `. ~/.codex/modes/env.sh`, because `ssh host 'cmd'` is
  neither interactive nor a login shell — §7.11).
- `tests/test_codex_rollout.py` (19 tests) and `tests/test_codex_handoff.py`
  (26 tests, driving the sourced bash helpers, so no peer is required).
- Wiring: `install-warp.sh` (Zenbook + box, one command), `bootstrap.sh`
  (`/usr/local/bin/codex-handoff`, helper under `/usr/local/lib/spectre-codex/`),
  a `spectre-doctor` section, README line, RUNBOOK §7.15 + gate lines.
- Agent guidance, so the box's agents find the command the same way they
  find obscura and devcodex: `install-codex.sh` now appends a
  `codex-handoff:begin/end` block to `~/.claude/CLAUDE.md` (once,
  append-only, local edits survive), and its existing install of
  `config/codex-global-AGENTS.md` carries the "Session handoff" section into
  `~/.codex/AGENTS.md`. On the daily driver, where no installer owns those
  files, the same note went in by hand — a section in `~/.codex/AGENTS.md`
  and a managed block in `~/.claude/CLAUDE.md`, both with a `.bak-20260915`
  copy beside them.

## Two bugs the tests caught

`classify_shards` treated a shard reported as `missing` as a
present-but-different file, so a first-time push would have been classed
"differs" and refused as a fork point. The first `python3 -m unittest`
run failed on exactly that; the state parser now ignores `missing` lines.
Without the bash-level tests this would have shipped as "handoff never
works the first time".

The printed resume line was not paste-safe: it embedded `cd '<path>'`
(single quotes) inside a single-quoted ssh argument, so the quotes
cancelled and the path ended up in an *unquoted* shell context — a path
with a space or `$(...)` would have broken or executed on the sender. The
directory now travels as `tmux new-session -c "<path>"`, leaving one
command string with no nested quote style. A test eval's the printed line
through two simulated shell levels (with `ssh`/`tmux` stubbed) and asserts
that tmux receives `-c`, the directory and the command as separate clean
arguments.

## Verification (fedora, 2026-09-15)

```
python3 -m unittest tests.test_codex_rollout tests.test_codex_handoff   # 45 tests, OK
bash verify.sh                          # all gates passed (whole suite green)
bash -n scripts/codex-handoff.sh scripts/doctor.sh scripts/bootstrap.sh scripts/install-warp.sh
shellcheck -S style scripts/codex-handoff.sh     # clean (shellcheck 0.11.0, run from /tmp)
codex-handoff list --all --limit 3      # 214 sessions, 1 grouped from 3 shards
codex-handoff list --all --json         # 214 objects (matches the deduped count)
codex-handoff status                    # host/user/helper/peer, 214 local, ledger path
codex-handoff push --nope               # unknown option -> exit 1, before any ssh
codex-handoff push zzzzzzzz             # "no session matches", exit 1, no ssh
bash scripts/install-warp.sh            # codex-handoff on PATH on the Zenbook
doctor's provider grep re-escaped       # PASS with the block, FAIL without it
```

The 26 bash-layer tests stub the peer, so the copy *sequence* is verified
even though the two machines are not both reachable: a push traces
`install -d -m 0700`, `scp` of each shard to `<dst>.incoming`, `mv -f` into
place, then the ledger and the remote resume line; a pull does the same in
reverse; a fork point exits 3 with `scp` never running and the local file
untouched. `verify.sh` reports shellcheck as SKIP here (not installed on
the daily driver), so it was fetched to /tmp and run at the repo's
`-S style` level for the new script; the repo has four pre-existing style
findings elsewhere (doctor.sh 3× SC2088 in message strings/a case pattern,
install-obscura.sh 1× SC2012) that this change neither adds to nor fixes.

The suite total moved while this was being written: another agent was
adding `scripts/goal-supervisor.py` and `tests/test_goal_supervisor.py` to
this same repo in the same window, so the whole-suite count is a
timestamped observation rather than a baseline. The 45 tests added here
are the ones counted above.

## On the box, end to end (2026-09-15 22:24–22:31 KST)

The Tailscale SSH 24 h check was re-approved, so the transfer path itself
was then measured rather than assumed:

- Installed on the box: `codex-handoff` at `/usr/local/bin`, the helper at
  `/usr/local/lib/spectre-codex/codex_rollout.py`, the new `spectre-doctor`,
  the refreshed `~/.codex/AGENTS.md`, and `install-codex.sh` re-run there
  (rc=0) which appended the `~/.claude/CLAUDE.md` block.
- `tests/test_codex_handoff.py` + `tests/test_codex_rollout.py`: **45/45 on
  the box** — the first box run failed one test (see below), which is how
  the SIGPIPE bug was found.
- Throwaway session on the Zenbook (`codex exec 'reply with exactly:
  handoff-ping'`, so no real work was involved) → `codex-handoff push
  --include-non-interactive`: one shard copied, and the box's copy matched
  the sender's `sha256` (`48c3bcd7…002d`) byte for byte.
- Re-running the same push: "identical copy on the other side — nothing to
  copy", exit 0.
- On the box, `codex exec resume <uuid> "reply with exactly: handoff-pong"`
  loaded the session (`session id: 01a0a53c…`, transcript replayed) and
  appended the new turn — the box's rollout grew 48018 → 71685 B. That is
  the handoff working: the box continued a Zenbook conversation.
- `codex-handoff pull` afterwards: exit 3, "already exists here with
  different content … that is a fork point", local file untouched at
  48018 B. `--replace` then took the box's copy and wrote the local backup
  `….jsonl.bak-20260915T133043Z`.
- `codex-handoff list --peer spectre` works from the Zenbook, and the box's
  `codex-handoff list --peer fedora` works in the other direction.
- `sudo spectre-doctor` on the box: **55 passed, 0 failed**, with the section
  "codex CLI + session handoff" all green.
- `~/.local/state/spectre-codex-handoff/handoff.jsonl` on the Zenbook holds
  the three records (push, push, pull) with id, label, shard and record
  counts.

Relay note, unrelated to the handoff: in the same window the Zenbook's
active provider (`temp-zenllm`) answered 401 Unauthorized and the box's
anyrouter answered "high demand" — both turns failed at the API, while the
sessions were created and resumed and their rollouts grew. Worth a look
separately: two of the user's relay keys look unhealthy.

The throwaway session was deleted on both machines afterwards, so nothing
of this is left in either picker.

## What the box itself caught

Four things only the second machine could show:

- **SIGPIPE between the two shells.** `remote_shard_state` piped the shard
  list into `ssh`; the first box run failed a test with exit 141 (128+13).
  On the Zenbook the write always won the race; on the box the reader
  exited first and `pipefail` aborted the whole handoff. It is a here-string
  now (`<<<`), and the test's ssh stub reads stdin the way the real peer
  loop does, so the data path is covered too.
- **The box's checkout is committed-files-only.** `/opt/spectre-xt-worker`
  is a git clone, and this repo has been uncommitted since 09-12, so the
  09-14 Codex files (`config/codex-instructions.md`, `read-guard.mjs`,
  `spectre-codex-config.toml`, `scripts/codex-mode`) were never in it —
  `install-codex.sh` failed on the first missing file. They were copied over
  by hand (`scp` + `tar`) and the installer then ran clean (rc=0). Recorded
  in RUNBOOK §7.15; the real fix is pushing the repo.
- **A permission-shaped doctor check.** `command -v codex-mode` fails as
  root because the binary is user-level in `~/.local/bin`; the check now
  tests the installed path as the user, the pattern the executor check
  already uses. Box doctor: 55 passed, 0 failed.
- **A peer-side lookup that was one directory short.** `install-warp.sh`
  puts the helper in `lib/spectre-warp/`, which the peer lookup did not
  search, so the box→fedora call silently dropped a second copy into the
  cache. Both warp locations are in the list now; verified by deleting the
  cache copy and re-running the call (works, no copy re-created).

## Honest limits

- **Snapshot, not attach.** A still-open session keeps diverging locally;
  the receiver continues from the copied transcript.
- **No "as-new" import.** The ZCode path rewrites session ids to import
  additively; a Codex rollout carries its id 229 times across 522 records
  in unknown-schema records, so rewriting was rejected as guesswork. The
  answer to "both sides advanced" is the `--replace` backup, not a fork.
- **The workspace must exist on the receiving side** at the same absolute
  path (sessions record their cwd) — `warp push` moves the project.
- **The discovery probe is version-bound.** A future codex-cli could start
  requiring the thread/app-server state. Re-run the probe after an upgrade:
  copy one rollout into a scratch `CODEX_HOME` and resolve it by id.

## Follow-ups

1. Commit and push this repo: the box's `/opt/spectre-xt-worker` only holds
   committed files, so the new scripts live there by hand-copy until then
   (and a fresh one-click install would not carry them).
2. Re-run the discovery probe whenever codex-cli is bumped on either side.
3. Two relay keys look unhealthy (Zenbook `temp-zenllm` → 401, box
   anyrouter → "high demand"); the handoff does not depend on them, but
   resuming anywhere does.
4. Decide whether a handoff should announce itself in Slack `#fleet`
   (the box already has `spectre-slack-notify`); not done here.
