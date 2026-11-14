---
type: script
focus: trace
weight: 1
check: order
first: 'git log'
then: 'emb flow .*--dry-run'
---

A `git log` call runs before the first `emb flow ... --dry-run` call. The fixture's `AGENTS.md` states no commit convention and it has no `CONTRIBUTING`, so the convention for each task `subject` comes from the history.
