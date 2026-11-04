---
type: llm
focus: trace
weight: 1
---

Before starting the worker, Claude runs `emb run --dry-run` with the start's exact options, including the same engine, directory, task file, claim and trailer, and the same model, effort and read-only options if given. It copies the dry run's standard-output line visibly before the start: `Team, Single tasks Writer: codex gpt-6.1-sol · high (single.writer)` when no overrides were passed. A guessed line without a dry-run call, a changed model or effort, or a line that says Menu or Single runs fails. It asks the owner no question about which worker or model to use.
