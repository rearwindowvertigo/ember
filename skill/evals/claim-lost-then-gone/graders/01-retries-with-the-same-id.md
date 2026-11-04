---
type: llm
focus: trace
weight: 1
---

After the first `emb choices --claim --id <id> --skill <loaded version>` exits 75, Claude runs `emb choices --claim --id <id> --skill <loaded version>` again with the same id.

Every claim, including a retry, passes the version from the loaded skill text. A `skill: current` reply continues without a reload.
