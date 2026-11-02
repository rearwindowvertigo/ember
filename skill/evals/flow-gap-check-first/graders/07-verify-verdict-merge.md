---
type: llm
focus: trace
weight: 1
---

After the end report Claude runs verify_branch.py on the flow branch and worktree, runs its own checks, gives a Keep verdict, then runs `emb merge ember/flow/<id> --into claude/flow-gap-check-first --verdict keep` with the repository directory.
