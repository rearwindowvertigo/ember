# Releasing

- Versions follow Semantic Versioning 2.0.0. The version is set in
  `pyproject.toml`.
- `CHANGELOG.md` follows Keep a Changelog 1.1.0, with one section per
  release headed by its version and date.
- Release Please, run as a GitHub Action, reads the Conventional Commits on
  `main`, keeps a release pull request open with the next version and its
  changelog section, and on merge tags the release `v<version>` and creates
  the GitHub release.
- `feat` commits raise the minor version, `fix` and `perf` the patch version,
  and a breaking change the major version. Before 1.0.0, a breaking change
  raises the minor version.
