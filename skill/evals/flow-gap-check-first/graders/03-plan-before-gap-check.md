---
type: llm
focus: trace
weight: 1
---

The plan file has a nonblank failure_model before `emb flow --plan <file> --dry-run` runs, and that dry run precedes the first `emb gapcheck --plan` call. Every task has a nonblank one-line `subject` in the form of the repository's existing commit subjects, which the session's starting context lists and `git log` shows: an area tag in square brackets, such as `[report]`, then a sentence that starts with a verb. Which tag it uses, a trailing period, capital letters and the verb's tense do not matter. A subject with no tag in square brackets fails, and so does one with a type prefix such as `feat:`. These are the task squash subjects.
