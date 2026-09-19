# Spectre agent automation pipeline — live inventory

Target `ssh spectre` (Tailscale SSH, user `person`, Debian 13, kernel 6.12.101+deb13-amd64).
Probe window 2026-09-18 ~23:26–23:29 KST. Raw evidence per claim; no secret values printed.

## 0. Connectivity
```
$ timeout 30 ssh -o BatchMode=yes spectre 'echo CONNECTED; hostname; uname -a'
CONNECTED / spectre / Linux spectre 6.12.101+deb13-amd64 #1 SMP PREEMPT_DYNAMIC Debian 6.12.101-1 (2026-08-05) x86_64
```
No `login.tailscale.com/a/<token>` URL. Tailscale check-mode re-approval **not** required.

## 1. Requested units (`systemctl --user show -p ActiveState -p SubState -p UnitFileState`)
| Unit | Active | Sub | FileState | Evidence |
|---|---|---|---|---|
| orca-serve | active | running | enabled | start Sat 2026-09-12 17:49:16 KST |
| slack-bridge | active | running | enabled | start Wed 2026-09-16 18:26:59 KST |
| goal-supervisor | inactive | dead | static | unit exists but is a RETIRED stub (§10) |
| qoder-goal-watch | inactive | dead | static | oneshot, last Fri 23:25:53; timer-enabled |
| devspace | active | running | enabled | start Fri 2026-09-18 21:54:35 KST |
| codexpro-handoff | — | — | **ABSENT** | no unit file, no binary anywhere |
| obscura-cdp | active | running | enabled | start Mon 2026-09-14 00:27:23 KST |
| worker-health | inactive | dead | static | oneshot, last Fri 23:26:53; timer-enabled |
| slack-brief | inactive | dead | static | oneshot, last Fri 09:00:03; timer-enabled |
| status-http | active | running | enabled | start Sat 2026-09-05 13:32:41 KST |
| glm-proxy | — | — | **ABSENT** | no unit file; text match only in `spectre-doctor`/`spectre-status` |

`list-units --type=service --all` = **35 loaded**. Verbatim agent rows:
Row shape from the command: `<unit> loaded <active> <sub> <description>`. Agent rows verbatim:
`devspace loaded active running`; `obscura-cdp loaded active running`; `orca-serve loaded active running`;
`qoder-efficient-guard loaded inactive dead`; `qoder-goal-watch loaded inactive dead`;
`qoder-nudge loaded inactive dead`; `slack-bridge loaded active running`;
`slack-brief loaded inactive dead`; `status-http loaded active running`; `tmux-work loaded active running`;
`worker-health loaded inactive dead`.
Extra agent units in `list-unit-files`: `qoder-efficient-guard.timer`, `qoder-nudge.timer`,
`qoder-goal-watch.timer`, `worker-health.timer`, `slack-brief.timer` all **enabled**;
`codex-goal-healer.timer` **disabled** (service static, inactive dead); `grokbot-goal-event.path` **disabled**;
`goal-supervisor.path` static/RETIRED. No `slack-bridge.timer` (long-running daemon).

Ports (`ss -ltnp`) corroborate the daemons: `0.0.0.0:6768 orca-ide pid 1423323`;
`127.0.0.1:7676 node pid 2677631` (devspace); `127.0.0.1:9091` + `100.119.252.88:9091 python3 pid 453086`
(status-http, Tailscale IP is 100.119.252.88); `127.0.0.1:9222 obscura pid 1635600`.

## 2. `systemctl --user list-timers --all --no-pager`
```
NEXT                            LEFT   LAST                        UNIT                        ACTIVATES
Fri 2026-09-18 23:26:48 KST      11s   Fri 23:23:48 (2min ago)     qoder-nudge.timer           qoder-nudge.service
Fri 2026-09-18 23:26:53 KST      16s   Fri 23:25:53 (43s ago)      qoder-efficient-guard.timer qoder-efficient-guard.service
Fri 2026-09-18 23:26:53 KST      16s   Fri 23:25:53 (43s ago)      worker-health.timer         worker-health.service
Fri 2026-09-18 23:27:53 KST 1min 16s   Fri 23:25:53 (43s ago)      qoder-goal-watch.timer      qoder-goal-watch.service
Sat 2026-09-19 06:39:12 KST       7h   Fri 06:37:57 (16h ago)      fullmoon-backup-pull.timer  fullmoon-backup-pull.service
Sat 2026-09-19 09:00:00 KST       9h   Fri 09:00:03 (14h ago)      slack-brief.timer           slack-brief.service
6 timers listed.
```
No `goal-supervisor.timer` exists (it is a `.path` unit).

