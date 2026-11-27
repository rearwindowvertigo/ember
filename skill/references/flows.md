# Flows

Ember runs each task's editing writer, read-only reviewers and fix rounds.
Each reviewer reads its own detached worktree at the writer's committed
`HEAD`; it does not read uncommitted edits in the writer's worktree
(`references/runs.md` says what closing does to it).
Accepted tasks squash into `ember/flow/<id>`, one commit per task under its
plan `subject`, with the accepted review's recorded verdict in
`Ember-Verdict` and the worker tip in `Ember-Task-Tip`. Recovery and cleanup
recognize that tip through the trailer. Dependencies become ready
after merges into that flow branch. The lead reviews the whole result before
merging it into the session branch. Keep one claim for the flow through
its gap check, owner answers, verification and merge.

Complete the skill-version check at the claim (SKILL.md step 2) before
choosing workers or running the gap check.

## Workers and plan

Use the owner's named workers as `--writer W` and `--reviewer W` on both
the dry run and start, where `W` is `<engine>:<model>[@<effort>]`.
Otherwise resolution uses the plan's `writer` and `reviewer`, then the
`flow.writer` and `flow.reviewer` rows (Team's Flow writer and Flow
reviewer, which follow `single.writer` and `single.reviewer` while `same`).
Normally leave workers out of the plan. Read the claimed choices without
`--brief` for the flow rows. Use a fixed CLI row as set. For an inherited
`ask` or `auto`, follow `references/worker-selection.md` separately for the
plan's writer and reviewer and pass the chosen model and effort for each
unresolved role. A fixed Claude subagent row cannot run in a flow; ask
before substituting a CLI worker.

When you pass `--reviewer` to resolve the flow's Team policy, add
`--gapcheck-use-team` to each dry run, including comparisons with
`--previous`. This keeps the gap check's reviewer policy separate. A
reviewer already named for the flow by the owner or in the plan also
applies to the gap check. If the owner names a different gap check
reviewer, use `--gapcheck-use-team` and pass that reviewer to `emb gapcheck`.

If gap answers materially change the plan's scope or checks, check that
the chosen pair still fits before the final dry run.
After the latest dry run, post its `writer` and `reviewer` lines to the
owner before starting. Append a short task-specific reason to each
automatically chosen worker's line. Name the benchmark and cost unit
when benchmark cost helped select the worker. Use
those same names in reports and pull request text.

Write a JSON plan in the session scratchpad:

- `dir`: the absolute checkout path; `base` only when other than `HEAD`.
- `trailers`: any attribution trailer supplied by the harness or required by
  the repository, in a list. Omit the field when there is none.
- `rounds`: leave it out; the flow then uses the owner's Max review
  rounds row (`flow.review_rounds`), which counts reviews per task. A plan's
  `rounds` counts fix rounds, one fewer than reviews. Set it only for a flow
  that needs a different count, and say why before the dry run.
  `parallel`: leave it out; the flow then uses the
owner's Max tasks at once row (`flow.parallel`). The stall time comes from
Stall after (`flow.stall`) unless you pass `--stall`.
- `check`: an argv list for the repository's full check. It runs on the
  lead's machine in the flow worktree, outside any worker sandbox, with
  the environment of the `emb flow` command that starts the runner:
  `--start`, every `--resume`, and a `--lead` answer that restarts it. Set
  variables the check needs in that shell each time. Its timeout is
  `check_timeout`, an integer number of seconds, at least 1, default `1800`.
  Tests that need a local service, such as a database, can go in this check.
  For a flow not meant to repair a failing base, run the plan's exact check
  argv on the base, on this machine, before `--start`. Start only when it
  exits 0. To have Ember do this before starting tasks, use
  `emb flow --plan <file> --start --check-base`. It runs the check in the
  prepared flow worktree at wave 0, before creating any task run. A non-zero
  exit or timeout ends the flow `failed`, with a reason starting
  `the check failed on the base` and the check log tail. The `report:` and
  `next:` lines say to correct the check or the flow branch, then resume.
  An interrupted or cancelled base check stays pending and runs on resume;
  a passed base check does not run again on later resumes. `--check-base`
  without `--start` or a plan check exits 64. A flow meant to repair a
  failing base starts without `--check-base`.
  Leave out a test this machine cannot run through a wrapper script named
  as the check argv. The wrapper lists each excluded file and fails on any
  other failure. Name the exclusions to the owner and in the report.
  The check runs on the merged result after each wave. Writers without a
  resolved `writer_check` are told to run it too. A chain of one-task waves
  runs the wave check once per task; wave count multiplies check time.
