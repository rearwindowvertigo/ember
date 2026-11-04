---
type: llm
focus: trace
weight: 1
---

Claude claims with `emb choices --claim --id <id> --skill <loaded version>` before reading choices or starting the panel.

Every claim, including a retry, passes the version from the loaded skill text. A `skill: current` reply continues without a reload.
