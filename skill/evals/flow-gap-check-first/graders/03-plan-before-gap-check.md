---
type: llm
focus: trace
weight: 1
---

The plan file has a nonblank failure_model before `emb flow --plan <file> --dry-run` runs, and that dry run precedes `emb gapcheck`. Every task has a nonblank one-line `subject` in the repository's commit convention, read from `AGENTS.md`, `CONTRIBUTING` or `git log`; these are the task squash subjects.
