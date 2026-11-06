# Flows

Ember runs each task's editing writer, read-only reviewers and fix rounds.
Each reviewer reads its own detached worktree at the writer's committed
`HEAD`; it does not read uncommitted edits in the writer's worktree
(`references/runs.md` says what closing does to it).
Accepted tasks squash into `ember/flow/<id>`, one commit per task under its
plan `subject`, with the accepted review's recorded verdict in
`Ember-Verdict` and the worker tip in `Ember-Task-Tip`. Recovery and cleanup
recognize that tip through the trailer. Dependencies become ready
after merges into that flow branch. Claude reviews the whole result before
merging it into the session branch. Keep one claim for the flow through
its gap check, owner answers, verification and merge.

Complete the skill-version check at the claim (SKILL.md step 2) before
choosing workers or running the gap check.

## Workers and plan

Use the owner's named workers as `--writer W` and `--reviewer W` on both
the dry run and start, where `W` is `<engine>:<model>[@<effort>]`.
Otherwise resolution uses the plan's `writer` and `reviewer`, then the
`flow.writer` and `flow.reviewer` rows (Team's Flow writer and Flow
reviewer, which follow `single.writer` and `single.reviewer` while `same`). Normally leave workers out of
the plan. Team rows must be `set`; a Claude subagent row exits 64. Ask the
owner for a CLI worker when the row cannot answer and none was named;
pass the answer explicitly. After the latest dry run, copy its `writer` and
`reviewer` announce lines from the text output to the owner, and use those
same names in reports and pull request text.

Write a JSON plan in the session scratchpad:

- `dir`: the absolute checkout path; `base` only when other than `HEAD`.
- `trailers`: the harness attribution trailer in a list.
- `rounds`: leave it out; the flow then uses the owner's Max review
  rounds row. Set it only for a flow that needs a different count, and say
  why before the dry run. `parallel`: leave it out; the flow then uses the
owner's Max tasks at once row (`flow.parallel`). The stall time comes from
Stall after (`flow.stall`) unless you pass `--stall`.
- `check`: an argv list for the repository's full check. It runs on the
  lead's machine in the flow worktree, outside any worker sandbox, with
  the runner's environment. Its timeout is `check_timeout`, an integer
  number of seconds, at least 1, default `1800`. Tests that need a local
  service, such as a database, can go in this check.
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
| `subject` | Required nonblank one-line string, at most 72 characters | `"feat: add order totals"` |
| `task` | Required nonblank string describing the work | `"Implement and test order totals."` |
| `files` | Required non-empty list of unique exact relative POSIX file paths, with no trailing `/` | `["src/totals.py", "tests/test_totals.py"]` |
| `after` | List of other task ids as strings; default `[]`; every id exists, no self dependency or cycle | `["schema"]` |
| `review` | String giving the reviewer's focus; default `""` | `"Check empty orders and rounding."` |

Write `subject` in the repository's commit convention, read from
`AGENTS.md`, `CONTRIBUTING` or `git log`, with `!` for a breaking
Conventional Commit. It is the task's squash subject and the writer
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
as SKILL.md step 6 says.

Ember creates the flow worktree at `worktrees/<id>` in its data folder,
`$XDG_DATA_HOME/ember` (default `~/.local/share/ember`), outside every
repository. The flow record and branch use the same resolved path; the
record also keeps the main checkout. Use the reported path for verification
and cleanup, including for flows started under the old layout. A worktree
folder inside any Git working tree or the source repository's git folder
exits 69, naming the folder and the setting that placed it there.

## Lead stops and owner decisions

A `lead_stop` returns exit 0; it is not a completed flow. For every stop,
send the owner one message naming the task, status, reason, open `lead`
and `must` findings, the tasks it blocks (or none), and every fact its
`report:` line names, such as whether a runner is live. End the message with
one direct question ending in a question mark. Use the report's available
choices, not an assumed list:

| Answer after `emb flow --lead <id> <task>` | Does |
|---|---|
| `--send <file>` | Send a correction to the existing writer, then review it. |
| `--accept` | Accept the task branch's current tip without another review. |
| `--accept --commit <commit>` | Accept the specified reviewed commit. |
| `--review [--reviewer W]` | Collect a running turn, then start fresh reviews and a fix budget. |
| `--worker W` | Keep the old branch and start a new writer from the flow tip. |
| `--drop` | Keep the branch and drop the task; its dependants stay blocked. Also accepted for a pending or blocked task. |

A merge conflict stops the task as `conflict`. Its `next:` line names the
hand merge of the approved tip into the flow branch. After that merge, the
flow records the task as merged when its runner next runs: a resume, or
any queued answer, including `--accept` on that task. A task whose work is
already in the flow branch needs no `--drop`.

Do not settle a flow stop yourself, edit to choose its answer, or merge a
task branch into the session branch. Call `emb flow --lead` only after the
owner's answer. A `--send` file carries that decision, not your own choice.

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
task states. A cancelled watch exits 82; `--lead` requires a resume first.
Exit 75 from `--cancel` means the flow had already ended or the 60-second
deadline from writing `cancel.json` passed with the request still pending;
follow `next: emb flow --status <id>`.

## Provider errors

Each task's run retries `capacity` as `references/runs.md` (Provider errors)
says; other tasks keep running. If retries are exhausted or disabled, exit 73
stops that task without halting new runs. A `usage_limit` or `outage` exit 73,
or a provider error without a recorded kind, halts new runs with
`provider error on <engine>; new runs halted`; active jobs finish and
queued writers stay pending. Resume clears the halt, but the failed task
still needs a lead answer. Report the kind. After a capacity error, name no
other worker as an option; the capacity `report:` line gives the recommendation.

## Denied paths

Each task's status and watch report includes `denied_paths` with `path`,
`reason`, `allow_path`, `run` and `turn`. Flow status also gives their union
with a `task` id on each entry. The runner adds new denials to its
`denied_paths` events with the task and worker role. Empty lists mean no
recorded denials. Report each path with its task, run, turn and reason,
even when the task succeeds. Startup-file denials and older records are
handled as `references/runs.md` (Denied paths) says. Report printed owner
actions and hidden-path restrictions as `references/runs.md` (Denied paths)
describes.

## Verify, merge and report

Before accepting a flow, fetch the remote default branch and compare it
with the flow's recorded base. If the default branch has moved, merge it
into the flow branch and run the plan `check` again in the flow worktree.

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

Report `flow <id> · <writer> / <reviewer> · exit <n> · <merged> of
<tasks> merged · ember/flow/<id>`, one line per task with id, status,
rounds and reason, then the verdict given as the merge's note.
Include the verified tip and cleanup keeps. Finish the claim as SKILL.md
step 9 says, after the merge or the owner's decision on unmerged work, with
the claim id, not the flow id.
