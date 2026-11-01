# Ember

Ember lets Claude lead AI coding workers on one Linux machine. Claude splits
a task, hands the parts to Codex, OpenCode or Claude workers in isolated git
worktrees, reviews what they return and merges what passes. A desktop app
shows live work, engine status and dependencies, and holds the settings.

Status: in development toward 0.1.0. The design is
`docs/design/0001-first-release.md`.

## Development

uv builds the environment, and `make check` runs the full check:

```bash
uv sync
make check
```
