---
type: llm
focus: trace
weight: 1
---

Claude's message at the stop lists all five printed choices and says in words what each one does: `--send` sends a correction to the same writer; `--accept` accepts the task branch's current tip without another review, and with `--commit <sha>` accepts that reviewed commit instead of the tip; `--review` starts fresh reviews; `--worker` starts another writer; `--drop` drops the task. Both effects of `--accept [--commit <sha>]` are stated: repeating the printed syntax without saying what `--commit` does is not enough.

Its recommendation names one of the five commands, or names a behaviour and says which of the five commands carries it out.
