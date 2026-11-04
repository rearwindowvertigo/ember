---
tags: [ember]
max_turns: 30
timeout_seconds: 900
allowed_tools: [Read, Glob, Grep, Skill, Write, Edit, Bash]
---

/ember Prepare an Ember flow plan for these three independent changes to
order-tools. Use Codex gpt-6.1-sol at high effort as writer and Codex
gpt-6.1-sol-mini at medium effort as reviewer. Review the task boundaries
so independent implementations can run together, and keep all requested
reference updates. Save the plan and run `emb flow --dry-run --json`.
Stop after reporting its waves and any warnings; do not start workers or
run a gap check yet.

Here is the draft plan:

- `totals`: add `order_count(rows)` in `tools/totals.py`, returning the sum
  of `qty`, including zero for no rows. Add its tests and describe it in
  the Totals section of `docs/reference.md`.
  Files: `tools/totals.py`, `tests/test_totals.py`, `docs/reference.md`.
- `items`: add `item_names(rows)` in `tools/items.py`, returning sorted
  unique `item` names, including an empty list for no rows. Add its tests
  and describe it in the Items section of `docs/reference.md`.
  Files: `tools/items.py`, `tests/test_items.py`, `docs/reference.md`.
- `summary`: add `revenue(rows)` in `tools/summary.py`, returning the sum
  of `qty * price` as a Decimal, including Decimal zero for no rows.
  Add its tests and describe it in the Summary section of
  `docs/reference.md`.
  Files: `tools/summary.py`, `tests/test_summary.py`, `docs/reference.md`.

Each module's tests exercise only that module. None reads the reference
file or imports the other two modules. Use the ids above for the
implementation tasks. The repository check is
`python3 -m unittest discover -s tests`.