## 3. `tmux ls` + pane detail
```
pugc: 1 windows (created Sat Sep 12 00:40:18 2026)
qoder: 1 windows (created Tue Sep  8 22:56:40 2026) (attached)
work: 1 windows (created Wed Aug 26 16:29:21 2026)

pugc:0.0  cmd=qodercli pid=1078754 path=/home/person/Projects/distribution-project/pugc-ade
qoder:0.0 cmd=qodercli pid=574580  path=/home/person/Projects/orca-rust
work:0.0  cmd=bash      pid=1649     path=/home/person
```
All three live; `work` is the phone-SSH shell owned by `tmux-work.service`.

## 4. Worker / Slack configs
`~/.config/qoder-workers.json` **ABSENT**; `~/.config/slack-agents.json` **ABSENT**.
Live copies are root-owned in `/usr/local/share/remote-agent/` (as `slack-bridge.service` documents).

`/usr/local/share/remote-agent/qoder-workers.json` (verbatim; no secrets present):
```json
{"workers":{
 "qoder":{"cwd":"/home/person/Projects/orca-rust","tmux":"qoder"},
 "pugc":{"cwd":"/home/person/Projects/distribution-project/pugc-ade","tmux":"pugc"},
 "zzbrush":{"cwd":"/home/person/Projects/zzbrush","tmux":null},
 "minecraft":{"cwd":"/home/person/Projects/minecraft-server-project","tmux":null,
              "terminal":"term_8e21fb5c-7632-4601-a60f-07d804858886"},
 "korea-metro-twin":{"cwd":"/work/korea-metro-twin","tmux":null}}}
```
`/usr/local/share/remote-agent/slack-agents.json` (verbatim — identities/emoji only, no tokens):
`bridge :link:`, `healthcheck :stethoscope:`, `orca :satellite:`, `zcode :zap:`, `claude :robot_face:`,
`qoder :brain:`, `spectre :desktop_computer:` (each entry also carries `"username"` = its key).
`zcode` is still registered though ZCode was retired. Other names there: `qoder-goal-clause.md`,
`slack-executor-settings.json`, `slack-executor-settings.json.bak-20260916`,
`slack-claude-settings.json.retired`, `slack-guard.mjs.retired`.

## 5. `~/.config/remote-agent/` (filenames only — values NOT read)
```
-rw------- person person 364  health.env
-rw------- person person 370  slack.env
```
Exactly two files, both mode 0600. No values printed.

## 6. Agent CLIs
`ls -la ~/.local/bin/` (all entries): `claude -> /usr/local/bin/claude`, `codex-mode` (60 KB),
`qodercli -> /home/person/.qoder/bin/qodercli/qodercli-1.1.55`, `qoder-efficient`, `qoder-efficient-guard`,
`qoder-nudge`, `qoder-sota-watchdog`, `qoder-sota-watchdog-guard`, `qoder-account2`, `orca`,
`orca-ide -> /opt/Orca/resources/bin/orca-ide`, `playwright`, `playwright-mcp`, `fullmoon-backup-pull.sh`,
`f2py`, `nbt`, `numpy-config`, `yt-dlp`.

`command -v` under the probe shell (remote PATH `/usr/local/bin:/usr/bin:/bin:/usr/local/games:/usr/games` —
`~/.local/bin` is **not** on it):
```
qodercli -> NOT_FOUND   (symlink exists in ~/.local/bin; PATH artifact)
qoder-efficient -> NOT_FOUND   (same PATH artifact)
codex -> /usr/bin/codex | claude -> /usr/local/bin/claude | obscura -> /usr/local/bin/obscura
devcodex -> /usr/local/bin/devcodex | orca -> /usr/bin/orca
orca-ide -> /usr/bin/orca-ide  (differs from ~/.local/bin/orca-ide -> /opt/Orca/...)
agy -> NOT_FOUND | grok -> NOT_FOUND
```
Versions: `qodercli 1.1.55`, `codex-cli 0.154.0`, `claude 2.1.236`, `obscura 0.2.2`, `orca-ide 1.4.198`;
`devcodex` rejects `--version` (UNVERIFIED version).

## 7. Project directories
```
ls /home/person/Projects/ : DarwinInspection adverse devspace-demo distribution-project mc-atelier
                            minecraft-server-project orca-rust qoder-devcodex-harness zzbrush
ls /work/                 : korea-metro-twin logs lost+found npm-cache oracle-backup person
```
`/work/logs`: `charge-limit.log`, `darwin-autonomous.log` (39.8 MB), `goal-supervisor.log` (Sep 16 18:21),
`health.log`, `orca-bootstrap.log`, `pugc-verify-2026-09-12.log`, `qoder-efficient-guard.log` (Sep 18 23:27),
`qoder-goal-watch.log` (Sep 17 09:01), `qoder-nudge.log` (Sep 18 23:27), `slack-bridge.log` (Sep 18 23:22),
`stealth.log`. Note `pugc-ade` is under `Projects/distribution-project/`, not `Projects/` top level.

