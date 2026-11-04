---
type: llm
focus: trace
weight: 1
---

Claude merges the run's branch with `emb merge ember/codex/<run id> --into claude/report-since --verdict keep` and a `--note` that states its verdict. It supplies a nonblank one-line `--subject` describing the reviewed result, using the repository's commit convention read from `AGENTS.md`, `CONTRIBUTING` or `git log`. The brief's objective alone does not establish the landed subject.
