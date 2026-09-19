# 2026-09-16 — DevSpace's advertised skill path: the `~` resolved against the workspace

## Requirement

User: "다만 별개 버그 하나는 아직 안 고쳐졌습니다. DevCodex가 제공하는 skill
경로 `~/.agents/skills/devcodex/SKILL.md`를 아직도
`/home/person/Projects/devspace-demo/~/.agents/...` 처럼 워크스페이스 상대
경로로 해석해서 ENOENT를 냅니다. 권한 수정은 완료됐지만 skill 경로 처리 버그는
잔존합니다. 이거 스펙터 박스에서 수정좀."

The same-day guard relaxation (`2026-09-16-agent-guard-relaxation.md`) widened
the DevSpace roots (its item 3) and closed the *permission* half of the problem
— the file tools could now reach `~/.agents` at all — but left the path bug
that the 2026-09-14 connector work had recorded as a "tilde-path read quirk".
The user asked for the box itself to be fixed, not just documented.

## Root cause

`@waishnav/devspace` 1.0.8, `dist/roots.js`:

```js
export function resolveAllowedPath(inputPath, cwd, allowedRoots) {
    const absolutePath = resolve(cwd, inputPath);   // `~/x` is not absolute
    return assertAllowedPath(absolutePath, allowedRoots);
}
```

`dist/server.js` advertises every skill through `formatPathForPrompt()`
(`dist/skills.js`), which rewrites a home path to the tilde form
(`~/.agents/skills/devcodex/SKILL.md`), and the model is told to `read`
exactly that advertised path. `dist/pi-tools.js` hands the value returned by
`resolveAllowedPath()` to the pi-coding-agent read/write/edit tools. Since
`~` is not absolute, `resolve(cwd, '~/...')` produced
`<workspace>/~/.agents/skills/devcodex/SKILL.md`:

- on the box, `/home/person/Projects/devspace-demo/~/.agents/skills/devcodex/SKILL.md`
  → **ENOENT** on the advertised path, which the model then had to guess
  around with the expanded absolute one;
- and a *silent wrong target* rather than an error for `write`/`edit` with a
  tilde path: the file landed under the workspace instead of `$HOME`.

The gate was the only resolver on that path that missed `~` — DevSpace's own
skill loader (`dist/skills.js` `resolveSkillReadPath`) and the
pi-coding-agent tools underneath (`utils/paths.js` `normalizePath`, used by
`resolveToCwd`) both expand it. That asymmetry is exactly why the
`$HOME`-expanded read worked while the advertised read did not.

## What was done (repo)

- `scripts/patch-devspace-tilde.sh` — the fix: one anchored line
  (`resolve(cwd, expandHomePath(inputPath))`), backup beside the file,
  idempotent, with a **behaviour probe** (resolve `~/probe` from an unrelated
  workspace and require an absolute home path), `--check` (exit 1 when
  unpatched), `--restore`, and a loud failure when the anchor moved (an
  upstream build that relocated the line is not half-patched).
- `tests/devspace-devcodex-e2e.mjs` — the advertised-`~` read is now a
  **required check** (it was a printed note), so the regression is pinned at
  the MCP surface the model actually uses: 14 checks now, not 13.
- `scripts/doctor.sh` — probes the *installed module's* behaviour
  (imports `dist/roots.js`, resolves `~/probe` from an unrelated workspace)
  instead of matching the file text; fails when the patch is missing.
- `scripts/bootstrap.sh` — installs the script as
  `/usr/local/bin/spectre-patch-devspace-tilde` beside `spectre-doctor`, so
  the box can re-apply after a devspace upgrade without a repo checkout (the
  box has no `remote-agent` clone).
- RUNBOOK §7.14 — a "Tilde skill paths (patched 2026-09-16)" subsection with
  the mechanism, the re-apply rule for `npm install -g`, the restore path,
  and the verify block.

## Evidence (box, 2026-09-16)

Patching (script piped over `ssh spectre`, run through `sudo bash -s`):

- `--check` before: exit 1, "still resolves '~' against the workspace root".
- direct probe of the installed module before the patch:
  `resolveAllowedPath('~/.agents/skills/devcodex/SKILL.md', '/home/person/Projects/devspace-demo', ['~'])`
  → `/home/person/Projects/devspace-demo/~/.agents/skills/devcodex/SKILL.md`
  (the reported symptom, reproduced).
- apply: `backup: .../dist/roots.js.bak-20260916`, `patched: .../roots.js`,
  `probe ok: '~' expands to $HOME, not to the workspace root`; the diff is the
  single line above. `--check` after: exit 0, "patched: ...".
- `systemctl --user restart devspace.service` → `active`; startup waited out
  (~20 s) before probing.

Verification:

- `node /tmp/devspace-devcodex-e2e.mjs` (tests/ copy) → **E2E PASS, 14/14**,
  including
  `PASS read: the advertised "~" skill path resolves to $HOME (needs
  scripts/patch-devspace-tilde.sh) (~/.agents/skills/devcodex/SKILL.md)`.
- `sudo spectre-doctor` → **59 passed, 0 failed**, with the new
  `PASS devspace file tools expand ~ (tilde patch, RUNBOOK 7.14)` inside the
  devspace section. (The 2026-09-16 morning run was 57/1; the extra pass is
  this probe, and the morning's `devspace /mcp 000` drift is green again now
  that the connector is up.)
- `sudo spectre-patch-devspace-tilde --check` (installed copy) → `patched`.
- Daily driver: `verify.sh` all gates green — `bash -n` for every script
  including the new one, `py_compile`, slack bridge tests, the vendored
  devcodex suite, 181 unit tests. Note for the record: the first local run
  failed with `ModuleNotFoundError: No module named 'encodings'` because the
  DSH launcher leaks `PYTHONHOME`/`PYTHONPATH` into the shell from its
  AppImage mount; with `env -u PYTHONHOME -u PYTHONPATH` the same run is
  green. Environment artifact, not a repo failure.

## Honest limits

- **A patched npm tree, not a fork.** `npm install -g @waishnav/devspace`
  overwrites `dist/` and silently removes the fix; the re-apply step is
  documented, `doctor` fails when it is missing, and the anchor check makes a
  relocated line fail loudly. Upstream is not fixed and has not been reported.
- **The Zenbook connector is still on the unpatched 1.0.8** (same bug, live
  ChatGPT connector). The repo e2e now reports 13 PASS + 1 FAIL there until
  the script is run on that host; that is the intended signal.
- **Only `read`/`write`/`edit` consume the resolved path.** `grep`/`find`/`ls`
  call `resolveAllowedPath` purely as an allow-list gate and pass the raw path
  to pi, which expands `~` itself, so their *gate* was wrong for tilde paths
  (it compared `<cwd>/~...`) while their behaviour was right. Left alone: the
  fix that matters is the value the file tools act on.
- **The patch widens nothing it should not**: a tilde path is now expanded
  before the allow-list comparison too, which can only turn a
  `<cwd>/~...` candidate into the `$HOME/...` path the roots were always
  meant to describe.
- The box has no `remote-agent` clone, so the two updated tools
  (`spectre-doctor`, `spectre-patch-devspace-tilde`) were installed directly;
  a future `bootstrap.sh` run from a checkout installs the same files.

## Follow-ups

1. Report the tilde-advertised-path vs `resolveAllowedPath` disagreement
   upstream (the local patch is invisible to npm and to the next upgrade).
2. Run the same script on the Zenbook connector, or retire that connector in
   the ChatGPT UI (RUNBOOK §7.14 keeps it alive until then) — after that, the
   14-check e2e is green on every host that runs it.