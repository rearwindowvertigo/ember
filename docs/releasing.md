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
- Release Please also sets the project's version in `uv.lock`
  (`extra-files` in `release-please-config.json`), so `uv.lock` matches
  `pyproject.toml` after each release.

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
- The owner installs a release with
  `sudo apt install ./ember_<version>_amd64.deb`, then runs
  `emb skill install` once.
