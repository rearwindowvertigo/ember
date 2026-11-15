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
plan, or export it to a file in the session scratchpad and add the file to
the flow plan's `read` list or pass it with `--read` (for a GitHub issue,
`gh issue view <n> --json number,title,state,body,comments --jq '"# #\(.number) \(.title) (\(.state))\n\n\(.body)\n\n## Comments\n\([.comments[].body]|join("\n\n---\n\n"))"' > <scratch>/issue-<n>.md`).
`emb gapcheck --plan` on a flow plan grants and lists the plan's own `read`
paths. `--read` adds to them. A path given both ways is used once. A flow
plan that fails flow validation still supplies its `read` paths.
`emb gapcheck` and `emb flow --dry-run` warn about each citation no read file
supplies; treat that warning as a missing source. A read file that is empty
or holds only whitespace is refused with exit 64, for `emb gapcheck --read`
and for the flow plan's `read` paths; folders are not checked.

## Decide first

Settle whether the plan gets a check before choosing a reviewer or asking
any worker question:

- Flow: read `gapcheck_status` in the `emb flow --dry-run` JSON.
- Claude Workflow: read `gapcheck.when` with `emb choices --json --id <id>`
  for the run's claim, without `--brief` (a brief reply leaves it out),
  and count the plan's steps (its tasks or numbered steps). `off`, or a
  number above the step count, is `skipped`; `ask` is `ask`; anything else
  is `run`.

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

The repository's `worktree.prepare` command runs in the reviewer's worktree
before its session starts, as `references/runs.md` describes. On preparation
failure, exit 79 names the log and the `next:` line repeats the original
`emb gapcheck` invocation. Inspect the log before retrying; no reviewer
started. The `--brief` and `--reply` routes create no worktree and run no
preparation.

Use the run's existing claim. `emb gapcheck` uses the owner's Gap check
reviewer row (`gapcheck.reviewer`, which follows `single.reviewer` while
`same`). When the owner named the reviewer, or that row is not `set`, pass
`--engine <e> --model <m> --effort <x>` with the resolved effort when there
is one. If no reviewer was named and
the row cannot answer, ask the owner using the skill's worker question.

For a flow, run the exact `gapcheck` command from the dry run (also printed
after `gap check:`; it already carries `--json` and `--confirmed` after an
`ask`). The printed command does not repeat the plan's `read` paths as
`--read` options, in text or JSON. Add `--claim <id>` when absent and the
redirect above. For a Claude Workflow, run the command above.
Should `emb gapcheck` itself report `status` `skipped` or `ask`, the
settings changed since you read them: treat it as the decision above.

Read the JSON or text before starting work. Check `parse_error`,
`uncovered_steps` and `unknown_steps`: any error or missing or unknown
step leaves the check incomplete. With `--previous`, a missing or unknown
earlier gap disposition also leaves it incomplete. Empty gap lists and
zero counts are valid only with coverage of every expected step. Exit 81: follow `next:`
to obtain a complete reply. On a `WARNING:` about a missing failure model,
add it and check again. Follow printed lines as SKILL.md step 6 says. For a CLI reviewer's
denied paths, inspect its run status and report them as `references/sandbox-and-exits.md`
(Denied paths) says.

Check for tasks whose `files` overlap only on a shared file, even when
they edit different sections, and raise that overlap as a gap when the
shared edits can be separated from the tasks' own checks. The default
answer follows the shared-file rule in `references/flows.md` (Workers and
plan).

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
   `emb gapcheck --plan <file> --reply <reply-file> --brief-file <scratch>/gap-brief.md [--previous <result>] --json > <scratch>/gapcheck-<plan stem>-<n>.json`.

These two commands start no run and use no claim. Check the parsed result
for coverage and errors as above.

Keep the printed brief: it carries the hash of the plan the reviewer saw.
Every `--reply` needs `--brief-file`, including panel replies. A missing or
unreadable brief, missing hash or changed plan exits 64 with a `next:` line
to print a new brief. Review that new brief before importing a new reply.
For CLI run recovery, use the run's saved brief named by the `next:` line.

## Owner answers

