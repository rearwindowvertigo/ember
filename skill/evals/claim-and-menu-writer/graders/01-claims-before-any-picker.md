---
type: llm
focus: trace
weight: 1
---

`emb choices --claim --id <id> --skill <loaded version>` runs before any question to the owner about the worker, before any other `emb choices` call, and before `emb run --dry-run` or a start. The id matches `[a-z0-9][a-z0-9-]{5,63}`.

Every claim, including a retry, passes the version from the loaded skill text. A `skill: current` reply continues without a reload.
