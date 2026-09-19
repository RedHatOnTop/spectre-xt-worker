# 2026-09-16 — Agent guards relaxed on the Spectre: work surface full, destructive floor kept

## Requirement

User: "스펙터에서 devcodex의 안전장치를 대폭 해제해줘 - 파괴적 명령어를 제외한 모든
작업이 가능하도록" — the guards on the worker box were too tight for an agent to
work, and the model in question (GPT-5.6 Sol / gpt-6-astra on the box's Codex CLI)
is not the risk the guards were sized for. The one thing that must survive is the
block on actions whose damage cannot be undone.

Scope was confirmed against a four-item inventory rather than guessed; the user
selected items 1, 3, 4 and 5 and left the Claude-side deny list (item 2) alone:

1. Codex CLI guard hooks — drop read-guard, install the Codex-shaped
   irreversible-guard.
2. Claude Code deny list — **not in scope** (left as-is).
3. DevSpace connector `allowedRoots` — widen to the whole home.
4. devcodex permission profile — `shell: ask` → `allow`.
5. Slack/qoder executor allowlist — widen from read-only to a full working
   surface behind a destructive floor.

Deploy shape: repo is the source of truth (`distribution-project/remote-agent`)
and the box is re-applied from it, so a re-run of the installers does not revert
the change.

## What was done (repo — verified with `verify.sh`, all gates green)

- **Codex (item 1).** `scripts/install-codex.sh` now installs
  `config/codex-irreversible-guard.mjs` → `~/.codex/hooks/irreversible-guard.mjs`
  and removes a stale `read-guard.mjs`. `config/spectre-codex-config.toml` points
  its `[[hooks.PreToolUse]]` at the new file. `scripts/doctor.sh` checks the guard
  file plus both probe directions (an irreversible command exits 2, ordinary work
  exits 0) and asserts read-guard is gone.
- **devcodex (item 4).** `devcodex/src/permission.js` allows every named action;
  `devcodex/test/permission.test.js` and `devcodex/README.md` moved with it. The
  vendored tree hash pin in `scripts/install-devcodex.sh` moved from
  `59c2fac9…` to `b9f50632…`. Upstream (`~/Projects/devspace-demo/devcodex`) was
  deliberately not edited, so the two trees now differ on purpose.
- **Executor (item 5).** `config/slack-executor-settings.example.json`: allow is
  now the practical tool set with a bare `Bash`; deny keeps the secret path rules
  and gains the destructive first-word/subcommand floor. `tests/test_slack_executor_settings.py`
  is rewritten as the regression guard for the new boundary (capability set
  exact, secret + destructive denies required). `scripts/slack-bridge.mjs` lost
  its read-only prose (help text, box facts, control/triage personas, progress
  message, executor comment).
- **DevSpace (item 3).** RUNBOOK 7.14 config block and prose (no installer by
  design — the owner token is a live credential).
- **Docs.** RUNBOOK 7.10 (posture change, superseded banners on the 2026-09-12
  read-only probe record, union-cannot-widen note), 7.11 (Codex guard bullets and
  verify block), 7.13 (devcodex posture), 7.14, and the preflight checklist line.

## Evidence

Repo, on the daily driver: `verify.sh` green — shell syntax, `py_compile`,
slack bridge tests, vendored devcodex suite, 181 unit tests.

On the Spectre (all observed 2026-09-16, not inferred):

- `sudo spectre-doctor`: **57 passed, 1 failed** — every Codex check green
  (`irreversible-guard hook installed`, `guard blocks an irreversible command`,
  `guard allows ordinary work`, `read-guard removed`), devcodex green. The one
  failure is unrelated drift: `devspace /mcp` answered `000` because the check
  ran while the connector was still booting after its restart; it answers `401`
  now.
- Codex guard probes: `git merge main` → exit 2 with the refusal message;
  `ls -la` → exit 0.
- `devcodex permission shell --json` → `allow`, `read` → `allow`,
  `fileDelete` → `deny`.
- `devspace`: restarted clean, logs `allowed roots: /home/person`,
  `curl /mcp` → `401`.
- Vendored devcodex suite on the box: **70/70**.
- Executor probes (RUNBOOK 7.10, results in that section): native `Write` →
  DONE under `acceptEdits` (DENIED under `default`); never-allowlisted bash
  `touch` → DONE; plain `rm -rf` → DENIED, target untouched; compound
  `rm -rf … && touch …` → DENIED, target untouched; tool set back to 29 names.

## Two things the box taught us

- **The headless write gate is mode-driven, not allow-list-driven.** Adding
  `Write`/`Edit` to the allow list did nothing while the bridge passed
  `--permission-mode default`; the same probe wrote as soon as the mode was
  `acceptEdits`. The bridge, its test and RUNBOOK 7.10 all moved together.
- **The repo was stale against its own box.** The live bridge carried an
  attribution-trailer fix (three `Sent using` forms) from 2026-09-15 that the
  repo never got. It is folded back in here, with the test it never had.

## Honest limits

- **Indirection is the gap, not compounds.** Plain and `&&`-compound `rm` are
  both refused (the engine checks each segment), and substitution-bearing
  commands were refused in the 2026-09-12 probes. What the deny list cannot see
  is a script written and then run — the same honest limit the guard hooks
  document. qodercli 1.1.47 runs no PreToolUse hooks from `--settings`, so
  there is no second layer on that path.
- **Bash-side secret reads are open.** The secret rules are `Read(...)` tool
  rules; with the reader-command denies gone, `cat` runs and `cat
  /etc/hostname` proves it. The secret probe produced no token only because the
  model declined — not an engine gate. Restoring the reader denies costs
  ordinary work; the alternatives are a wrapper or accepting it.
- **Slack `#control` is now execution access.** An allowlisted member's mention,
  or anything that prompt-injects the models the executor reads, reaches a full
  shell (plus native write tools) as the box user.
- **Cron/worktree mechanics stay denied** on purpose. They are not "work" —
  they let a run outlive itself or leave its sandbox — so the allow list leaves
  them out even though the tool set contains them.
- DevSpace file tools can now reach box credentials. The marginal risk is small
  because the `bash` tool beside them was already unabridged, but the file-tool
  bound is gone as a *stated* barrier.
- **Operational slip, recorded:** verifying the bridge binary by running
  `spectre-slack-bridge --help` started a second, manual bridge instance that
  held the Slack socket for ~3m46s until it was killed. The audit log shows no
  dispatch or executor run in that window (only socket heartbeats and the
  `stop SIGTERM`), and the systemd instance reconnected. Not a no-op on the
  box's Slack session, but no work was done under the new posture.

## Follow-ups

1. Decide the bash-side secret question above (reader denies vs wrapper vs
   accept).
2. The box's `/usr/local/share/remote-agent/slack-agents.json` is stale against
   `config/slack-agents.json` (7 agents vs 9), which fails
   `test_self_test_ok_on_full_config` when the suite runs on the box. Pre-existing
   and untouched here — refreshing the registry is a Slack-facing change and
   needs a call.
3. Re-vendoring devcodex must re-apply the box permission posture and re-pin,
   or the `install-devcodex.sh` hash check will fail loudly (by design).