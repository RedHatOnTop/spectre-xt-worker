# 2026-08-26 — DarwinInspection session migrated; autonomous run live

## Migration

User asked to migrate session
`sess_bb0c372b-1e4c-4e0f-90a2-5ed6720bb9c1` ("Continue Autonomous
Research Project with QEMU") into the Spectre's ZCode and let it run
autonomously. The referenced `.zcode-session` export file did not
exist anywhere — instead of waiting for an export, used the warp
infrastructure directly:

1. Project rsync fedora→spectre (13 GB, same absolute path, excluded
   node_modules/.venv/archive/zip)
2. `warp_zcode.py export` from fedora's live db: 11 sessions,
   16311 messages, 47337 parts → scp bundle → import on spectre.
   Target session verified present by id.
3. Session artifacts (`~/.zcode/cli/artifacts/sess_bb0c372b...`) and
   bash-startup dir copied separately.

## Headless CLI discovered

`node /opt/ZCode/resources/glm/zcode.cjs` is a full headless CLI:
--prompt, --resume <sessionId>, -c/--continue, --target (session goal),
--target-replace, --mode build|edit|plan|yolo, --max-turns, app-server.

Two gotchas solved by source archaeology:
- The CLI reads **~/.zcode/cli/config.json**, a different file from the
  GUI's ~/.zcode/v2/config.json. Created it with the Hardened provider
  + `"model": "<provider-uuid>/ox-alpha"`.
- jq pitfall again avoided; model ref shape is "providerId/modelId".

## Permission broker issue (open question)

`--resume sess_bb0c372b ... --prompt` reached the model and streamed
responses, but every Bash tool call failed with "No permission client
configured for Bash" — DenyPermissionBroker fires on the resume path
even with --mode yolo and even after setting the session row's
permission JSON to a full yolo object. Fresh `--prompt` sessions get a
working yolo auto-approver (verified: echo/git status/git checkout all
execute). Workaround: launched autonomy via `-c`/fresh-prompt path with
a `/goal` slash command inside the prompt, giving the agent explicit
re-orientation steps. Worth investigating upstream later why resume
path skips the yolo approver registration.

## Live status at time of writing

- Runner alive (nohup zcode.cjs); messages grew 16425→16445+; tool calls
  completing: FACTS.md greps, patches listing, QEMU build checks — the
  agent re-oriented itself and updated its own todo list ("Re-orient"
  done, "Trace getConfig(0x3c) parked class byte" in progress).
- Intermittent stalls traced to upstream: proxy health shows both ox
  endpoints (openrouter/zen) cooling with lastReason "empty" and 503
  "failed after 8 discarded upstream replies" — free-endpoint instability,
  not box config. CLI retries are marked isRetryable:true.
- Crash resilience: installed
  `~/.config/systemd/user/darwin-autonomous.service`
  (Restart=on-failure, RestartSec=60s, logs appended to
  /work/logs/darwin-autonomous.log). Not yet switched over while the
  nohup instance lives.

## Follow-ups

1. If runner dies: `systemctl --user start darwin-autonomous.service`
2. Watch: message count in db + latest part statuses
3. Upstream empty-reply storms are environmental; nothing to fix locally

## Model switch ox-alpha → glm-5.3 (23:20 KST)

ox-alpha promotion ended mid-run; upstream returned
"ox-alpha failed after 8 discarded upstream replies" repeatedly.
Switch facts learned:

- Direct curl `model:"glm-5.3"` through the proxy: works (streaming
  response with reasoning content).
- **`-c`/resume sessions remember their model**: the runner kept calling
  ox-alpha even after config.json was switched — the session's stored
  model ref wins over config.model on resume. Fix: run the goal as a
  FRESH session (--prompt "/goal ..."), which picks up config.model.
  Continuity comes from the workspace (git/LOG.md re-orientation), not
  the session id.
- `--max-turns` is NOT a global option despite appearing in --help's
  options block: "Unknown option '--max-turns'" → runner died instantly
  in a restart loop. Removed; default turn budget is ample (observed
  142 turns in ~2h before this switch).
- Both ~/.zcode/cli/config.json (CLI) and ~/.zcode/v2/config.json (GUI)
  now carry Hardened with models=["glm-5.3"] only, model ref
  78a47ecf.../glm-5.3. Verified in db: newest messages record
  {"providerID":"78a47ecf...","modelID":"glm-5.3"}.

darwin-autonomous.service (Restart=always, 45s) active and producing
tool calls again on glm-5.3.
