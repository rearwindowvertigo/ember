# Gap check

Follow this before every flow and every Claude Workflow of two or more
tasks. Single runs and panels do not need it. The owner's Team settings
decide whether a plan is checked (Run the gap check: off, always, from a
task count, or ask) and which gaps reach the owner (Gaps Claude asks you
about). Ember applies both; do not decide them yourself.

Use the run's claim after its skill-version check (SKILL.md step 2).

Write the plan in the session scratchpad with the cases it must handle and
those it accepts: a nonblank `failure_model` string in a flow plan, or a
"Failure model" section in a Workflow plan. Keep it to the request: no
wording, docstrings or tests the owner did not ask for. A blank plan is
refused; a one-step plan is checked as one step.

A reviewer reads only the plan, its worktree and its read paths; it cannot
open an issue tracker. Put the text of each source the plan cites in the
plan, or export it to a file in the session scratchpad and pass the file
with `--read` (for a GitHub issue,
`gh issue view <n> --json number,title,state,body,comments --jq '"# #\(.number) \(.title) (\(.state))\n\n\(.body)\n\n## Comments\n\([.comments[].body]|join("\n\n---\n\n"))"' > <scratch>/issue-<n>.md`).
`emb gapcheck` and `emb flow --dry-run` warn about each citation no read file
supplies; treat that warning as a missing source. A read file that is empty
or holds only whitespace is refused with exit 64, for `emb gapcheck --read`
and for the flow plan's `read` paths; folders are not checked.

## Decide first

Settle whether the plan gets a check before choosing a reviewer or asking
any worker question:

- Flow: read `gapcheck_status` in the `emb flow --dry-run` JSON.
- Claude Workflow: read `gapcheck.when` in the choices reply you already
  hold for the run's claim, and count the plan's steps (its tasks or
  numbered steps). `off`, or a number above the step count, is `skipped`;
  `ask` is `ask`; anything else is `run`. Run no command to find out.

Then:

- `skipped`: run nothing and ask nothing. Start from the plan, and say in
  your report that it ran without a gap check and why (the dry run's
  `gapcheck_reason`, or the setting and step count).
- `ask`: put one question to the owner: run the gap check, or start without
  one. On start without, go on as for `skipped`. On run, every command below
  carries `--confirmed`.
- `run`: go on below.

## Run and read

Save the JSON result to a numbered scratch file; it is where answers are
recorded and what a repeat check reads:

```bash
emb gapcheck --plan <file> --dir <repo> --claim <id> --json > <scratch>/gapcheck-<plan stem>-1.json
```

The reviewer reads the repository in its own detached worktree at the
source checkout's `HEAD`. The brief includes the plan text, including an
uncommitted plan, and tells it to use its own working folder without
naming the source checkout. Other uncommitted repository edits are absent.

Use the run's existing claim. `emb gapcheck` uses the owner's Gap check
reviewer row (`gapcheck.reviewer`, which follows `single.reviewer` while
`same`). When the owner named the reviewer, or that row is not `set`, pass
`--engine <e> --model <m> --effort <x>` with the resolved effort when there
is one. If no reviewer was named and
the row cannot answer, ask the owner using the skill's worker question.

For a flow, run the exact `gapcheck` command from the dry run (also printed
after `gap check:`; it already carries `--json` and `--confirmed` after an
`ask`). Add `--claim <id>` when absent and the redirect above. For a Claude
Workflow, run the command above.
Should `emb gapcheck` itself report `status` `skipped` or `ask`, the
settings changed since you read them: treat it as the decision above.

Read the JSON or text before starting work. Check `parse_error`,
`uncovered_steps` and `unknown_steps`: any error or missing or unknown
step leaves the check incomplete. With `--previous`, a missing or unknown
earlier gap disposition also leaves it incomplete. Empty gap lists and
zero counts are valid only with coverage of every expected step. Exit 81: follow `next:`
to obtain a complete reply. On a `WARNING:` about a missing failure model,
add it and check again. Follow printed lines as SKILL.md step 6 says. For a CLI reviewer's
denied paths, inspect its run status and report them as `references/runs.md`
(Denied paths) says.

