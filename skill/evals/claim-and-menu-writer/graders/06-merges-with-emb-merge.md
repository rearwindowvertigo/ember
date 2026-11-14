---
type: llm
focus: trace
weight: 1
---

Claude merges the run's branch with `emb merge ember/codex/<run id> --into claude/report-since --verdict keep` and a `--note` that states its verdict. It supplies a nonblank one-line `--subject` that describes the reviewed result in the form of the repository's existing commit subject, a plain sentence starting with a verb, as `git log` shows. Grader `06b` checks that the convention was read before the merge.
