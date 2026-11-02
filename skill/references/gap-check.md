# Gap check

Run this check before every flow and every Claude Workflow of two or more
tasks. Single runs and panels do not need it.

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

Use the run's existing claim. When the owner named the reviewer, or
`single.reviewer` is not `set`, pass `--engine <e> --model <m> --effort <x>`
with the resolved effort when there is one. If no reviewer was named and
the row cannot answer, ask the owner using the skill's worker question.
Never change settings to make the check run.

For a flow, run the exact command printed after `gap check:` by
`emb flow --dry-run`, or in its JSON `gapcheck` field. Add `--claim <id>`
when absent. That command uses the flow's resolved reviewer.

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

Ask each `high` gap as its own question, in task order, waiting for its
answer before the next question. Give the gap, its scenario and its
proposed default as the recommended answer. Then offer all `medium` and
`low` gaps together as one question whose recommended answer accepts their
defaults into the failure model. An empty gap list needs no question.

With the owner away and no answer possible, work waits. Do not take a
default for a `high` gap yourself. Write the answers into the failure
model, context or task text before `--start` or the Workflow. If answers
materially change task scope, files, dependencies or ordering, repeat the
flow dry run and gap check (the gap check alone for a Workflow). Recording
answers without such changes needs no repeat. The reviewer finds gaps;
it decides nothing.
