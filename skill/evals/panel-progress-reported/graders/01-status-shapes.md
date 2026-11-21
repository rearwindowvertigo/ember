---
type: llm
focus: trace
weight: 1
---

Claude reads `emb panel --status --json` before inspecting the listed panel twice with `--status <id> --json`. It uses the list's member_count of 3 and running_members with members 2 and 3, and the one-panel members array to identify the answered Codex member and running Claude and OpenCode members. It does not treat member_count as an array or expect members in the list.
