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
  default merge message, so Release Please reads only the pull request's own
  commits.
- The release pull request is merged only when the release's target in its
  design document is complete.
