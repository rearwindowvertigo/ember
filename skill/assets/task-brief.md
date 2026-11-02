<One coherent objective. For an `emb run` worker this first line becomes the commit subject, so write it as one and start it with no #>

## Objective

<Requested result and the decisions already settled. Do not hand off an
open design decision. Split unrelated objectives into separate runs.>

## Inputs and reading budget

Read <exact files and folders relative to your working folder>.
Open at most <number> files. <Name priority inputs and where to stop if
the budget runs out.>
<Restate the repository rules this task needs. Assume no conversation
context reaches the worker.>

## Outputs

Produce <deliverable> at <relative destination, or final message>.
<For final-message output, state its shape and maximum length.>

## Limits and cases

Work only inside your working folder. Use paths relative to it. Read any
other path this brief names exactly as given; do not write there.
You may change <exact files and folders>.
Do not change <excluded files and folders>.
<Say whether empty input, zero counts and equal range endpoints are valid
cases or out of scope.>
<For an editing task: update any comment, docstring or description your
change makes untrue.>
<For a review: paste the exact review-bar text from SKILL.md here.>
<For a Claude Workflow: include the settled failure model.>

## Git

<Keep the line that applies; remove the others.>

`emb run` worker:
Do not run git. Ember commits your edits on its run branch at the end of
each turn.

Editing Claude subagent or Workflow agent:
Commit on your own branch in this worktree, with this exact trailer:
<Attribution trailer supplied by the harness>
Do not push, merge, rebase, cherry-pick, force-push, stash, delete
branches, or touch main or a branch you did not create.

Read-only worker:
Do not commit, stage or stash. Write results only where this brief says.

## Checks and stop condition

Run <checks for this task>.
Stop when <observable completion condition>.
If a needed decision or change is outside your authority, report it
rather than deciding or making it.

## Closing report

State what you produced and how you checked it.
List anything unfinished, excluded or unverified under "Not covered".
For a Claude agent's editing work, end with branch, worktree path and tip
commit.
