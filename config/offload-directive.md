
This box is an **agent runtime, not a compile farm**, and that is enforced, not
advice: `spectre-thermal-guard` samples every 60 s and **SIGTERMs** build-class
work (compilers, Gradle/JVM daemons and test workers, cargo/rustc, npm/pnpm/yarn
builds and installs, pytest/tox, `podman|docker|buildah build`) at every
temperature, and stops memory- or CPU-intensive non-agent processes as well. It
reports each stop to Slack `#fleet`.

Anything CPU- or memory-intensive runs on a throwaway **AWS spot instance**
instead (`aws-burst`):

```sh
burst-offload --repo <dir> [--rust <toolchain>] [--setup '<command>'] \
  [--type <instance type>] [--artifact <path>]... -- <command...>
```

`burst-offload` (`~/.local/bin`, source `scripts/burst-offload.sh`) starts an
Amazon Linux 2023 instance, installs gcc, git and rsync (rustup with clippy and
rustfmt when `--rust` names a toolchain), pushes the repo's tracked and
unignored files (uncommitted edits included, `target/` and `node_modules/`
not), runs the command there, copies `--artifact` paths back into the run
directory and terminates the instance. The command's output goes to stderr and
the run log; stdout ends with one JSON line (`ok`, `exit`, `instance`,
`seconds`, `log`, `artifacts`). An instance powers itself off after `--ttl`
minutes (default 90) or 10 idle minutes, so a crashed run cannot leave one
running for long. Every instance is fresh: dependencies download and compile
each run (a two-crate Rust test + clippy took 211 s on the default
`c7i-flex.large`, 2 vCPU / 4 GiB; use `--type c7i.xlarge` or larger for big
builds). `aws-burst status` lists running instances and the credit left;
`aws-burst down --all` ends them.

The older route, `spectre-offload` (Lightning Studio, RUNBOOK §7.21), works
only while the Lightning login is valid; prefer `burst-offload`.

Rules for every agent session on this machine:

1. **Before starting** any build, compile, test suite, container build, dataset
   job, model run, or anything expected to hold a core for more than ~60 s or use
   more than ~512 MB RSS: run it through `burst-offload`.
2. Local work stays **I/O-shaped**: reading, searching, editing, git, API calls,
   small scripts, Slack, service checks. If you are unsure whether a command is
   heavy, it is — offload it.
3. **Do not work around the guard** (no `nice`, no backgrounding a build, no
   running it from another worktree, no renaming a build to look like an editor,
   no allow-file entry for a build tool). A blocked build is the guard working
   correctly.
4. Offloading is part of the task, not an optimisation. If a goal needs a build
   or a long test run, the goal is not done until it ran off the box.
5. Long offloads that may outlive your shell:
   `setsid nohup burst-offload … > result.json 2> run.err &` and read the JSON
   result; the wrapper's EXIT trap still terminates the instance.

Guard internals: **RUNBOOK §7.20**. Lightning recipes and limits: **RUNBOOK §7.21**.
