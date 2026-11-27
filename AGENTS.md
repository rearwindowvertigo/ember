# Ember

Ember is a Linux desktop application and command-line tool for coordinating
AI coding workers on one machine. The lead agent splits the work, Codex,
OpenCode or Claude Code workers do it in isolated git worktrees, and the lead
reviews and merges the result. The owner watches and configures it in the
desktop app.

Work is tracked in GitHub Issues: one issue per bug, feature or task, with
one type label (`bug`, `feature`, `chore`), one area label (`app`, `cli`,
`flows`, `skill`, `packaging`) and the milestone of its target release.
`gh issue list --milestone <version>` lists a release's work. A pull
request that finishes an issue says `Closes #<n>` in its description.
Decisions are in `docs/decisions/`. Releases follow `docs/releasing.md`.

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
- uv manages the environment and lock file; `uv sync` builds it. A
  dependency is added with `uv add`, never by editing `uv.lock`.
- `make check` is the full check: ruff lint and format check, Pyrefly type
  check, and every test with pytest on all cores (pytest-xdist `-n auto`).
  pre-commit runs ruff on the changed files at each commit.
- Every test is independent: its own temporary folders and state, no
  dependence on another test or on run order, safe to run in parallel with
  every other test.
- A pull request merges only after every check GitHub reports for its
  latest commit has passed.

## Conventions

- Where no rule here covers a case, follow the nearest existing Ember code.
  Where there is none, use the most widely used standard for that language or
  tool, and name it in the commit message.
- Python follows PEP 8, enforced by ruff with the rule sets `E`, `W`, `F`,
  `I`, `N`, `UP`, `B`, `SIM`, `ANN` and `D` (Google docstring convention)
  set in `pyproject.toml`. Every function has type hints.
- Ember's own code uses PEP 8 names. Qt's methods and their overrides keep
  Qt's camelCase names (`setWindowTitle`, `closeEvent`).
- Tests mirror the source tree: `src/ember/<path>/<module>.py` is tested in
  `tests/<path>/test_<module>.py`.
- The `emb` command follows the Command Line Interface Guidelines
  (clig.dev): long `--options`, `--help` on every command, data on standard
  output and messages on standard error, `--json` for machine-readable
  output, exit 0 on success and a documented non-zero code otherwise.
- Files live in the XDG base directories. The desktop entry, icons and
  autostart entry follow the freedesktop.org specifications. The app ID is
  `io.github.rearwindowvertigo.Ember`.
- Prefer the standard library and dependencies already in use. A commit that
  adds a dependency names it and the reason in its message.
- Keep this file short. Add a rule only where an agent would otherwise get it
  wrong.

## Layout

| Path | Holds |
|---|---|
| `src/ember/` | The Python package: core, engines, runs, flows, the `emb` command and the desktop app |
| `tests/` | pytest tests |
| `skill/` | The `ember` lead skill, with its references and eval cases |
| `packaging/` | The `.deb` build, the desktop entry and icons |
| `docs/design/` | Designs 0001 to 0004 (0.1.0 to 0.5.0), kept as history, and any later design too large for an issue |
| `docs/decisions/` | Architecture decision records, numbered |
| `docs/releasing.md` | Versioning, changelog and tags |
| `docs/reference.md` | Current behaviour, once built |
