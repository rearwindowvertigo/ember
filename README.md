# Ember

Ember lets Claude lead AI coding workers on one Linux machine. Claude splits
a task, hands the parts to Codex, OpenCode or Claude workers in isolated git
worktrees, reviews what they return and merges what passes. A desktop app
shows live work, engine status and dependencies, and holds the settings.

Status: in development toward 0.1.0. The design is
`docs/design/0001-first-release.md`.

## Install

Ember runs on Ubuntu 26.04 on x86_64. Download `ember_<version>_amd64.deb`
from the GitHub release, then install it and link the `ember` skill into
Claude Code:

```bash
sudo apt install ./ember_<version>_amd64.deb
emb skill install
```

## Development

uv builds the environment, pre-commit runs ruff at each commit, and
`make check` runs the full check:

```bash
uv sync
uv run pre-commit install
make check
```
