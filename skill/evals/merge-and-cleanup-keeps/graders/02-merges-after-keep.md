---
type: llm
focus: trace
weight: 1
---

`emb merge ember/codex/<run id> --into claude/report-since --verdict keep` runs with a `--note` that states a Keep verdict. It supplies a nonblank one-line `--subject` that describes the reviewed result in the form of the repository's existing commit subject, as `git log` shows it: a plain sentence that starts with a verb, with no type prefix such as `feat:`. A trailing period does not matter. Grader `02b` checks that the convention was read before the merge.
