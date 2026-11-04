---
type: llm
focus: trace
weight: 1
---

Every `emb choices --claim` passes `--skill` with the version from the skill text Claude loaded and an id matching `[a-z0-9][a-z0-9-]{5,63}`. The claim runs before any worker question or start.
