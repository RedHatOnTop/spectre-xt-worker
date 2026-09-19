# 2026-09-13 — Lobby debate: Antigravity (`agy`) as a second responder in #lobby

## Why

§7.10 shipped a single responder: qoder (qodercli Efficient) replies once
in-thread to an agent post, and the chain stops by construction. The user
wants #lobby to be a real commons — opinions flowing back and forth
("자유롭게 의견이 오가야지") — with the Antigravity CLI (`agy`) as a second
voice. Two user constraints shaped the design:

- **agy hallucinates badly.** Mitigations: a persona that forces
  uncertainty marking, thread-only evidence, a `[PASS]` escape so it can
  decline to add noise, and hard caps so a bad turn cannot snowball.
- **agy is committed to a separate project.** One short run per turn,
  prompt-based read-only mode, and its quota is the user's own
  Antigravity account.
- A one-shot "critic" role was explicitly rejected: the point is
  discussion, not review.

Scope agreed: **#lobby only**. #control / #alerts / #fleet stay
qoder-only. Kill switch `SLACK_DEBATE=0` is the default; without it the
bridge behaves byte-identically to before (unit-tested).

## Turn model (v1)

Eligibility in #lobby when `SLACK_DEBATE=1`:

| author | level | result |
|---|---|---|
| allowlisted human with `qoder:` prefix | any (as today) | qoder turn, continuation=false |
| registered third-party agent (orca/healthcheck/…) | top-level only | qoder turn, continuation=false |
| responder (qoder / antigravity) post | top-level **or** thread reply | turn by the *other* responder, continuation=true |
| bridge/infra posts, unknown identities, third-party thread replies | — | unchanged ignores (fail closed) |

- Strict alternation: candidate ≠ author of the triggering event. The
  ping-pong is driven by our own posts echoing back as
  `message.channels` events (`bot_id` + customized `username`) — no
  chained enqueue, one turn per event.
- `[PASS]` is offered **only on continuation turns** (persona sentence is
  injected only then): responder replies exactly `[PASS]` → bridge posts
  nothing, audits `debate_pass`. A human question can never be answered
  with silence.
- Chunk merge: a responder-authored message within 60 s of the same
  author's previous one in the same thread → audit `debate_merged`, no
  turn (stops chunk→turn amplification).
- Caps: per-thread hourly `SLACK_THREAD_RUNS_PER_HOUR=4` (existing, now
  configurable) + non-renewing per-thread-per-day
  `SLACK_THREAD_TURNS_PER_DAY=8` + debate-day
  `SLACK_DEBATE_MAX_RUNS_PER_DAY=20`. Debate runs also count against the
  global daily 30, so #lobby cannot starve #control's budget.
- agy turns require agy bin **and** settings profile present; otherwise
  audit `debate_skip` (no error spam, no ack). qoder turns unaffected.
- A restart loses queued runs; the thread just goes quiet. Rollback =
  `SLACK_DEBATE=0` + restart.

## agy adapter

- Bin `SLACK_AGY_BIN` (default `~/.local/bin/agy`). Child env = existing
  whitelist (`HOME` carries the cached login).
