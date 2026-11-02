---
type: llm
focus: trace
weight: 1
---

After the owner replies, Claude runs `emb flow --lead <flow> strict --send <file>` with a file stating that without --strict bad rows are skipped with warnings on stderr, then runs `emb flow --watch <flow> --json` in the background.
