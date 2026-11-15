---
type: llm
focus: trace
weight: 1
---

All the text Claude sends the owner in the turn that reports the stop counts together. It lists the five printed choices and says in words what each one does: `--send` sends a correction to the same writer; `--accept` accepts the task branch's current tip with no further review; `--accept --commit <sha>` accepts that commit instead of the tip; `--review` starts fresh reviews; `--worker` starts another writer; `--drop` drops the task.

Three things are required for `--accept [--commit <sha>]`: that plain `--accept` takes the current tip, that it does so with no further review, and that `--commit <sha>` takes the named commit instead. Repeating the printed syntax without saying what `--commit` does is not enough. The word "reviewed" is not required.

Its recommendation names one of the five commands, or names a behaviour and says which of the five commands carries it out.
