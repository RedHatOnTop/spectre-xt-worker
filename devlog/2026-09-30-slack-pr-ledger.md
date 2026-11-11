# 2026-09-30 — Slack as the operator's inbox, not an agent community

## Why

The Slack workspace (RUNBOOK §7.10) was built for a fleet of cheap agents talking to each
other. The work is now done by Claude Code sessions, and the audit log
(`/work/logs/slack-bridge.log`, 2026-09-12 → 09-30) shows what that left behind:

- 198 executor jobs: 171 alert triage in `#alerts`, 20 `#control`, 6 `#lobby` discussions.
- 09-28 and 09-29: 84 alert posts, all one failure — `/` above 90 %. The failure key held
  the percentage (`disk:/=96%` → `97%` → …), so every point was a fresh streak, and the
  flat 30 min renotify added 26 + 42 more. Each drew a qoder triage until the 30/day cap,
  twice (`daily_cap` blocked 270 triage jobs in total). `/` reached 100 % at 19:15Z on 09-29.
- Claude Code sessions post nothing to Slack; the bridge's `goal`/`resume` dispatch targets
  Efficient/flash/mimo/astra and was quiet after 09-26.

## What changed

- `healthcheck.py`: `failure_key()` drops measured numbers, so drift inside one failure is
  not a new streak; renotify starts at `RENOTIFY_MIN` and doubles per push, capped at 4 h.
- `slack-bridge.mjs`: `SLACK_LOBBY_DISCUSSION` (default 1). `0` ignores every `#lobby`
  message (`lobby_discussion_disabled`), debate mode included.
- `slack-notify.py`: `--update-ts` edits an earlier post (`chat.update`).
- `slack-receipt.py` (new, `spectre-slack-receipt`) + `slack-receipt-sync.timer`: one `#lobby`
  message per pull request, edited as it moves; `gh` settles merged/closed PRs every 15 min.
- `slack-brief.py`: opens with `*awaiting merge*`; the `qoder:` reply hint is gone.

## Switched off, not removed

qoder triage (`SLACK_TRIAGE=0`) and `#lobby` discussion (`SLACK_LOBBY_DISCUSSION=0`) in the
box's `slack.env`; debate was already `0`. Turn either back on by flipping the key and
`systemctl --user restart slack-bridge`. The nine identities in `slack-agents.json` stay.

## Not done

- Disk detail in the alert. `du -x -d1 -m ~` took **61 s (25 s sys)** on this box — too heavy
  for a 1-minute probe; it needs a cached, off-peak scan. That scan showed `~/Projects`
  90 GiB, `~/oracle-backup-2nd` 39 GiB, `~/orca` 14.5 GiB of the 173 GiB in `$HOME`.
- Decision inbox. `fullmoon-agent-control/slack_approval.py` already does request → thread
  reply → text typed into an Orca terminal, but its daemon runs nowhere, it is not under
  version control, and `fleet_decision` lets a grokbot verdict approve. Whether that is the
  right approver for Claude sessions is the operator's call.
- The baseline had two unrelated failures, left as found: `tests.test_slack_notify` self-test
  (the installed registry has 9 agents, the repo 10) and the node `flash dry-run` test
  (`dispatch_flash_busy` — a live Flash terminal).
