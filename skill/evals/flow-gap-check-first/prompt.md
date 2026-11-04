---
tags: [ember]
max_turns: 60
timeout_seconds: 1800
allowed_tools: [Read, Glob, Grep, Skill, Write, Edit, Bash, Agent]
---

/ember Two tasks for tools/report.py, with the task ids `item` and `json`. First, an `--item NAME` option that totals only the rows of that item. Second, after the first, a `--json` option that prints the totals as one JSON object. Each task adds its tests. Change only tools/report.py and tests/test_report.py.

After recording the gap answers, run another dry run and gap check with the saved result before starting.
