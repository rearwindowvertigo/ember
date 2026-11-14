---
type: script
focus: trace
weight: 1
check: order
first: 'git log'
then: 'emb merge .*--verdict'
---

A `git log` call runs before the first `emb merge ... --verdict` call. The fixture's `AGENTS.md` states no commit convention and it has no `CONTRIBUTING`, so the convention for `--subject` comes from the history.
