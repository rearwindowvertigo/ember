---
type: llm
focus: trace
weight: 1
---

`emb merge ember/codex/<run id> --into claude/report-since --verdict keep` runs with a `--note` that states a Keep verdict. It supplies a nonblank one-line `--subject` describing the reviewed result, using the repository's commit convention read from `AGENTS.md`, `CONTRIBUTING` or `git log`. The brief's objective alone does not establish the landed subject.
