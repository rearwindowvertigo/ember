# Ember

Ember is a Linux desktop application and command-line tool that lets Claude
lead AI coding workers on one machine. Claude splits the work, Codex, OpenCode
or Claude workers do it in isolated git worktrees, and Claude reviews and
merges the result. The owner watches and configures it in the desktop app.

The specification is `docs/plans/0.1.0-first-release.md`. Decisions are in
`docs/decisions/`.

## Rules for every change

- Committed text states what is true and stops: documentation, decision
  records, commit messages and pull request text. It does not say what a
  finding means or how large it is.
- No en dash. Use "to" for a range in a sentence and a hyphen for a bare range
  in a table or heading.
- Documents describe Ember as it is. The repository's history starts at its
  first commit.
- Work lands through a branch and a squash merge to `main` on the owner's word.
  Commit subjects follow Conventional Commits (`feat:`, `fix:`, `docs:`,
  `test:`, `refactor:`, `chore:`).
- Versions follow Semantic Versioning. Every merge to `main` that changes
  behaviour sets the version in `pyproject.toml` and heads its
  `CHANGELOG.md` section with that version and the date. A release is tagged
  `v<version>` on `main`.
- A plan is named "plan <version>, draft <n>"; a bare version means released
  code.
- Never commit a credential, token or login file.
- Tests use stub tools only. No test reaches the network or starts a real
  worker or engine.

## Layout

| Path | Holds |
|---|---|
| `src/ember/` | The Python package: core, engines, runs, flows, the `emb` command and the desktop app |
| `tests/` | pytest tests, with stub engine and git tools |
| `skill/` | The `ember` skill for Claude Code, with its references and eval cases |
| `packaging/` | The `.deb` build, the desktop entry and icons |
| `docs/decisions/` | Architecture decision records, numbered |
| `docs/plans/` | Plans, one file per planned version |
| `docs/reference.md` | Current behaviour, once built |
