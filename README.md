# Ember

Ember coordinates AI coding workers on one Linux machine. The lead agent
splits the work, hands it to Codex, OpenCode or Claude Code workers in
isolated git worktrees, reviews their results and merges what passes. The
desktop app shows live work, engine status and dependencies, and holds the
settings.

Claude Code and OpenCode V2 can both host the lead. The lead's model and
the engines and models it runs as workers are independent choices.

Work is tracked in GitHub Issues, with one milestone per release. Designs
for 0.1.0 to 0.5.0 are in `docs/design/`.

## Install

Ember runs on Ubuntu 26.04 on x86_64. Download `ember_<version>_amd64.deb`
from the GitHub release, then install it and link the `ember` skill:

```bash
sudo apt install ./ember_<version>_amd64.deb
emb skill install
```

The default link, `~/.claude/skills/ember`, works in both Claude Code and
OpenCode V2. OpenCode can use it without Claude Code installed. Use
`emb skill install --harness opencode` for OpenCode's native skills folder
instead. See [skill installation](docs/reference.md#emb-skill) for paths
and status commands.

Ask the lead to use the Ember skill; in OpenCode you can also reference it
as `@ember`.

## Development

uv builds the environment, pre-commit runs ruff at each commit, and
`make check` runs the full check:

```bash
uv sync
uv run pre-commit install
make check
```
