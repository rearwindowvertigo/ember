---
type: llm
focus: trace
weight: 1
---

Claude reads `emb run --status --json` and uses each run's `turns` array
for its count. It reports the first run's turn 1 provider error (exit 73)
and turn 2 success (exit 0), with one known provider error overall.
Its count does not use only `last_exit`, `last_reason` or summary lines.

Claude identifies `usage_limit` on turn 1 and the capacity attempt before
turn 2 succeeds. It keeps one known failed provider turn and reports the
failed capacity attempt separately, without counting it as another turn.