- `writer_check`: optional default list of check commands for writers. Each
  command is an argv list with the same rules as `check`. A task can set its
  own list, and an empty list tells the writer to run no writer check
  command. Writer briefs, fix messages and lead corrections omit the
  separate wave check instruction when the current flow check's argv
  matches a writer command. If the plan has a check, a dry run reports
  `<task id>: writer_check runs the plan's check` once per task whose
  resolved writer check contains the plan's check argv, in plan order.
  The warning appears as `WARNING: <text>` in text output and in the JSON
  `warnings` list. Use narrower writer checks when the full check is long
  or several writers would run it at once on one machine. The wave check
  covers the merged result.
- `failure_model`: the cases that must work and those accepted.
- `context`: only settled decisions and the rules tasks need.
- `read`: the files that hold each source the plan cites, such as an
  exported issue (`gh issue view <n> --json number,title,state,body,comments --jq '"# #\(.number) \(.title) (\(.state))\n\n\(.body)\n\n## Comments\n\([.comments[].body]|join("\n\n---\n\n"))"' > <scratch>/issue-<n>.md`);
  workers cannot open an issue tracker. Writers and reviewers see them
  listed under "Sources to read". Read file rules are in
  `references/gap-check.md`.
- `tasks`: a non-empty list of task objects with these fields. See the
  task-field table under Plan in `docs/reference.md` for the full rules.

| Task field | Type and rule | Example |
|---|---|---|
| `id` | Required unique string matching `[a-z0-9][a-z0-9-]{0,39}` | `"totals"` |
| `subject` | Required nonblank one-line string, at most 72 characters | `"Add order totals"` |
| `task` | Required nonblank string describing the work | `"Implement and test order totals."` |
| `files` | Required non-empty list of unique exact relative POSIX file paths, with no trailing `/` | `["src/totals.py", "tests/test_totals.py"]` |
| `after` | List of other task ids as strings; default `[]`; every id exists, no self dependency or cycle | `["schema"]` |
| `review` | String giving the reviewer's focus; default `""` | `"Check empty orders and rounding."` |
| `writer_check` | List of command argv lists for this task, including `[]` for no commands. Omit to inherit the plan's `writer_check`. | `[["pytest", "tests/test_totals.py"]]` |

Write `subject` in the repository's commit convention, with `!` for a
breaking Conventional Commit: the one `AGENTS.md` or `CONTRIBUTING` states;
when neither states one, the form of the session branch's recent commit
subjects, including any prefix or tag they share. Use the subjects from
your starting context if present; run
`git log --oneline -10` in the checkout only when that list is absent, is
for another repository or branch, or shows no single form. It is the
task's squash subject and the writer
brief's first line. Recorded flows keep their resolved subjects.

`files` refuses a leading slash, an empty path component, `.` or `..`
components, or NUL with exit 64. A trailing `/` is an empty component.
A path that is a folder at `base` gets the warning
`<id>: <path> is a folder; list the files the task changes`.

List exact files, including shared tests that a change would break.
Declared overlap serializes tasks, even when edits affect different sections.
When edits to a shared file can be separated from the tasks' own checks,
put them in their own task with `after` on the implementation tasks whose
results it describes or registers, or make them yourself while merging.
This covers reference documents, changelogs, indexes and registration
points such as command-line parsers. The implementation tasks do not list
that shared file in `files`; their briefs name who makes those edits.
Keep a shared-file edit with its implementation when the task's own checks
need it: a parser option exercised by that task's tests stays with the
task, and tasks touching that parser serialize.

