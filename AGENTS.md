# Ember

Ember is a Linux desktop application and command-line tool that lets Claude
lead AI coding workers on one machine. Claude splits the work, Codex, OpenCode
or Claude workers do it in isolated git worktrees, and Claude reviews and
merges the result. The owner watches and configures it in the desktop app.

The design is in `docs/design/`. Decisions are in `docs/decisions/`. Releases
follow `docs/releasing.md`.

## Rules for every change

- Committed text states what is true and stops: documentation, decision
  records, commit messages and pull request text. It does not say what a
  finding means or how large it is.
- Documents describe Ember as it is. The repository's history starts at its
  first commit.
- Commit messages follow Conventional Commits 1.0.0:
  `<type>[optional scope][!]: <description>`. Types: `feat`, `fix`, `perf`,
  `refactor`, `docs`, `test`, `build`, `ci`, `style`, `chore`, `revert`.
  A breaking change adds `!` after the type or scope, or a
  `BREAKING CHANGE:` footer.
- Never commit a credential, token or login file.

## Layout

| Path | Holds |
|---|---|
| `src/ember/` | The Python package: core, engines, runs, flows, the `emb` command and the desktop app |
| `tests/` | pytest tests |
| `skill/` | The `ember` skill for Claude Code, with its references and eval cases |
| `packaging/` | The `.deb` build, the desktop entry and icons |
| `docs/design/` | Design documents, numbered, each with a status and target release |
| `docs/decisions/` | Architecture decision records, numbered |
| `docs/releasing.md` | Versioning, changelog and tags |
| `docs/reference.md` | Current behaviour, once built |
