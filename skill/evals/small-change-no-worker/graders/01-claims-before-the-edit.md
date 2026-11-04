---
type: llm
focus: trace
weight: 1
---

`emb choices --claim --id <id> --skill <loaded version>` runs before README.md is edited.

Every claim, including a retry, passes the version from the loaded skill text. A `skill: current` reply continues without a reload.
