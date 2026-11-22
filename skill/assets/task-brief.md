<One coherent objective, at most 72 characters, with no #. Ember uses this
line for worker-turn subjects; the lead writes the landed subject at merge.>

## Objective

<Requested result and the decisions already settled. Do not hand off an
open design decision. Split unrelated objectives into separate runs.
Mark each acceptance line with its source: (request), (rule: <file>),
(decision: <where recorded>) or (suggestion, optional). A suggestion is
not an acceptance requirement.>

## Inputs and reading budget

Read <exact files and folders relative to your working folder>.
Open at most <number> files. <Name priority inputs and where to stop if
the budget runs out.>
<Restate the repository rules this task needs. Assume no conversation
context reaches the worker.>
<The worker cannot open an issue tracker. Put each source this brief cites
here, or export it to a file in the scratchpad, pass it with `--read`, and
name that file here.>

## Outputs

Produce <deliverable> at <relative destination, or final message>.
<For final-message output, state its shape and maximum length.>

## Limits and cases

Work only inside your working folder. Use paths relative to it. Read any
other path this brief names exactly as given; do not write there.
You may change <exact files and folders>.
Do not change <excluded files and folders>.
If a tool reports a denied path, include the path and error in your report.
Do not add it to `sandbox.read` or change sandbox settings.
<Say whether empty input, zero counts and equal range endpoints are valid
cases or out of scope.>
<For an editing task: update any comment, docstring or description your
change makes untrue.>
<For a flow: name any shared-file edits assigned to a later task or to the
lead while merging, and exclude those files here. Keep shared edits needed
by this task's own checks in this task's files.>
<For a review: paste the exact review-bar text from SKILL.md here.>
<For a Claude Workflow: include the settled failure model.>

## Git

<Keep the line that applies; remove the others.>

`emb run` worker:
You may run git commands that only read, such as `git log`, `git show`,
`git diff`, `git status` and `git blame`. Run no git command that changes
the index, a tracked file's state, a branch or a ref: Ember commits your
edits on its run branch at the end of each turn.

Editing Claude subagent or Workflow agent:
Commit on your own branch in this worktree, with this exact trailer:
<Attribution trailer supplied by the harness>
Do not push, merge, rebase, cherry-pick, force-push, stash, delete
branches, or touch main or a branch you did not create.

Read-only worker:
Read the repository in your own working folder, using relative paths.
For `emb run --read-only` and panel members, this is a detached worktree
at the recorded commit; uncommitted source edits are absent.
You may install dependencies in your own detached worktree to run checks,
without changing tracked files. Otherwise create, edit or delete no file,
and change nothing in git.
Put your whole answer in your final message.

## Checks and stop condition

Run <checks for this task>.
<Choose one. Use "Run the repository's full test suite, or name why not."
When the full suite is long and the lead or CI runs it on the result, use
"Run only <named checks>, or name why not. Do not run the full suite.
<Lead or CI> runs it on the result.">
<Name checks the worker cannot run in its sandbox, why, who runs them, and
the constraints, grants and fixture prerequisites those checks exercise.>
For a test someone else runs against a database: before writing it, read
each check constraint, trigger and grant on every table its fixture
writes; create every row the test needs; confirm each table it names
exists in the current schema. Name in your closing report anything of
these you could not read.
Search for other tests that reference what this task changes.
Report failures in files outside the allowed set without editing them.
Stop when <observable completion condition>.
If a needed decision or change is outside your authority, report it
rather than deciding or making it.

## Closing report

State what you produced and how you checked it.
List anything unfinished, excluded or unverified under "Not covered".
For a Claude agent's editing work, end with branch, worktree path and tip
commit.
