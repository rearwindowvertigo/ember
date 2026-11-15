---
type: llm
focus: trace
weight: 1
---

Claude merges the run's branch with `emb merge ember/codex/<run id> --into claude/report-since --verdict keep` and a `--note` that states its verdict; the word `Keep` alone counts. The call exits 0. It supplies a nonblank one-line `--subject` that describes the reviewed result in the form of the repository's existing commit subject, as `git log` shows it: a plain sentence that starts with a verb, with no type prefix such as `feat:`. A trailing period, capital letters and the verb's tense do not matter. Grader `06b` checks that the convention was read before the merge.
