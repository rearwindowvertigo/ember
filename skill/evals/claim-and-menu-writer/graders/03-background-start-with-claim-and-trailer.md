---
type: llm
focus: trace
weight: 1
---

`emb run --engine codex` is started for the task as a background command (the Bash call sets run_in_background), without `--read-only`, with `--dir` and `--task-file`, and its arguments include `--claim` with the same id as the claim call and `--trailer` with an attribution line.
