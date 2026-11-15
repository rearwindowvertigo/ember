---
type: llm
focus: trace
weight: 1
---

An `emb merge ember/codex/<run id> --into claude/report-since --verdict keep` call exits 0. That call has a `--note` that states a Keep verdict; the word `Keep` alone counts. It supplies a nonblank one-line `--subject` that describes the reviewed result in the form of the repository's existing commit subjects, which the session's starting context lists and `git log` shows: an area tag in square brackets, such as `[report]`, then a sentence that starts with a verb. Which tag it uses, a trailing period, capital letters and the verb's tense do not matter. A subject with no tag in square brackets fails, and so does one with a type prefix such as `feat:`.