Check for tasks whose `files` overlap only on a shared file, even when
they edit different sections: a reference document, changelog, index or
registration point such as a command-line parser. Raise that overlap as a
gap when the shared edits can be separated from the tasks' own checks.
The default is a separate shared-file task with `after` on every
implementation task whose results it describes or registers; remove the
shared file from those implementation tasks' `files` and name the boundary
in their briefs. The lead may instead make those edits while merging.
When a task's own checks need the shared edit, keep it with that task and
serialize the overlap. A parser option exercised by the task's tests is
such a case. Keep the one-file rule; section scopes do not remove overlap.

## Claude subagent reviewer

Exit 64 naming the `--brief`/`--reply` route means the reviewer row runs
as a Claude subagent:

1. Print the brief, with `--confirmed` when the owner approved an `ask`
   check:
   `emb gapcheck --plan <file> --dir <repo> --brief [--confirmed] [--previous <result>] > <scratch>/gap-brief.md`.
2. Give it to a read-only general-purpose subagent without isolation.
   Follow `references/claude-agents.md` for its model and tools.
3. Save its final message to a scratch file, then parse it with the same
   `--previous`:
   `emb gapcheck --plan <file> --reply <reply-file> [--previous <result>] --json > <scratch>/gapcheck-<plan stem>-<n>.json`.

These two commands start no run and use no claim. Check the parsed result
for coverage and errors as above.

## Owner answers

Each gap in `steps` carries an `id` and `ask_owner`, from the owner's Gaps
Claude asks you about. Take the default of every gap whose `ask_owner` is
false: write it into the failure model, ask nothing, and list each one in
your report. For the rest,
ask each `high` gap as its own question, in task order, waiting for its
answer before the next question. Give the gap, its scenario, its underlying
cause and the other places it affects. Offer the fix at that cause first
as the recommended answer. Then offer the remaining
`medium` and `low` gaps together as one question whose recommended answer
accepts their defaults into the failure model. An empty gap list needs no
question.

Record every owner answer and every default taken with:

```bash
emb gapcheck --answer <scratch>/gapcheck-<plan stem>-<n>.json --id <gap id> --text '<answer>'
```

Ember validates and updates the result. Do not edit the JSON by hand.
The answer is stored once, on its gap in `steps` (or `settled` for an
earlier settled gap). `ranked` refers to active gaps by id, step and harm.

Before putting a `high` gap to the owner, check its factual premise
against a live source, such as a log, a rollout file, a record file or the
code it names. When the premise cannot be checked, the question to the
owner says so.

With the owner away and no answer possible, work waits. Do not take a
default for a `high` gap yourself. Write the answers into the failure
model, context or task text before `--start` or the Workflow. If answers
materially change task scope, files, dependencies or ordering, repeat the
flow dry run with `--previous <last result>` and its printed gap check
command (the gap check alone for a Workflow, with the same `--previous`).
Recording answers without such changes needs no repeat. The reviewer finds gaps;
it decides nothing.

## Repeat checks

Every repeat check passes the last result, with its answers recorded, as
`--previous <scratch>/gapcheck-<plan stem>-<n>.json`, and saves its own
result as the next number. Keep the plan stem in each result filename; report a
`WARNING:` when the earlier result names a different plan path. The result
records `plan` and `previous_plan`; check that the earlier file belongs to
this review chain.

The reviewer sees the ids, original steps, gap text and answers of every
active and settled earlier gap and gives each a `dispositions` entry:
`{id, status: settled|reopened|closed, reason}`. An unanswered gap cannot
be settled. Every current plan step still needs coverage. In the new result:

- `settled` lists answered gaps that stay answered. Ask nothing about them;
  they carry into the next result.
- A gap with `earlier` repeats an earlier gap or reopens its answer after
  the plan change. Ask it again as a new gap, giving the earlier answer
  from the previous result, and record the new answer on the new gap.
- Other gaps are new.
- `closed` keeps earlier gaps with their closure reasons. Ask nothing
  about them. They carry forward as history and need no disposition on
  later checks; a closed concern that matters again is raised as a new gap.

Every `reopened` disposition must link to exactly one new gap through
`earlier`. Ember assigns new ids from the earlier result's `next_id`, so
ids are never reused in a chain. Missing or unknown dispositions and
invalid links leave the check incomplete (exit 81); finish that check
before using its result for another repeat.
