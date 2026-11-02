---
type: llm
focus: trace
weight: 1
---

After the run's turn ends and before the merge, `scripts/verify_branch.py` runs with `--base claude/report-since`, `--branch` the run's branch `ember/codex/<run id>` and `--worktree` the run's worktree.
