---
type: llm
focus: trace
weight: 1
---

The plan file has a nonblank failure_model before `emb flow --plan <file> --dry-run` runs, and that dry run precedes `emb gapcheck`. Every task has a nonblank one-line `subject` in the form of the repository's existing commit subject, as `git log` shows it: a plain sentence that starts with a verb, with no type prefix such as `feat:`. A trailing period does not matter. These are the task squash subjects. Grader `03b` checks that the convention was read before the dry run.
