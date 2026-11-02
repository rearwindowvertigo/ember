---
type: llm
focus: trace
weight: 1
---

Both `emb flow --dry-run` and `emb flow --start` explicitly carry `--writer codex:gpt-6-astra@high` and `--reviewer codex:gpt-6-astra@high`. Naming workers only inside the plan does not satisfy this expectation.
