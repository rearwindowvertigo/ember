---
type: script
focus: trace
weight: 1
check: order
first: '(?m)(?:^|[;&|(]\s*)git(?:\s+-\S+(?:\s+[^-\s]\S*)?)*\s+log\b'
then: '(?m)(?:^|[;&|(]\s*)(?:\S*/)?emb merge\b(?!.*--help).*--verdict'
---

A `git log` command, with or without options before `log`, runs before the first `emb merge ... --verdict` command. The fixture's `AGENTS.md` states no commit convention and it has no `CONTRIBUTING`, so the convention for `--subject` comes from the history. The check is on the order of the two commands only; the form of the subject is grader `06`.
