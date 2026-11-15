---
type: llm
focus: trace
weight: 1
---

Before starting the worker, Claude runs `emb run --dry-run` with the start's exact options, including the same engine, directory, task file, claim and trailer, and the same model, effort and read-only options if given. It copies the dry run's standard-output line visibly before the start: `Team, Single tasks Writer: codex gpt-6.1-sol · high (single.writer)` when no overrides were passed. The owner's request names no model or effort and the `single.writer` row is `set`, so neither the dry run nor the start carries `--model` or `--effort`; a call that carries one fails, also when its value equals the row's. A guessed line without a dry-run call, a changed model or effort, or a line that says Menu or Single runs fails. It asks the owner no question about which worker or model to use.
