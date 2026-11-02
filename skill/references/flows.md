# Flows

Ember runs each task's editing writer, read-only reviewers and fix rounds.
Accepted tasks merge into `ember/flow/<id>`. Dependencies become ready
after merges into that flow branch. Claude reviews the whole result before
merging it into the session branch. Keep one claim for the flow through
its gap check, owner answers, verification and merge.

## Workers and plan

Use the owner's named workers as `--writer W` and `--reviewer W` on both
the dry run and start, where `W` is `<engine>:<model>[@<effort>]`.
Otherwise resolution uses the plan's `writer` and `reviewer`, then the
`single.writer` and `single.reviewer` rows. Normally leave workers out of
the plan. Menu rows must be `set`; a Claude subagent row exits 64. Ask the
owner for a CLI worker when the row cannot answer and none was named;
pass the answer explicitly. Never set a row yourself. Say both resolved
workers and their efforts in one line before the dry run.

Write a JSON plan in the session scratchpad:

- `dir`: the absolute checkout path; `base` only when other than `HEAD`.
- `trailers`: the harness attribution trailer in a list.
- `rounds`: default 2; `parallel`: default 4.
- `check`: an argv list for the repository's full check.
- `failure_model`: the cases that must work and those accepted.
- `context`: only settled decisions and the rules tasks need.
- `tasks`: each has `id`, `subject` (the commit subject, at most 72
  characters), `task`, `files`, `after` and `review`.

List exact files, including shared tests that a change would break.
Declared overlap serializes tasks. Put a shared interface first and make
its users depend on it through `after`. Each task completes its own change;
none leaves work for another or does lead or owner work. A one-task plan
is valid; blank plans and empty `tasks` are refused with exit 64.

## Dry run, gap check and start

```bash
emb flow --plan <file> --dry-run --claim <id> [--writer W] [--reviewer W] --json
```

Read `waves`, `warnings` and `gapcheck`. Clear or explain every warning.
Run the printed gap check command as `references/gap-check.md` says, and
write the owner's answers into the plan before starting. A material change
to task scope or ordering requires another dry run and gap check.

Run these as background Bash commands, unpiped, taking each exit code:

```bash
emb flow --plan <file> --start --claim <id> [--writer W] [--reviewer W]
emb flow --watch <id> --json
```

Start with the same worker options used for the dry run. Keep the flow id,
branch and worktree from the start output before watching. Do not poll
`--status` while a watch runs. Read standard error even at exit 0, report
warnings and follow `next:` within these rules; bring `owner:` to the owner.

## Lead stops and owner decisions

A `lead_stop` returns exit 0; it is not a completed flow. For every stop,
send the owner one message naming the task, status, reason, open `lead`
and `must` findings, and the tasks it blocks (or none). List every choice
in its `choices`, each with its flags (an optional flag such as
`--accept --commit <commit>` is its own choice), what each does, and your
recommendation among them. End the message with one direct question
ending in a question mark. Use the report's available choices, not an
assumed list:

| Answer after `emb flow --lead <id> <task>` | Does |
|---|---|
| `--send <file>` | Send a correction to the existing writer, then review it. |
| `--accept` | Accept the task branch's current tip without another review. |
| `--accept --commit <commit>` | Accept the specified reviewed commit. |
| `--review [--reviewer W]` | Collect a running turn, then start fresh reviews and a fix budget. |
| `--worker W` | Keep the old branch and start a new writer from the flow tip. |
| `--drop` | Keep the branch and drop the task; its dependants stay blocked. |

Do not settle a flow stop yourself, edit to choose its answer, or merge a
task branch into the session branch. Call `emb flow --lead` only after the
owner's answer. A `--send` file carries that decision, not your own choice.
For a refused answer, report the refusal and the choices now available.
Watch again only when `next:` says a runner is live; otherwise wait for
the owner's answer, which starts the runner when queued. After the answer,
follow its printed watch command in the background.

If the stop says `git commit was refused` (exit 77), offer `--send <file>`
first. It retries the commit; if the hook refuses again, the writer receives
the hook's refreshed output ahead of the correction. The output is capped
at its last 200 lines and last 64 KiB, with a note when cut. The following
turn commits the writer's files from both turns with its normal subject.
This send counts as a fix round in the existing budget, even when all
rounds are used. Alternatives are to fix the files named by the hook in the
task's worktree, run `emb run --recover <writer run>`, then answer with
`--review`; or use `--worker W`; or `--drop`. Perform the chosen repair only
after the owner answers, and run recovery in the background. Exit 77 also
covers `git add failed`: repair that failure and use `--recover`; the hook
output send applies only when `git commit was refused`.

## Exits

Exit 82 means failed or ended with tasks unmerged. Report every task's
status and reason and leave unmerged work to the owner. Do not merge the
flow branch until the owner decides what to do with it.

Exit 72 means the runner is gone and work may still be running. Tell the
owner, then follow `next: emb flow --resume <id>`. Other exits follow the
`emb flow` exit table in the command reference and their `next:` or
`owner:` lines; a refusal is not permission to force an action.

## Verify, merge and report

At `end`, exit 0 with every task merged, verify in the flow worktree:

```bash
python3 scripts/verify_branch.py --repo <repo> --base <session branch> --branch ember/flow/<id> --worktree <flow worktree>
```

Omit `--expect-files`. Run your own checks and apply the skill's exact
review bar to the whole flow diff. Inspect each task's `outside_files`,
every retained `should` and `lead` finding, and every retained branch.
Give one verdict for the flow branch, written as reply text such as
`Verdict: Keep.` in the same message as the `emb merge` call and before
it; a verdict only in thinking does not count. Run that call on its
own, with no other command chained before or after it.

If verification rejects the result, keep the flow branch and claim open,
name the failed check, and make a separately reviewed correction on the
flow branch before repeating verification. Use an editing run based on
the flow worktree, review its result and merge that correction into the
flow branch through `emb merge`; do not answer a completed flow with
`--lead` or merge the rejected result into the session branch. When that
correction fails too, the verdict is `Keep, with <fix>` naming what is
missing, or Discard; write it right before the merge, and leave a
Discard unmerged. Merge a `Keep, with <fix>` at once, then make and check
the fix yourself on the session branch; it needs no question to the owner.

After `Verdict: Keep.` or `Verdict: Keep, with <fix>.`, merge under the skill's merge and cleanup rules:

```bash
emb merge ember/flow/<id> --into <session branch> --verdict keep --dir <dir>
```

Report `flow <id> · <writer> / <reviewer> · exit <n> · <merged> of
<tasks> merged · ember/flow/<id>`, one line per task with id, status,
rounds and reason, then the verdict already written before the merge.
Include the verified tip and cleanup keeps. Finish with `emb choices --finish <id>` after the merge or the
owner's decision on unmerged work, using the claim id, not the flow id.