## 8. Orca state — minecraft-server-project IS an Orca worktree
`orca-ide terminal list --json` → `ok: true`, **19 terminals**, all `connected: true`, `orphaned: false`.
Compact projection (title | worktreePath | agentIdentity):
```
◇ Gemini CLI | /home/person/Projects/minecraft-server-project | gemini
(person@spectre shell, untitled) | /home/person/Projects/minecraft-server-project | None
◇ Gemini CLI | /work/korea-metro-twin | gemini
◇ Gemini CLI | /home/person/Projects/orca-rust | gemini   (+ one untitled, same path)
◇ Gemini CLI | /home/person/Projects/zzbrush | gemini
◇ Gemini CLI | /home/person/Projects/adverse | gemini    (x4)
person@spectre: ~/wt/release-readiness-spectre | None     (x6)
표시 정책 업데이트 적용 | release-readiness-spec... | codex
DeepSeek Flash LIVE - active tutorial exit | release-readiness-spec... | None
```
minecraft worktree detail: handle `term_8e21fb5c-7632-4601-a60f-07d804858886`, worktreeId
`bbc15fac-9ef1-426c-a4ba-81a9e6346afd::/home/person/Projects/minecraft-server-project`,
`branch refs/heads/main`, `writable: true`, preview:
```
YOLO Shift+Tab to Auto Mode | goal on 0/9999 · 1 AGENTS.md file · 17 skills
*   Type your message or @path/to/file
```
**That handle is exactly the `terminal` pin recorded for worker `minecraft` in `qoder-workers.json` (§4)** —
the pin is present and correct even though a second live terminal sits in the same worktree.
`ls ~/.orca/` → only `agent-hooks/`; no other state files.

## 9. `/work/logs/slack-bridge.log` — last 40 lines
Exists, 199 KB, mtime 2026-09-18 23:22 KST. Content is **only** Socket Mode heartbeat pairs, ~9 min apart,
`connection_time: 600`; nothing secret appeared, so nothing was redacted:
```json
{"at":"2026-09-18T14:22:23.392Z","evt":"socket_open"}
{"at":"2026-09-18T14:22:23.402Z","evt":"hello","connection_time":600}
```
Identical pairs at 11:31, 11:40, 11:49, 11:58, 12:07, 12:16, 12:25, 12:34, 12:43, 12:52, 13:01, 13:10,
13:19, 13:28, 13:37, 13:46, 13:55, 14:04, 14:13, 14:22 UTC. No message/dispatch events in the tail.
`~/.local/state/remote-agent/slack-bridge-state.json` (373 B, Sep 18 08:30) observed but not read.

## 10. Unit definitions (redaction applied; no secret values were present)
`systemctl --user cat slack-bridge.service`:
```ini
[Unit]
Description=Slack agent-community bridge (Socket Mode, outbound only)
Documentation=file:///opt/spectre-xt-worker/RUNBOOK.md
# Prereqs (RUNBOOK 7.10): ~/.config/remote-agent/slack.env (0600, parsed by the bridge itself);
# /usr/local/share/remote-agent/{slack-agents.json, slack-executor-settings.json};
# executor: ~/.local/bin/qoder-efficient (qodercli Efficient; bridge default).
# No EnvironmentFile: slack.env must never enter the process environment.
StartLimitIntervalSec=300
StartLimitBurst=5
[Service]
Type=exec
ExecStart=/usr/local/bin/spectre-slack-bridge
Environment=SPECTRE_WORKER_PROFILE=box
Environment=PATH=/usr/local/bin:/usr/bin:/bin
UnsetEnvironment=DESKTOP_SESSION XDG_CURRENT_DESKTOP XDG_SESSION_TYPE DISPLAY
NoNewPrivileges=true
# Do NOT add PrivateTmp=true: the cost gate (~/.local/bin/qoder-efficient-guard) could then not
# readlink /proc/<pid>/exe of live qodercli processes (EACCES) -> live-pid scan goes blind ->
# executor run refused (exit 75, cost_gate_refused). Bisected on the box 2026-09-12.
Restart=always
RestartSec=3s
[Install]
WantedBy=default.target
```
`systemctl --user cat goal-supervisor.service` — **RETIRED stub**:
```ini
[Unit] Description=RETIRED - Grok CLI is not Grok Bot; do not use for goal supervision
[Service] Type=oneshot
ExecStart=/usr/bin/true
```
`systemctl --user cat goal-supervisor.path` — also retired:
```ini
[Unit] Description=RETIRED - Grok CLI supervisor path disabled
[Path] PathExists=/home/person/.local/state/remote-agent/DO-NOT-ENABLE-GROK-CLI-SUPERVISOR
       Unit=goal-supervisor.service
```
`goal-supervisor.path` state `inactive dead static`; the `DO-NOT-ENABLE-...` sentinel was absent from the
`~/.local/state/remote-agent/` listing, so the path unit could never trigger regardless.

