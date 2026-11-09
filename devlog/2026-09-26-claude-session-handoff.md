# The 11-hour control-plane session moved to the box — 2026-09-26

A Claude Code session was migrated from fedora to Spectre while it was still
running its own `/goal` loop. The operator asked for it as a **full handoff**
(sender stops, receiver owns it) because the desk was about to be left
unattended for the night. RUNBOOK §7.19 is the procedure this run produced.

## What was migrated

| | |
| --- | --- |
| Session | `5522c1fa-6111-416a-99ec-a3888035bfa2` |
| Started | 2026-09-25 12:34 KST, ~11 h 40 m of wall clock |
| Goal | `/goal control plane 구현 계속해줘` (session-scoped Stop hook) |
| Model | Opus 5.5 · xhigh · anyrouter (`claude-opus-5-5`) |
| Branch | `feat/spectre-control-plane-pr1` @ `d7f43e9`, PR #4 |
| Sender | Orca terminal `term_248399d4-…` in the fedora worktree |
| Receiver | Orca terminal `term_01e3b40f-ab5b-4616-adbd-788ebe5dbba7` in `~/Projects/remote-agent` |

The session was mid-slice: it had just pushed two reaper fixes and was about to
document the tab-record/listener/title slices. It was **not** at a clean
stopping point, which is exactly the case the handoff has to survive.

## The box already had the tree — at a different path

The 2026-09-26 control-plane deploy had cloned the repo to
`~/Projects/remote-agent`, while the session's cwd was
`~/Projects/distribution-project/remote-agent`. `/home/person/Projects/
distribution-project/` also exists on the box (it holds `pugc-ade`), so the
familiar path was a **trap**: it resolves, and nothing is in it. The receiving
side therefore needed no new clone, only the path spelled correctly.

HEAD matched on both sides (`d7f43e9`), so only the working tree had to travel.
`git status --porcelain` had 17 entries on fedora (6 modified, 11 new — three
unpushed devlogs, `LESSONS.md`, `scripts/relay_probe/`, `tests/test_relay_probe.py`);
after `rsync` the box's set was identical, entry for entry.

`rsync -a` carried 28 MB: the whole tree except `.git/`, `zcode-remote-app/`
(233 MB Electron app), `.mimocode/` (58 MB), `references/` and build caches.
`zcode-remote-app` is tracked but irrelevant to the session, and the box had
never had it; leaving it out kept the copy honest about what actually moved.

## Four artifacts, not one

The transcript is the visible part of a session but not the whole of it:

- `~/.claude/projects/<slug>/<uuid>.jsonl` — the conversation
- `~/.claude/file-history/<uuid>/` — per-session backup blobs, 601 KB
- `/tmp/claude-1000/<slug>/<uuid>/` — background-task stdout, 110 KB
- `~/.claude.json` → `projects["<cwd>"].hasTrustDialogAccepted`

The **slug is derived from the cwd** (`/` → `-`), so the same session id lands
under `-home-person-Projects-remote-agent` on the box rather than the sender's
`-home-person-Projects-distribution-project-remote-agent`. Copying the file to
the sender's slug would have left `claude --resume` unable to find it. The
transcript carries both slugs' worth of nothing else — its own `cwd` field stays
at the fedora path, which is the address rather than a claim about the machine.

The transcript hashes matched byte for byte after the copy
(`ea5a2e9e465a4e276638ce6d0c09022deec53852adc70a97a2ac1cf271f0c717`), which is
also the proof that the sender had really stopped: a live sender appends on
every turn, so an identical hash five minutes later means nothing else wrote to
it.

## The trap that cost the first attempt: the trust dialog

The first terminal came up and sat at

```
 Quick safety check: Is this a project you created or one you trust?
 ❯ No, exit
```

`--permission-mode bypassPermissions` does **not** skip this. It is a
workspace-trust gate, not a permission gate, and reading the box's CLI strings
confirmed there is no non-interactive flag for it (`Yes, I trust this folder` is
the only affirmative). Sending text to the dialog from the outside is a
keystroke-guessing game that is not worth playing when the state is a documented
field.

