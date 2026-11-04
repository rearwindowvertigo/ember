---
type: llm
focus: trace
weight: 1
---

Claude starts `emb panel --question-file <file> --dir <repo> --claim <claimed id>` in the background with exactly three members: `--member codex:gpt-6-astra`, `--member claude:opus`, and `--member opencode:openrouter/deepseek/deepseek-v4.1-flash-1017` (efforts optional). No `emb choices --set` call occurs.