For a task whose product reads input the task does not control, such as a
check, parser or validator over the repository's code or data, state the
input contract in `task` before the gap check: the inputs and planned
changes the product must handle, and what it does with input it cannot
handle. List each miss you accept under the accepted cases in
`failure_model`. In `review`, name one case that must pass and one
violation the product must catch.

Put a shared interface first and make its users depend on it through
`after`. Each implementation task completes its change and its own checks;
it does not leave required implementation or checks to the shared-file
task. No task does lead or owner work. A one-task plan is valid; blank
plans and empty `tasks` are refused with exit 64.

## Dry run, gap check and start

```bash
emb flow --plan <file> --dry-run --claim <id> [--writer W] [--reviewer W]
emb flow --plan <file> --dry-run --claim <id> [--writer W] [--reviewer W] --json
```

Read `waves`, `warnings`, `gapcheck` and `whole_review` from each flow's
JSON dry run. Announce its `whole_review` value to the owner.
Clear or explain every warning.
Run the printed gap check command as `references/gap-check.md` says, which
also covers recording the owner's answers and repeat checks; write the answers
into the plan before starting. `emb flow --previous` is accepted only with
`--dry-run`, not with `--start`.

Start and watch the flow:

```bash
emb flow --plan <file> --start --claim <id> [--writer W] [--reviewer W]
emb flow --watch <id> --json
```

Start with the same worker options used for the dry run. Keep the flow id,
branch and worktree from the start output before watching. Do not poll
`--status` while a watch runs. Follow the printed `next:` and `owner:` lines
as SKILL.md step 6 says. `--gapcheck-use-team` is a dry-run option.

Start prints the resolved writer and reviewer announce lines on standard
error, as the text dry run does in its output. Both print
`same model: writer and reviewer both resolve to <engine> <model>` when
both roles resolve to one engine and model, even with different efforts.
The JSON dry run carries this fact as `same_model`. Report it to the owner
in one sentence and change nothing.

Ember creates the flow worktree at `worktrees/<id>` in its data folder,
`$XDG_DATA_HOME/ember` (default `~/.local/share/ember`), outside every
repository. The flow record and branch use the same resolved path; the
record also keeps the main checkout. Use the reported path for verification
and cleanup, including for flows started under the old layout. A worktree
folder inside any Git working tree or the source repository's git folder
exits 69, naming the folder and the setting that placed it there.

The new flow worktree starts with tracked files, without installed
dependencies, generated files or ignored local configuration. The owner's
standing `worktree.prepare` command runs there after creation and before
the runner starts workers or checks. It also runs in every new writer and
reviewer worktree before its session starts; `references/runs.md` covers
the setting, execution and failure checks. With no command, Ember performs
no preparation.

A flow-worktree preparation failure at `--start` leaves the flow failed
with exit 79 and its worktree kept. A task-run preparation failure halts
new runs in that flow. Running worker sessions finish; prepared runs
recheck the halt immediately before session creation and do not launch.
Inspect the named log and follow `next: emb flow --resume <id>`. Resume
retries unsuccessful preparation with the current setting, keeps earlier
logs and never repeats successful preparation in an existing worktree.
It creates replacement runs only for children blocked by preparation,
keeps completed writer work, and leaves unrelated lead stops for their
owner answers. An indeterminate preparation state refuses automatic
continuation; inspect its log and worktree.

## Lead stops and owner decisions

