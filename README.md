# Ember

Ember lets Claude lead AI coding workers on one Linux machine. Claude splits
a task, hands the parts to Codex, OpenCode or Claude workers in isolated git
worktrees, reviews what they return and merges what passes. A desktop app
shows live work, engine status and dependencies, and holds the settings.

The design documents are in `docs/design/`:
`0001-first-release.md` covers 0.1.0 and 0.2.0, and `0002-after-0-2-0.md`
covers 0.2.1 and 0.3.0.

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
