---
type: llm
focus: trace
weight: 1
---

After the end report Claude runs verify_branch.py on the flow branch and worktree, runs its own checks, then runs `emb merge ember/flow/<id> --into claude/flow-gap-check-first --verdict keep` with a `--note` that states a Keep verdict and with the repository directory.