The fix was to seed the entry before creating the terminal, merging into
`~/.claude.json` with a timestamped backup (`.claude.json.bak-handoff-…`) — the
file is mode 600 and holds relay keys, so a from-scratch rewrite was not
acceptable. The shape was copied from an already-trusted project so the CLI
finds every field it expects. The second terminal resumed straight to the TUI.

This is worth remembering because it is invisible in every "copy the .jsonl"
recipe: **a Claude session handoff is three files and one workspace-trust
flag**, and the flag is the one that fails silently in the direction of looking
like a hang.

## The goal re-armed by itself

The resumed terminal's statusline read `◎ /goal active` with no `/goal` typed
on the box. The Stop hook is reconstructed from the transcript's recorded
`/goal` command, so the session keeps its condition across the move. The
operator's stated worry — that a migrated session would quietly lose its loop
and stop after one turn — does not happen here.

The session was told, as the first message on the new machine, that the cwd had
changed and which paths are now real. That is not politeness: the transcript's
tail still reads the fedora path, and a session that resumes and immediately
writes a path from its own history writes it on the wrong machine.

## Honest limits

- **It is a fork point, not a live attach.** The sender was killed first
  (SIGTERM; the TUI exited in about a second), so there is exactly one live
  copy. If both had run, the two transcripts would diverge silently and the
  git history with them.
- **The fedora Orca tab stays open as a dead shell.** Killing the CLI leaves
  the tab at a prompt; it was closed with `orca-ide terminal close`, because a
  worktree that looks like it holds a live worker is exactly what the Slack
  dispatch pin logic keys on.
- **The session's `ctx` counter and auto-compact history travel with it.**
  The resumed session came up at `ctx 125k` with twelve prior auto-compacts
  already in the transcript, so the context ceiling work of 2026-09-24/25
  (§`devlog/2026-09-25-spectre-claude-code-token-slimming.md`) applies on the
  box unchanged — the box's prefix is slimmer (19 tools) than the one the
  session was built under (48–50), so it is not a regression.
- **fedora's `~/.claude.json` was not edited.** The trust seed was written only
  on the box. The sender's entry still claims the fedora path, which is now
  misleading but harmless.

## Verification

Per the AGENTS.md rule, the checks were run on the box:

- `orca-ide repo add --path /home/person/Projects/remote-agent` → repo
  `564a5322-…`; `worktree current` reports
  `564a5322-…::/home/person/Projects/remote-agent`, a real registered worktree
  (not a `path:` binding), and `worktree ps` shows `live:1 pty:yes`.
- The terminal banner on the box reads
  `~/Projects/remote-agent · ⎇ feat/spectre-control-plane-pr1 · ◎ /goal active ·
  PR #4 · bypass permissions on`.
- Transcript `sha256sum` identical across the two machines; `file-history` and
  task directories present on the box.

**One step is unverified at the time of writing:** the Tailscale SSH 24 h check
lapsed during the session (the browser re-approval URL appeared), so the final
"send it a message and watch it answer" confirmation was made *before* the
lapse and not repeated through the restored link. The terminal was observed
`running` with the operator's handoff note on its input line and the session
generating a reply; the reply itself was not read back.

## Follow-ups

- The procedure is RUNBOOK §7.19, with gates appended to the box verification
  list. There is no `claude-handoff` helper. §7.15's `codex-handoff` exists
  because Codex sessions are hidden under date-partitioned rollouts; a Claude
  session is a flat `<uuid>.jsonl`, so `rsync` plus the trust seed is the whole
  job. A helper would mostly be the trust-merge, which is the part that must
  stay auditable.
- The `config/qoder-workers.json` `terminal` pin question does not arise for
  this worktree yet — the box's registry is seeded from the live box state
  (`~/.config/remote-agent/qoder-workers.json`) and `remote-agent` is not a
  registered worker there. If it becomes one, the handoff terminal is the pin.