# Releasing

- Versions follow Semantic Versioning 2.0.0. The version is set in
  `pyproject.toml`.
- Release Please, run as a GitHub Action, reads the Conventional Commits on
  `main`, keeps a release pull request open with the next version and its
  changelog section, and on merge tags the release `v<version>` and creates
  the GitHub release.
- `feat` commits raise the minor version, `fix` and `perf` the patch version,
  and a breaking change the major version. Before 1.0.0, a breaking change
  raises the minor version.
- Release Please writes `CHANGELOG.md`. Each release has a section headed by
  its version and date, with these subsections: Added (`feat`), Changed
  (`perf`, `revert`) and Fixed (`fix`), and a breaking-changes subsection
  when a commit is marked breaking. `docs`, `build`, `ci`, `test`,
  `refactor`, `style` and `chore` commits are not listed.
- Pull requests merge into `main` with a merge commit that keeps GitHub's
  default subject and has an empty body (`gh pr merge <n> --merge --body
  ""`), so Release Please reads only the pull request's own commits. A body
  holding the pull request's title is read as one more change.
- Worker branches land through `emb merge --subject '<line>'` as one squash
  commit each, with the lead's subject in the repository's commit convention.
  Completed flow branches merge with a merge commit and keep those task
  squash commits.
- A `BEGIN_COMMIT_OVERRIDE` / `END_COMMIT_OVERRIDE` block in a merged pull
  request's body applies to every commit that pull request brings to `main`,
  including its merge commit.
- A pull request with more than one commit uses no override entry that
  Release Please lists, since each commit repeats it. Its changes land as
  one commit whose message holds one entry per change, in the multi-change
  footer format in Release Please's README.
- The release pull request is merged only when every issue in the
  release's milestone is closed. The milestone is then closed.
- Release Please also sets the project's version in `uv.lock` and the
  `Skill version:` line in `skill/SKILL.md` (`extra-files` in
  `release-please-config.json`). The skill line carries the generic
  updater's `<!-- x-release-please-version -->` annotation. Both versions
  match `pyproject.toml` after each release.

## Skill eval fixtures

`make eval-fixtures` regenerates the CLI help and canned results used by
the skill evals. Claim replies and skill-version warnings are generated
through the CLI with an isolated installed-skill link. Generated help and
result fixtures are produced from the checkout at eval setup, outside
`skill/evals`; they are not committed. A parser or result-shape
change is checked by regenerating fixtures and running
`tests/skill/test_evals.py`. Single-worker dry-run text and JSON are
generated with the worker selector and formatter; the fixture driver
supplies effort validation without contacting an engine. Gap check results
are generated with the result builder and validated against the packaged
`ember/data/gapcheck-result.schema.json`.
Provider-error eval streams and status replies are generated with the
Codex classifier and run formatters, including the exit-73 `next:` lines.
The `sandbox-denied-paths` eval generates warnings, owner actions and
status through the sandbox detector and run outcome and status writers in
an isolated home folder, including secret and protected paths.
The `flow-shared-document-task` eval computes waves from the submitted
plan with `ember.flows.plan`; it has no canned dry-run fixture.

## The `.deb` package

- `make deb` runs `packaging/deb/build_deb.py`, which builds
  `dist/ember_<version>_amd64.deb` on Ubuntu 26.04 on x86_64. It needs
  `/usr/bin/python3.14`, `uv`, `dpkg-deb`, `objdump`, and the packages the
  `.deb` depends on (`python3 packaging/deb/build_deb.py --depends` lists
  them). Its exit codes: 0 built; 1 a build command failed; 64 usage error;
  65 the version in `pyproject.toml` is not `X.Y.Z`; 66 `skill/SKILL.md` is
  missing; 69 a tool is missing; 70 a Qt library or module file is missing or
  unreadable, a Qt library needs a library that no package in `Depends`
  provides, or a staged file holds the staging path.
- When Release Please creates a release, the `deb` job of
  `.github/workflows/release-please.yml` checks out the release tag on
  `ubuntu-26.04`, runs `make deb`, prints the file's SHA-256 and attaches
  `ember_<version>_amd64.deb` to the GitHub release.
- The workflow's manual run, with the tag as its `tag` input, rebuilds the
  file from that tag and replaces the attached one.
- Without the job, `make deb` on Ubuntu 26.04 builds the file in `dist/`, and
  `gh release upload v<version> dist/ember_<version>_amd64.deb` attaches it.
- The owner installs a release in an empty folder with three commands, in
  this order:

  1. `gh release download v<version> --repo rearwindowvertigo/ember --pattern '*.deb'`
  2. `sudo apt install ./ember_<version>_amd64.deb`
  3. `emb skill install`
