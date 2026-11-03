# Gap check

Follow this before every flow and every Claude Workflow of two or more
tasks. Single runs and panels do not need it. The owner's Team settings
decide whether a plan is checked (Run the gap check: off, always, from a
task count, or ask) and which gaps reach the owner (Gaps Claude asks you
about). Ember applies both; do not decide them yourself.

Write the plan in the session scratchpad with the cases it must handle and
those it accepts: a nonblank `failure_model` string in a flow plan, or a
"Failure model" section in a Workflow plan. Keep it to the request: no
wording, docstrings or tests the owner did not ask for. A blank plan is
refused; a one-step plan is checked as one step.

## Run and read

Run as a background Bash command, unpiped, and take its exit code:

```bash
emb gapcheck --plan <file> --dir <repo> --claim <id> --json
```

Use the run's existing claim. `emb gapcheck` uses the owner's Gap check
reviewer row (`gapcheck.reviewer`, which follows `single.reviewer` while
`same`). When the owner named the reviewer, or that row is not `set`, pass
`--engine <e> --model <m> --effort <x>` with the resolved effort when there
is one. If no reviewer was named and
the row cannot answer, ask the owner using the skill's worker question.
Never change settings to make the check run.

For a flow, read `gapcheck_status` in the `emb flow --dry-run` JSON:

- `skipped`: `gapcheck` is null. Run nothing and ask nothing; start the flow
  from the plan, and say in your report that it ran without a gap check and
  why (`gapcheck_reason`).
- `ask`: put one question to the owner, with `gapcheck_reason`: run the gap
  check, or start without one. On run, use the `gapcheck` command, which
  carries `--confirmed`; on start without, go on as for `skipped`.
- `run`: run the exact `gapcheck` command (also printed after `gap check:`).
  Add `--claim <id>` when absent.

For a Claude Workflow, run `emb gapcheck` itself. Its JSON `status` is
`skipped`, `ask` or `run` with the same meaning; `skipped` and `ask` start no
reviewer and print `reason`. After the owner says to run an `ask` check,
run the same command with `--confirmed`.

Read the JSON or text before starting work. Check `parse_error`,
`uncovered_steps` and `unknown_steps`: any error or missing or unknown
step leaves the check incomplete. Empty gap lists and zero counts are
valid only with coverage of every expected step. Exit 81: follow `next:`
to obtain a complete reply. On a `WARNING:` about a missing failure model,
add it and check again. Read every warning and follow `next:` within the
skill's rules; bring `owner:` lines to the owner.

## Claude subagent reviewer

Exit 64 naming the `--brief`/`--reply` route means the reviewer row runs
as a Claude subagent:

1. Print the brief:
   `emb gapcheck --plan <file> --dir <repo> --brief > <scratch>/gap-brief.md`.
2. Give it to a read-only general-purpose subagent without isolation.
   Follow `references/claude-agents.md` for its model and tools.
3. Save its final message to a scratch file, then parse it:
   `emb gapcheck --plan <file> --reply <reply-file> --json`.

These two commands start no run and use no claim. Check the parsed result
for coverage and errors as above.

## Owner answers

Each gap carries `ask_owner`, from the owner's Gaps Claude asks you about.
Take the default of every gap whose `ask_owner` is false: write it into the
failure model, ask nothing, and list each one in your report. For the rest,
ask each `high` gap as its own question, in task order, waiting for its
answer before the next question. Give the gap, its scenario and its
proposed default as the recommended answer. Then offer the remaining
`medium` and `low` gaps together as one question whose recommended answer
accepts their defaults into the failure model. An empty gap list needs no
question.

Before putting a `high` gap to the owner, check its factual premise
against a live source, such as a log, a rollout file, a record file or the
code it names. When the premise cannot be checked, the question to the
owner says so.

With the owner away and no answer possible, work waits. Do not take a
default for a `high` gap yourself. Write the answers into the failure
model, context or task text before `--start` or the Workflow. If answers
materially change task scope, files, dependencies or ordering, repeat the
flow dry run and gap check (the gap check alone for a Workflow). Recording
answers without such changes needs no repeat. The reviewer finds gaps;
it decides nothing.
