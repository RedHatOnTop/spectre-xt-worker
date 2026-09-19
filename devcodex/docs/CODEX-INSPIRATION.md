# Design acknowledgements

DevCodex is independently implemented, but some runtime concepts were informed by public interfaces and design ideas visible in OpenAI Codex CLI and its app-server ecosystem.

Primary reference:

- OpenAI Codex repository: https://github.com/openai/codex
- Repository license: Apache-2.0

Current DevCodex concepts with clear Codex-adjacent inspiration include structured review targets, detached review artifacts, daemon-oriented shared state, machine-readable lifecycle events, permission classification, and capability/environment discovery.

Earlier pre-1.0 versions also experimented with goal budgets, queued work, compare-and-swap steering, session forks, logical rollback, delegated model providers, and multi-provider implementation batches. Those features were intentionally removed before 1.0 because they added orchestration complexity without helping the actual single-agent DevSpace workflow enough to justify their maintenance cost.

DevCodex does not vendor or copy Codex source code. Similar names are used only where they describe common agent-runtime concepts clearly.

The 1.0 design boundary is intentionally narrower: the host model owns reasoning and edits, DevSpace owns execution primitives, and DevCodex owns compact context, durable task notes, recovery state, and evidence-backed completion.

DevCodex is not an OpenAI product and is not affiliated with or endorsed by OpenAI.
