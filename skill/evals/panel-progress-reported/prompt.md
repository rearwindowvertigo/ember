---
tags: [ember]
max_turns: 20
timeout_seconds: 600
allowed_tools: [Read, Glob, Grep, Skill, Bash]
---

/ember Is my panel still running? First list panels with `emb panel --status --json`, then inspect panel-20261102-120000-aaaaa0 with `emb panel --status panel-20261102-120000-aaaaa0 --json` twice. Report each member's progress and whether its elapsed time and screen activity changed between the two inspections. Use the existing records; start no workers and change no runs.