- Args: `agy -p <prompt> --output-format json --print-timeout 4m`
  (240 s, before the bridge's 300 s SIGKILL). No system-prompt or
  settings flags exist, so BOX_FACTS_AGY + persona + message + thread
  context are folded into the single prompt string.
  `--dangerously-skip-permissions` stays banned.
- Result: scan reversed stdout lines for the JSON envelope;
  `status=="SUCCESS"` → `.response`; else `agy_<STATUS>: <error>`; no
  envelope → `exit_<code>_no_result`. agy spawn/failure notes are audited
  but never posted (best-effort second voice — its failure must not
  decorate a qoder thread).
- cwd: `~/.local/state/remote-agent/agy-cwd` (created at run time)
  instead of `/`.
- Read-only profile `config/agy-slack-settings.example.json`, installed
  to the fixed path `~/.gemini/antigravity-cli/settings.json` (no
  override flag exists): deny `write_file(**)`, egress commands
  (`curl|wget|nc|ncat|socat|ssh|scp|rsync|ftp|telnet`), and secret
  reads (`slack.env`, `~/.config/remote-agent/**`, `~/.ssh/**`,
  `~/.codexpro/**`). Semantics are NOT contractual → the on-box probes
  below are the deploy gate. **Re-probe after every agy upgrade.**

## Build (on fedora)

- `scripts/slack-bridge.mjs`: constants `RESPONDER_AGY="antigravity"`,
  `RESPONDERS`, `AGY_SETTINGS_PATH`, `AGY_CWD`, `DEBATE_MERGE_SEC`,
  `PASS_RE`; per-speaker `BOX_FACTS_QODER`/`BOX_FACTS_AGY` and personas
  `discussionDebateQoder`/`discussionDebateAgy`; config keys
  (`SLACK_DEBATE`, `SLACK_DEBATE_MAX_RUNS_PER_DAY`,
  `SLACK_THREAD_RUNS_PER_HOUR`, `SLACK_THREAD_TURNS_PER_DAY`,
  `SLACK_AGY_BIN`; debate requires an absolute agy bin); new pure helpers
  `otherResponder`, `isPassText`, `speakerReady`, `debateMerged`/
  `debateMergeRecord`, `buildAgyArgs`, `parseAgyResult`,
  `buildAgyPrompt`; eligibility rewrite in `classifyMessage`; state adds
  `threadDays`/`merge`/`daily.debate`; caps in budgetCheck/Record;
  speaker-aware prompts; agy branch in `runExecutor`; `[PASS]` gate and
  speaker identity in `runJob`; routing + audits in
  `handleDiscussion`.
- Tests (31 node tests total): debate-off classification equals today's;
  eligibility matrix; `[PASS]`/merge/caps; `parseAgyResult` fails closed;
  `buildAgyArgs` never contains `--dangerously-skip-permissions`;
  `buildAgyPrompt` carries no qoder/executor text; PASS sentence only on
  continuation. Python: `tests/test_agy_slack_settings.py` (profile
  shape; bootstrap never touches `~/.gemini`); notify self-test now
  expects 8 agents.
- `config/slack-agents.json`: `antigravity` (`:milky_way:`).
  `config/slack.env.example`: debate keys. `scripts/doctor.sh`: with
  `SLACK_DEBATE=1` requires agy bin + settings + registry entry, else
  bad; debate off → warn. `scripts/bootstrap.sh`: installs the example
  JSON to `/usr/local/share/remote-agent/` (never writes `~/.gemini`).
  `systemd/slack-bridge.service` header comment; RUNBOOK §7.10
  "Lobby debate" subsection.

## Review pass (fedora, before deploy)

Critical review of the build found three issues; all fixed in place:

- The new non-renewing per-thread day cap initially applied in every mode — a
  9th human `qoder:` question in one thread would have been refused where the
  old bridge answered. Now gated on debate mode in `budgetCheck` /
  `budgetRecord`; a test pins the debate-off immunity.
- `doctor.sh` matched `^SLACK_DEBATE=1` literally while the bridge parses
  `truthy()` (1/true/yes/on, last key wins, quoted values not stripped) and
  honors `SLACK_AGY_BIN`; the doctor now parses the file the way the bridge
  does (same edge cases checked) and probes the configured bin.
- RUNBOOK claimed the `antigravity` registry entry "keeps a stray agy post
  failing closed" with debate off; in fact such a post is answered once by
  qoder as an ordinary third-party identity. Text corrected.

## Deploy evidence (Spectre) — PENDING

To be filled during plan steps 0-6. Sections:

### agy install + smoke

```
# e.g. agy version, `agy --help` --print-timeout confirmation, smoke
# `agy -p 'reply ok' --output-format json | jq -e '.status=="SUCCESS"'`
```

### Probes P1-P6 (read-only profile gate)

```
# raw outputs here; any write/secret/egress leak → STOP, do not enable
```

### Equivalence check (debate still 0)

```
# audit line showing lobby_self for qoder's reply (no agy turn)
```

### E2E (SLACK_DEBATE=1)

```
# thread transcript sketch + audit lines with "speaker" fields
```

### doctor

```
# sudo spectre-doctor → 0 FAIL
```
