---
tags: [ember]
max_turns: 20
timeout_seconds: 600
allowed_tools: [Read, Glob, Grep, Skill, Bash]
---

/ember Is my panel still running? First list panels with `emb panel --status --json`, then inspect the panel that list shows with `emb panel --status <its id> --json` twice. Report each member's progress and whether its elapsed time and screen activity changed between the two inspections. Use the existing records; start no workers and change no runs.
