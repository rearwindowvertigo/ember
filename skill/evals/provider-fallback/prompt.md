---
tags: [ember]
max_turns: 60
timeout_seconds: 1800
allowed_tools: [Read, Glob, Grep, Skill, Write, Edit, Bash, Agent]
---

Add a `--since YYYY-MM-DD` option to tools/report.py that leaves out orders dated before that day; an order dated that day counts. A malformed day is a usage error like the tool's others: a message on stderr, nothing on stdout, exit 64. Add tests for a day inside the data, a day after every order, and a malformed day. Change only tools/report.py and tests/test_report.py. Have a worker do it.
