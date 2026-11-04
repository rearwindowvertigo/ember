#!/usr/bin/env bash
set -eu
mkdir -p .ember-eval tools tests docs
printf '%s\n' 'SESSION_BRANCH="claude/flow-shared-document-task"' > .ember-eval/case.env
: > .ember-eval/real-flow-planner
for module in totals items summary; do
    printf '"""Order %s helpers."""\n' "$module" > "tools/$module.py"
    printf '"""Tests for order %s helpers."""\n' "$module" > "tests/test_$module.py"
done
cat > docs/reference.md <<'DOC'
# Order helpers

## Totals

No totals helper is defined.

## Items

No item-name helper is defined.

## Summary

No revenue helper is defined.
DOC
bash "$(dirname "$0")/../_stubs/install.sh" "$(dirname "$0")"
cat > .ember-eval/check-plan.py <<'PYTHON'
"""Assert the task boundaries and waves in the lead's submitted plan."""

import sys
from pathlib import Path

folder = Path(__file__).resolve().parent
sys.path.insert(0, str(folder / "bin"))
from ember.flows import plan  # noqa: E402

submitted = plan.load(folder / "flow-plan.json")
implementations = {"totals", "items", "summary"}
assert len(submitted.tasks) == 4, "expected three implementations and one document task"
for name in implementations:
    task = submitted.task(name)
    assert set(task.files) == {f"tools/{name}.py", f"tests/test_{name}.py"}, name
    assert not task.after, f"{name} must be independent"
(document,) = [task for task in submitted.tasks if task.id not in implementations]
assert set(document.files) == {"docs/reference.md"}, "the document has one owner"
assert set(document.after) == implementations, "the document follows all implementations"
assert [{task.id for task in wave} for wave in plan.waves(submitted)] == [
    implementations,
    {document.id},
], "expected all implementations in wave one and the document alone in wave two"
print("Plan checked: three implementations in wave one; document in wave two.")
PYTHON
