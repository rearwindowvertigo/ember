---
type: llm
focus: trace
weight: 1
---

`emb choices --claim --id <id> --skill <loaded version>` runs before the answer about the worker and before any other `emb choices` call.

Every claim, including a retry, passes the version from the loaded skill text. A `skill: current` reply continues without a reload.