A `lead_stop` returns exit 0; it is not a completed flow. For every stop,
send the owner one message naming the task, status, reason, open `lead`
and `must` findings, the tasks it blocks (or none), and every fact its
`report:` line names, such as whether a runner is live. Give the status and
the reason in Ember's printed words, such as `stopped` and `needs the
lead`, and say what they mean. End the message with one direct question
ending in a question mark. Use the report's available choices, not an
assumed list:

| Answer after `emb flow --lead <id> <task>` | Does |
|---|---|
| `--send <file>` | Send a correction to the existing writer, then review it. |
| `--accept [--commit <sha>]` | Accept the task branch's current tip without another review. With `--commit`, accept that reviewed commit instead of the tip. |
| `--review [--reviewer W]` | Collect a running turn, then start fresh reviews and a fix budget. |
| `--worker W` | Keep the old branch and start a new writer from the flow tip. |
| `--drop` | Keep the branch and drop the task; its dependants stay blocked. Also accepted for a pending or blocked task. |
| `--withdraw` | Remove the task's answer that the runner has not applied yet. |

The runner reads answers at every poll. It starts a `--review` answer at
once when a job slot is free and no running task holds the task's files;
every other answer waits until the running jobs of the wave end. Until the
runner applies an answer, a new answer for the same task replaces it and
`--withdraw` removes it; `emb flow --status <id>` lists the queued answers.
A fix that a review started this way decides waits for the next wave.
`emb flow --lead <id> --reviewer W`, with no task, queues a change of the
flow's saved reviewer; the runner applies it before the next review it
starts, and with no live runner it waits for the next runner. Use it only
on the owner's word for a new reviewer.

In the stop message, name each choice you offer by its printed option and
say in words what it does. For
`--accept [--commit <sha>]` say both: plain `--accept` takes the current
tip with no further review, and `--commit <sha>` takes that reviewed
commit instead of the tip. A `conflict` stop and a provider error limit
what you offer, as their paragraphs below say.

A merge conflict stops the task as `conflict`. Its `next:` line names the
hand merge of the approved tip into the flow branch. After that merge, the
flow records the task as merged when its runner next runs: a resume, or
any queued answer, including `--accept` on that task. A task whose work is
already in the flow branch needs no `--drop`.

Do not settle a flow stop yourself, edit to choose its answer, or merge a
task branch into the session branch. Call `emb flow --lead` only after the
owner's answer. A `--send` file carries that decision, not your own choice.
At a stop, a choice you wrote into the plan or its failure model without
the owner's word is yours, not the owner's decision. Say what the plan
says and that you chose it, then ask. Do not call the finding settled,
decided or covered, by your plan or by anything else you chose.

Every sent correction appears in that task's later reviewer briefs, in
order, as a settled decision; files it allows are in scope for the review.
When a reviewer repeats a finding already settled by a sent correction or
an owner answer, name the earlier decision to the owner. Recommend
`--accept` only when every open finding at the stop repeats such a decision;
otherwise recommend among the other available choices.

For a refused answer, report the refusal and the choices now available.
Watch again only when `next:` names a live runner; otherwise wait for
the owner's answer. After the answer, follow its printed watch command.

If the stop says `git commit was refused` (exit 77), offer `--send <file>`
first. It retries the commit; if the hook refuses again, the writer receives
the hook's refreshed output ahead of the correction. The output is capped
at its last 200 lines and last 64 KiB, with a note when cut. The following
turn commits the writer's files from both turns with its normal subject.
This send counts as a fix round in the existing budget, even when all
rounds are used. Alternatives are to fix the files named by the hook in the
task's worktree, run `emb run --recover <writer run>`, then answer with
`--review`; or use `--worker W`; or `--drop`. Perform the chosen repair only
after the owner answers. Exit 77 also
covers `git add failed`: repair that failure and use `--recover`; the hook
output send applies only when `git commit was refused`.

`emb flow --status <id>` names the review budget in the unit that set it;
JSON `review_budget` gives `value`, `unit` and `source`. For example,
`flow.review_rounds` 1 permits one review and no fix round; an unresolved
`must` stops with `unresolved after 1 review (flow.review_rounds 1 allows
no fix round)`. An explicit plan `rounds` names that plan budget in fix
rounds. The source is saved at flow start; older records without it report
source unknown unless their saved plan explicitly names the matching
`rounds`. The budget permits no automatic extra fix round.

## Cancellation

You may cancel a flow for any reason you state. State the reason and run:

```bash
emb flow --cancel <id> --reason "<reason>" --json
```

Report the cancellation to the owner with the reason, every kept branch
(the flow branch and every `task_branches` entry), and every `open_runs`
entry with its task, role and run id. Cancellation stops the flow runner;
writer and reviewer turns keep running. It closes no task run and deletes
no branch or worktree. `emb flow --resume <id>` restarts from the recorded
task states. `--lead` on a cancelled flow requires a resume first.
Watch exit 82 means the flow failed (including a failed check), was cancelled,
or ended with tasks unmerged. The watch report's status, `reason` and action
line tell these cases apart.
Exit 75 from `--cancel` means the flow had already ended or the 60-second
deadline from writing `cancel.json` passed with the request still pending;
follow `next: emb flow --status <id>`.

A plain `emb flow --resume <flow>` reruns the saved check after a failed
check. The `next:` line after a wave check failure assumes the branch is
at fault. When the command is wrong, replace it with:

```bash
emb flow --resume <flow> --check '<JSON argv list>' --reason <text>
```

Replacement requires a `failed` flow with `check_pending`, at any wave
including wave 0, and no running or starting runner. Other states or a
pending cancellation are refused with exit 75. An indeterminate worktree
preparation state also refuses continuation. The argv uses the plan's
`check` validation: a non-empty JSON list of strings, a nonblank command
and no NUL characters. Later arguments may be empty. Invalid argv or a
missing or blank reason exits 64. A refused call changes nothing.
Ember saves the replacement and its history entry atomically in the flow
record, with the time, old argv, new argv and reason. The saved plan stays
unchanged. The runner runs the flow record's check. When the flow tip lacks
the target tip, `emb merge` names that check in its next action. Writer
briefs, fix messages and lead corrections use the current flow check and
the task's saved `writer_check` in their checks section. Replacing `check`
leaves each task's `writer_check` unchanged. If runner launch fails after
the save, the replacement stays and a plain resume uses it. The replacement
runs before any pending wave and must pass. No option accepts a wave without
a passing check.
A flow merged while `failed` keeps that status in its record.

## Provider errors

Each task's run retries `capacity` as `references/runs.md` (Provider errors)
says; other tasks keep running. If retries are exhausted or disabled, exit 73
stops that task without halting new runs. A `usage_limit` or `outage` exit 73,
or a provider error without a recorded kind, halts new runs with
`provider error on <engine>; new runs halted`; active jobs finish and
queued writers stay pending. Resume clears the halt, but the failed task
still needs a lead answer. Report the kind. After a capacity error, name no
other worker as an option; the capacity `report:` line gives the recommendation.

When the owner's fallback list covers a provider error, the flow repeats
the writer or reviewer start on that worker by itself and does not stop.
The task's own worker stays: a later start of that role uses it again.
`emb flow --status <id>` then prints a `fallback:` line under the task, and
the JSON task has `fallbacks`. Tell the owner each such switch with its
task, the model that failed, its cause and the model that ran.

## Denied paths

Each task's status and watch report includes `denied_paths` with `path`,
`reason`, `allow_path`, `run` and `turn`. Flow status also gives their union
with a `task` id on each entry. The runner adds new denials to its
`denied_paths` events with the task and worker role. Empty lists mean no
recorded denials. Report each path with its task, run, turn and reason,
even when the task succeeds. Startup-file denials and older records are
handled as `references/sandbox-and-exits.md` (Denied paths) says. Report printed owner
actions and hidden-path restrictions as `references/sandbox-and-exits.md` (Denied paths)
describes.

## Verify, merge and report

At a `done` flow's end, a `report:` line names each merged task with outside
files and its count. For example:

```text
report: 2 merged tasks changed files outside their declared files: a (1), b (2); inspect each before merging
```

Watch and `emb flow --status <id>` show the line; single-flow status JSON
holds it in `reports`. Inspect each named task's outside files before
merging. Each review's `outside_files` has one entry per outside file:
`file`, `needed` and a one-line `reason`. A missing, repeated or invalid
entry for a listed file makes the review unreadable. Reviewers judge
whether each edit was needed and raise an unneeded edit as `must` or
`should` under the review bar. An allowed outside edit is not a `lead`
finding just for being outside the list. Reviewers also check changes to
behaviour or user-facing wording the task did not ask for, in any file.

A writer's change to a file that another task of its wave declares does
not stop the task. The review brief names each such file with the
declaring task, and the reviewer judges it as an outside edit. The writer
brief forbids such a change unless the task text names the file as the
task's to change. When Git reports a conflict at the
later task's merge, that task stops as `conflict`.

`emb merge` checks a completed flow branch against the local `--into`
target. With a matching flow record whose status is `done`, the flow tip
must contain the target tip (`git merge-base --is-ancestor <target tip>
<flow tip>`). Otherwise it exits 75 and merges nothing. Follow its `next:`
line: merge the target into the flow branch in the recorded flow worktree,
run the flow record's check again there, then run `emb merge` again. The line
prints the saved check argv or says no check is configured. An unreadable
or mismatched flow record also refuses. Run branches, non-completed flows
and flow branches without records have no such target-tip check.

Cut a flow from the target it will merge into. If an earlier flow reaches
that target by squash merge, a later flow cut from the pre-squash branch
can conflict when incorporating the target. Start it from the updated
target after the earlier work lands, or incorporate the updated target
in the flow worktree and rerun the check before merging.

At `end`, exit 0 with every task merged, verify in the flow worktree with the
`verify_branch.py` command in SKILL.md step 7 for a branch with no run record:
`--branch ember/flow/<id>` and `--worktree <flow worktree>`. Run your own
checks and apply the skill's exact review bar to the whole flow diff.

At the flow's end, read the flow record's `whole_review` with
`emb flow --status <id> --json` before running or skipping the whole-flow
review. When that value is true (Team's Review the whole flow), run one
read-only review of the whole flow before the merge: `emb run --read-only`
on the flow's reviewer, with `--dir` naming the flow worktree.
The reviewer reads its own detached
worktree at the flow's committed `HEAD`. Give it a brief that holds the
skill's exact review bar and asks for findings on `git diff <base>...ember/flow/<id>`. Handle its findings as
any review's, and close the run after reading its reply. When
`whole_review` is false, run no such review. Inspect each task's `outside_files`,
every retained `should` and `lead` finding, and every retained branch.
Give one verdict for the flow branch as the `--note` of the `emb merge`
call, such as `--note 'Keep.'`.

If verification rejects the result, keep the flow branch and claim open,
name the failed check, and make a separately reviewed correction on the
flow branch before repeating verification. Use an editing run based on
the flow worktree, review its result and merge that correction into the
flow branch through `emb merge --subject '<commit subject>'`, using the
repository's commit convention; do not answer a completed flow with
`--lead` or merge the rejected result into the session branch. When that
correction fails too, the verdict is `Keep, with <fix>` naming what is
missing, or Discard; give it as the merge's `--note`, and leave a
Discard unmerged. Merge a `Keep, with <fix>` at once, then make and check
the fix yourself on the session branch; it needs no question to the owner.

For a Keep or a Keep, with <fix>, merge under the skill's merge and
cleanup rules, with the `emb merge` command in SKILL.md step 9 for a branch
with no run record: `ember/flow/<id>` and `--dir <dir>`. The completed flow
branch gets a merge commit, preserving its task squash commits; this call does
not require `--subject`.

`emb merge` already cleans up the merged branch and its worktree when the
cleanup checks allow it. An empty `emb cleanup` afterwards is expected;
the empty message names run and flow branches and any claim filter.

Report `flow <id> · <writer> / <reviewer> · exit <n> · <merged> of
<tasks> merged · ember/flow/<id>`, one line per task with id, status,
rounds and reason, then the verdict given as the merge's note.
Include the verified tip and cleanup keeps. Finish the claim as SKILL.md
step 9 says, after the merge or the owner's decision on unmerged work, with
the claim id, not the flow id.
