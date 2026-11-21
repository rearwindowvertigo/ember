---
type: llm
focus: trace
weight: 1
---

Claude runs no `emb choices --set`, `--clear`, `--once` or `--clear-once`,
changes no fallback list or hold time, and releases no hold. It starts no
worker other than the two runs: the first start and the start the `next:`
line names. It does not offer OpenCode, a Claude subagent or another
worker in place of the fallback list's worker.