Each gap in `steps` carries an `id` and `ask_owner`, from the owner's Gaps
Claude asks you about. Each gap's level is the `harm` field, with values
`high`, `medium` and `low`. The result has no `severity` field.

Before asking about any gap or recording its answer, check its factual
premise against the code, record or file that the gap or its default
names.
When the premise cannot be checked, say so in the owner question or report.
When the premise does not hold, record a corrected default yourself for
`ask_owner: false` gaps and `medium` or `low` gaps, including defaults the
owner accepted together, and list the correction in your report.
For a `high` gap with `ask_owner: true`, take the corrected answer back to
the owner before recording it.

Take the checked default of every gap whose `ask_owner` is false: write it
into the failure model, ask nothing, and list each one in your report.
For the remaining `high` gaps, you may group several into one question
only after checking that they share one cause.
Name every gap id and the shared cause in a grouped question.
Ask it at the position of its earliest gap in task order.
Keep gaps without a shared cause as separate questions in task order.
Wait for each answer before asking the next question.
Follow SKILL.md step 3 for owner questions, and include each gap's id and
scenario.
Then offer the remaining `medium` and `low` gaps together as one question
whose recommended answer accepts their checked defaults into the failure
model. An empty gap list needs no question.

Record an owner answer or a corrected default with:

```bash
emb gapcheck --answer <scratch>/gapcheck-<plan stem>-<n>.json --id <gap id> --text '<answer>'
```

This form also overwrites an existing answer on an active or settled gap.
For a grouped question with distinct answers, run it for every gap id.

After checking each default's factual premise as above, record several
unchanged defaults in one call:

```bash
emb gapcheck --answer <scratch>/gapcheck-<plan stem>-<n>.json --take-defaults g1,g3 --take-defaults g5
```

List every id explicitly; there is no `all`. Use only defaults you may
take under the owner-answer rules above. This writes each gap's own
`Default taken: <default>` answer. It cannot be combined with `--id` or
`--text`. Under the result lock, Ember validates the whole batch before
writing: an unknown, duplicate, settled or already answered id refuses
with exit 64 and leaves the result unchanged.

Ember validates and updates the result. Do not edit the JSON by hand.
The answer is stored once, on its gap in `steps` (or `settled` for an
earlier settled gap). `ranked` refers to active gaps by id, step and harm.

With the owner away and no answer possible, work waits. Do not take a
default for a `high` gap with `ask_owner: true` yourself. Write the answers
into the failure model, context or task text before `--start` or the
Workflow. Record every answer of the current result first and update the
plan once with all of them. For a flow, run
`emb flow --plan <file> --dry-run --previous <last result>` and follow its
comparison lines (or `gapcheck_comparison` in JSON):

- `repeat needed` names each structural change: tasks added or removed,
  changed `files` or `after`, or changed waves. Run a whole-plan check.
- `lead judgement` lists changed task text, context or failure model and
  the affected task ids. Judge whether these change scope or behaviour
  enough to need a whole-plan check. Changes only from writing recorded
  answers into that text are the expected result of the step above. They
  do not by themselves call for a repeat check. For example, writing a
  recorded answer's timeout into the task text alone needs no repeat.
  A `repeat needed` line still calls for a whole-plan check when the change
  follows from a recorded answer. For example, adding a file the answer
  names to a task's `files` needs a whole-plan check.
- An incomplete earlier check or a result without a saved flow baseline
  reports `repeat needed`. For an incomplete check, complete it or run a
  whole-plan check without passing that result as `--previous`.

The comparison is independent of `gapcheck.when`: a repeat recommendation
does not grant permission. Keep the `skipped` and `ask` rules above.
Ember never reports that no repeat is needed. Files and `after` compare
by membership; task text, context and failure model compare exactly.
JSON `repeat_needed` is `true` for a recommendation and `null` otherwise;
`reasons`, `judge` and `recovery` carry the same findings as the text.
Results save a versioned checked plan; older results still load.
For a Workflow, judge material changes to scope, files, dependencies or
ordering yourself and repeat with the same `--previous` when needed.
The reviewer finds gaps; it decides nothing.

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
