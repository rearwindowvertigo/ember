# ADR 0004: Squash worker branches under a lead-written subject

- Status: Accepted, 2026-11-03.
- Release: Ember 0.13.0.
- Issue: #94.

## Context

An editing run commits each turn under the brief's first non-empty line,
with a turn suffix after turn 1. Keeping those commits in the target branch
passes each turn subject to Release Please. A commit override in a merged
pull request's body applies to every commit the pull request brings to
`main`, including its merge commit.

Design 0002 records the verdict in an `Ember-Verdict` trailer on a merge
commit.

## Decision

1. `emb merge` squashes an Ember run branch, including a flow task branch,
   into one commit on the target. The lead supplies a required, nonblank
   one-line `--subject` in the repository's commit convention. A missing or
   invalid subject exits 64 with a `next:` line. Completed flow branches
   and other branches retain merge commits.
2. `git merge-tree --write-tree` computes the tree without changing the
   target's index or worktree. `git commit-tree` writes a commit whose parent
   is the target tip. Ember fast-forwards the target in the worktree where
   it is checked out. A conflict exits 75 naming the files and changes
   nothing; there is no rollback state.
3. The squash message keeps distinct Git trailers from the worker commits
   since the common base, oldest first, with identical key/value pairs once
   and keys compared without case. Git's trailer grammar and configured
   separators apply. Keys beginning with `Ember-`, without regard to case,
   are Ember's and are not copied. Ember writes `Ember-Verdict` once with
   the accepted verdict and `Ember-Task-Tip` once with the actual worker tip.
4. Every task in a new flow plan requires a nonblank `subject`. It becomes
   the task's squash subject; the accepted review's recorded verdict becomes
   `Ember-Verdict` (`keep` when no review is recorded). Existing flow records
   retain their resolved subjects.
5. Flow recovery and cleanup find a landed worker tip through a reachable
   commit's `Ember-Task-Tip` trailer. Containment also accepts ancestry or a
   conflict-free trial merge that produces the target's tree.
6. The squash honours `commit.gpgsign` with `git commit-tree -S` and runs
   the target repository's `commit-msg` hook on the message. A refusal exits
   77 and moves nothing. Worker `pre-commit` hooks are not run again.
7. Ember warns when worker history marks a breaking Conventional Commit
   and the landed subject has no `!` before its colon. The lead writes the
   landed subject, including its breaking marker.

## Consequences

Three worker turns land as one commit on the target. A flow with two
accepted tasks holds one squash commit per task. Cleanup recognizes the
worker branches after later changes on the target through their recorded
tips. The verdict trailer is on each task's squash commit and on a completed
flow's merge commit.
