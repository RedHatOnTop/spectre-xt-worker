# remote-agent

24/7 agent worker box for the idle HP Spectre XT TouchSmart (13-2000,
2012, i7-3517U, 12 GB, 256 GB mSATA + 128 GB SATA). This is not an HP ZBook
and not the 15-inch ENVY Spectre XT. The daily driver is the ASUS
Zenbook Duo (`fedora`).
This directory is its own git repository. Do not mix commits with sibling
projects under `distribution-project/`.

## Role

The Spectre is an **agent runtime**, not a compile farm. The worker stack is
Orca ADE serve (`:6768`) + qodercli; ZCode and the round-robin
`hardened-zai-proxy` were retired from the box on 2026-09-15 (key pool dead;
`/opt/ZCode`, `~/.zcode`, `/work/hardened-zai-proxy` removed — fedora keeps
its copy). Rust debug builds, Docker Desktop, GNOME, and a second Electron
app do not belong here.

`devcodex` on the box is **ChatGPT-only** (2026-09-17): ChatGPT reaches it
through the DevSpace connector (Funnel → `:7676`) and runs the CLI with its
`bash` tool; the terminal agents keep their own tooling and never call
devcodex. After a devcodex change: install the tree, refresh
`~/.agents/skills/devcodex/SKILL.md` and `~/.codex/AGENTS.md`, then
`systemctl --user restart devspace` — the codex terminals are not part of
that reload (RUNBOOK §7.13).

## Rules

- Documentation is English. Replies to the user may be Korean.
- Never commit `env.json`, `.env`, API keys, `credentials.json`, or Tailscale
  auth keys. Reference secrets by name only.
- Prefer systemd user units over shell loops and `nohup`.
- Agents announce lifecycle events in Slack `#fleet` and open issue
  threads in `#lobby` via `spectre-slack-notify` under their own identity
  (registry: `config/slack-agents.json`; RUNBOOK §7.10). Never post
  secrets or env values to Slack.
- `ssh spectre` uses Tailscale SSH in check mode: every 24 h a browser
  re-approval is needed. When it lapses, ssh hangs after printing a
  `login.tailscale.com/a/<token>` URL — surface that URL to the user
  instead of treating it as an outage. Details in `RUNBOOK.md` §7.
- A stopped `/goal` unit is reviewed automatically by the goal supervisor
  (`spectre-goal-supervisor`, RUNBOOK §7.16) and the next goal is dispatched
  through the bridge's guards. It is installed off: `SPECTRE_GOAL_SUPERVISOR`
  must be truthy in the unit, and a reviewer decision dispatches only with
  evidence. Never let a reviewer run with write access.
- Sessions the user asks to migrate or create must be **visible and
  controllable in Orca**: bring them up as an Orca-managed terminal in the
  project's worktree (`orca-ide terminal create --worktree path:<dir>
  --title <t> --command <cmd>`, RUNBOOK §7.8) — never a bare tmux session
  or an invisible background process. The operator checks and controls work
  remotely through the Orca ADE client. A worktree may end up with several
  live terminals (a handoff session beside the qoder worker); the Slack
  bridge then dispatches only through the worker's `terminal` pin in
  `config/qoder-workers.json` (§7.10) — set the pin whenever a second live
  terminal appears in a worker's worktree.
- A change is done only when the verification command in `RUNBOOK.md` was
  actually run on the Spectre (or explicitly marked unverified).
