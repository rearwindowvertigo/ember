---
type: llm
focus: trace
weight: 1
---

Claude claims with `--skill` from the loaded skill text before reading choices, saves a JSON flow plan and invokes
`emb flow --plan <file> --dry-run --json` with the named workers and claim.
The last dry run exits 0. It starts no flow, run, panel or gap check.

Read `.ember-eval/flow-plan.json`, the copy of Claude's submitted plan
saved by the dry-run route. Run `python3 -I .ember-eval/check-plan.py` in
the case's working directory. It must exit 0: it loads that plan through
`ember.flows.plan`, asserts that totals, items and summary own only their
own modules and tests, that one document task owns docs/reference.md and
has after on all three implementations, and that the real planner puts
those three implementations in the first wave and only the document task
in the second. A missing saved plan, failed assertion or a report of the
right waves without that checked plan fails this grader.

The implementation briefs explicitly leave the reference edits to the
shared document task; that task describes all three completed changes.
The implementation tasks retain their own tests and checks. There are no
section-scoped file declarations and no omitted reference updates.
Claude reports both waves. The document-only task produces no
`no test file in its files` warning in the dry run.
