---
type: llm
focus: trace
weight: 1
---

The gap check is a background Bash command using the dry run's printed command, with `--plan`, `--dir`, `--json` and `--claim <claimed id>`, saving its JSON result. The saved Gap check reviewer row resolves to Codex gpt-6.1-sol-mini at medium effort; no worker question is asked. The repeat command also carries `--previous <last result>`.
