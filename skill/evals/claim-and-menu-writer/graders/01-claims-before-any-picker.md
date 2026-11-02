---
type: llm
focus: trace
weight: 1
---

`emb choices --claim --id <id>` runs before any question to the owner about the worker, before any other `emb choices` call, and before `emb run` starts. The id matches `[a-z0-9][a-z0-9-]{5,63}`.