`systemctl --user cat qoder-goal-watch.service` — the live replacement watcher:
```ini
[Service] Type=oneshot
ExecStart=/usr/bin/python3 /home/person/remote-agent-deploy/remote-agent/qoder-goal-watch.py --scan
```

## 11. Log evidence for the supervisor transition
`/work/logs/goal-supervisor.log` — stale since **2026-09-16T18:21**, ending in Grok quota failure:
```
2026-09-16T18:20:59.602362+09:00 grok_failed reason=quota_exhausted code=1 detail=API error (status 402 Payment Required): Grok Build usage balance exhausted
2026-09-16T18:20:59.785727+09:00 review_failed worker=qoder reason=quota_exhausted alerted=True (posted lobby ts=1.5)
2026-09-16T18:21:00.368908+09:00 reviewed worker=qoder decision=stop no_dispatch cost_usd=0.02 posted=True
2026-09-16T18:21:00.370703+09:00 scan workers=1 actions=1 acted=1 spend_today=0.02
```
Earlier in that file: `decision_invalid worker=qoder problem=no evidence`, `escalated worker=qoder reason=plan_gate`.
`/work/logs/qoder-goal-watch.log` last lines:
```
2026-09-17T01:06:23+09:00 codex_healer_spawn_failed Command '[.../codex-goal-healer.py', '--scan']' timed out after 30 seconds
2026-09-17T08:53:31+09:00 notify_park worker=minecraft detail=goal_budget delivered=True (posted fleet)
2026-09-17T09:01:52+09:00 notify_recovery worker=minecraft detail=active delivered=True (posted fleet)
```
→ `qoder-goal-watch` is what spawns `codex-goal-healer.py` (whose own timer is disabled).
`/work/logs/qoder-efficient-guard.log` current, healthy on the live qoder PID:
`2026-09-18T14:28:03Z check status=free price_factor=0.0 source=pid:574580`.
Deployed code: `/home/person/remote-agent-deploy/remote-agent/` — `qoder-goal-watch.py`,
`codex-goal-healer.py`, `goal-supervisor.py` (present but unwired), `grokbot-goal-event.py`,
`anyrouter-sse-shim.py`, plus `config/`, `scripts/`, `tests/`, `devcodex/`.

## 12. Pipeline as observed
```
60s qoder-goal-watch -> qoder-goal-watch.py --scan -> spawns codex-goal-healer.py; posts Slack #fleet
60s worker-health | qoder-efficient-guard (cost gate on qodercli PIDs) | 3min qoder-nudge (/goal)
daily 09:00 slack-brief -> #lobby
daemons: orca-serve :6768 | slack-bridge Socket Mode | devspace :7676 | obscura-cdp :9222 | status-http :9091
retired: goal-supervisor(+.path)=/usr/bin/true | grokbot-goal-event.path disabled | codex-goal-healer.timer disabled
absent:  codexpro-handoff | glm-proxy
```
UNVERIFIED (could not confirm from the box):
- `devcodex` version (binary rejects `--version`).
- Contents of `~/.config/remote-agent/health.env` / `slack.env` — deliberately unread (secrets policy).
- Contents of `slack-bridge-state.json`, `qoder-goal-watch.json`, `codex-goal-healer.json`, `health-state.json`.
- Whether the NOT_FOUND `qodercli`/`qoder-efficient` results matter at runtime: the unit uses explicit
  `PATH=/usr/local/bin:/usr/bin:/bin` and calls `~/.local/bin/qoder-efficient`; failure is a probe-shell
  PATH artifact, not evidence of removal.
- `/opt/spectre-xt-worker/RUNBOOK.md` and `/usr/local/bin/spectre-slack-bridge` not read.
- Whether the ~9-minute Socket Mode reconnect cycle is expected or a fault (no error events logged).
- Purpose of `pugc`/`pugc-ade` and `~/wt/release-readiness-spectre` beyond their paths.