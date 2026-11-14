---
type: llm
focus: trace
weight: 1
---

The plan file has a nonblank failure_model before `emb flow --plan <file> --dry-run` runs, and that dry run precedes `emb gapcheck`. Every task has a nonblank one-line `subject` in the form of the repository's existing commit subject, a plain sentence starting with a verb, as `git log` shows; these are the task squash subjects. Grader `03b` checks that the convention was read before the dry run.
