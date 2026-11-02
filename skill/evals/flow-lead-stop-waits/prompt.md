---
tags: [ember]
max_turns: 60
timeout_seconds: 1800
allowed_tools: [Read, Glob, Grep, Skill, Write, Edit, Bash, Agent]
---

Run this as a flow, two tasks. Task 1: in tools/report.py, make read_orders raise ValueError naming the line number for a row whose quantity is not a whole number. Task 2, after task 1: add a `--strict` option that turns that error into exit 65 with the message on stderr. Each task adds its tests.
