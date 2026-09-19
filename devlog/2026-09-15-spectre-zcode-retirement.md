# 2026-09-15 — The Spectre drops ZCode; worker dispatch gets a terminal pin

## Report

The hardened-zai-proxy's key pool is dead, and with it the reason ZCode and
the round-robin proxy existed on the box at all. User: "더이상 스펙터에 ZCode와
round robin 프록시가 남아있을 이유 자체가 없음 — 키 풀이 죽었으니 말일세",
scoped to the box: "스펙터에서만 날리고 이 머신에선 남겨둬" (the Zenbook keeps
its copy). The worker stack moved to Orca ADE serve + qodercli on
2026-09-08 (§7.8); this removes the leftovers.

## Off the box (fedora untouched)

- `zcode` 3.8.1-5310 purged; `/opt/ZCode` (568M) and the
  `/usr/bin/zcode` → `/etc/alternatives` chain gone;
- `~/.zcode` (355M of app state) and `/work/hardened-zai-proxy` (19M);
- user units `glm-proxy.service` (the proxy on :18088) and
  `darwin-autonomous.service`, both inactive/disabled before removal;
- `~/.local/bin/zcode-worker`, its autostart `.desktop` (the
  software-rendering chain), and `/usr/local/bin/spectre-pin-zcode`.

Verified in place: `sudo spectre-doctor` passes with both retirement lines
("hardened-zai-proxy retired on the box …", "ZCode retired on the box …")
and no FAILs; `spectre-status` prints `proxy retired keys=0` /
`zcode retired`; no process on :18088. `status.sh` / `doctor.sh` report
"retired" when the paths are absent, and still run the old checks when they
are present — fedora keeps its copies, so the branch is conditional, not
deleted.

## warp moves files only to a ZCode-less peer

`warp` used to push ZCode session bundles + artifacts and schedule an
import on the peer. `peer_has_zcode()` (checks
`~/.zcode/cli/db/db.sqlite` on the peer over ssh) now gates all of it:
push ends "pushed (files only). spectre has no ZCode — sessions skipped.",
pull ends "files only — the peer has no ZCode". Verified end to end from
`/tmp/warp-guard-test`: exit 0, box `~/.cache/spectre-warp` empty, no
`~/.zcode` recreated. The artifacts an earlier warp had already delivered
were discarded on the box (31 session dirs + 6 agent dirs, all ctime-today;
a 121MB sessions.db bundle; the pending import loop killed) — none have
reappeared.

## The bridge, same day (RUNBOOK §7.10)

Two reported bridge bugs were confirmed on the deployed binary and fixed:
Slack's `*Sent using ChatGPT*` trailer broke single-word command parsing
(`classifyCommand` strips it), and `handleControl` passed `decision.worker`
— a field the classifier never sets — to the dispatcher
(`controlDispatchArgs` re-derives `{builtin, worker, prompt}` from the raw
command). The dispatch path also gained the orca-native injection the
report asked for: a worker with `tmux: null` resolves through
`orca-ide terminal list --json` and is typed into via `orca-ide terminal
send`.

## The `terminal` pin (why it exists)

Bringing the warped Codex session up for remote control (AGENTS rule:
migrated or created sessions are Orca terminals, §7.15) put a **second**
live terminal in the minecraft worktree — and the "exactly one live
terminal" rule then refused every `resume minecraft` ("2 live orca
terminals … refusing to guess", reproduced live in a dry-run).
`pickNativeTerminal` now takes an optional `terminal` handle from the
worker registry. The pin only narrows: it is honored while that exact
terminal is live, a stale pin refuses rather than falling back to a sibling
(a fallback could type `/goal resume` into the Codex session), and
unpinned workers keep the old unique-match rule. Deployed 23:21 KST with
`minecraft` pinned to its qoder terminal; dry-runs: minecraft → pinned
handle (parked/goal_budget), korea-metro-twin → unique match, zzbrush →
refused (no live terminal).

## Open

- The installer scripts (`bootstrap.sh`, `install-zcode.sh`,
  `setup-wizard.sh`) still install ZCode + the proxy on a fresh machine.
  Kept as-is because fedora keeps its stack — but a from-scratch **box**
  install would reintroduce what this entry removed.
- `codex-handoff` still prints the tmux resume line; the Orca-terminal form
  is documented (§7.15) but not wired into the tool.
- `zzbrush`'s Orca terminal is absent from `terminal list` even though its
  qodercli process is alive (3+ days) — a pre-existing box condition, so
  `resume zzbrush` refuses until a terminal exists in that worktree.

Files: `scripts/{warp.sh,status.sh,doctor.sh,slack-bridge.mjs,install-codex.sh}`,
`config/qoder-workers.json`, `tests/slack_bridge.test.mjs`, `AGENTS.md`,
`RUNBOOK.md` §5/§7.10/§7.15, `config/codex-global-AGENTS.md`.
