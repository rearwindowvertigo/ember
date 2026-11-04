---
type: llm
focus: trace
weight: 1
---

Claude claims with `emb choices --claim --id <id> --skill <loaded version>` before any choices read, flow dry run or gap check.

Every claim, including a retry, passes the version from the loaded skill text. A `skill: current` reply continues without a reload.
