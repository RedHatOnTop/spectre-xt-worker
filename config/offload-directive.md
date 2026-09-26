# Heavy work is offloaded, never run here

This box is an **agent runtime, not a compile farm**, and that is enforced, not
advice: `spectre-thermal-guard` samples every 60 s and **SIGTERMs** build-class
work (compilers, Gradle/JVM daemons and test workers, cargo/rustc, npm/pnpm/yarn
builds and installs, pytest/tox, `podman|docker|buildah build`) at every
temperature, and stops memory- or CPU-intensive non-agent processes as well. It
reports each stop to Slack `#fleet`.

Anything CPU- or memory-intensive runs in the **Lightning Studio** instead:

```sh
spectre-offload --repo <dir> [--setup '<one-time command>'] \
  [--artifact <path>]... -- <command...>
```

`spectre-offload` starts the Studio, syncs the repo, runs the command there,
copies artifacts back, **deletes the uploaded tree** (Studio storage is billed
above the first 10 GB) and stops the Studio. `spectre-offload --studio-report`
shows the Studio's footprint; `--prune-caches` cleans the build caches too.

Rules for every agent session on this machine:

1. **Before starting** any build, compile, test suite, container build, dataset
   job, model run, or anything expected to hold a core for more than ~60 s or use
   more than ~512 MB RSS: run it through `spectre-offload`.
2. Local work stays **I/O-shaped**: reading, searching, editing, git, API calls,
   small scripts, Slack, service checks. If you are unsure whether a command is
   heavy, it is — offload it.
3. **Do not work around the guard** (no `nice`, no backgrounding a build, no
   running it from another worktree, no renaming a build to look like an editor).
   A blocked build is the guard working correctly.
4. Offloading is part of the task, not an optimisation. If a goal needs a build
   or a long test run, the goal is not done until it ran in the Studio.
5. Long offloads that may outlive your shell: `setsid nohup spectre-offload … &`
   and read the JSON result; the wrapper's EXIT trap still cleans up.

Recipes, limits (free 4 vCPU Studio, 4-hour session cap, 10 GB free storage) and
the verification ladder: **RUNBOOK §7.21**. Guard internals: **RUNBOOK §7.20**.
