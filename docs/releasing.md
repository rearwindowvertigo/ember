# Releasing

- Versions follow Semantic Versioning 2.0.0. The version is set in
  `pyproject.toml`.
- Release Please, run as a GitHub Action, reads the Conventional Commits on
  `main`, keeps a release pull request open with the next version and its
  changelog section, and on merge tags the release `v<version>` and creates
  the GitHub release.
- The `check` workflow runs `dev/release_only.py` first. When a pull request
  changes only `CHANGELOG.md`, `.release-please-manifest.json` and the
  project version (in `pyproject.toml`, in the `ember-app` entry of
  `uv.lock` and on the `Skill version:` line of `skill/SKILL.md`), the job
  passes without the full check. Any other change runs `make check`.
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

Before a change that moves, removes or rewrites rules in `skill/SKILL.md`
or `skill/references/` merges into `main`, its author lists the cases whose
graders exercise those rules and runs each five times with
`bash skill/evals/_runner/run_repeat.sh --runs 5 <case dir> <out dir>`.
The author grades the runs with `skill/evals/_runner/grade.py` and records
`tally.py`'s counts on the change's issue. The change merges when every
listed case shows `5/5` in the `skill` column of `all graders`.

When a case falls short, the author finds the root cause before changing
anything: what causes the miss, which other cases and rules the same cause
affects, and the fix that covers all of them, not only the run that
failed. Where a change to a design or a decision would serve the intended
behaviour better than a patch, the author says so to the owner. The cause
and the fix are recorded on the change's issue. The fix changes the skill
text, a grader or the fixture, and the case is then run five more times.
The same text is not run again to get a better batch.
The [eval README](../skill/evals/README.md#repeated-runs-_runnerrun_repeatsh-and-_runnertallypy)
describes repeated runs, grading and tallying.

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
  65 the version in `pyproject.toml` is not `X.Y.Z`; 66 `skill/SKILL.md` or
  the archive public key is missing; 69 a tool is missing; 70 a Qt library
  or module file is missing or unreadable, a Qt library needs a library that
  no package in `Depends` provides, or a staged file holds the staging path.
- When Release Please creates a release, the `deb` job of
  `.github/workflows/release-please.yml` checks out the release tag on
  `ubuntu-26.04`, runs `make deb`, prints the file's SHA-256 and attaches
  `ember_<version>_amd64.deb` to the GitHub release.
- The workflow's manual run, with the tag as its `tag` input, rebuilds the
  file from that tag. It uploads the .deb if the release has no asset with
  that name. If the asset exists, the job downloads it and compares its
  SHA-256 with the rebuild. Matching files leave the existing asset in
  place and the job succeeds. A changed digest requires a new version.
  An unreadable asset fails the job.
- Without the job, `make deb` on Ubuntu 26.04 builds the file in `dist/`, and
  `gh release upload v<version> dist/ember_<version>_amd64.deb` attaches it.

## The apt repository

GitHub Pages serves Ember's signed apt repository at
<https://rearwindowvertigo.github.io/ember/apt/>. It holds the three highest
published stable versions for `stable/main` on `amd64`. The package installs
`/etc/apt/sources.list.d/ember.sources` with `Signed-By` set to
`/usr/share/keyrings/ember-archive-keyring.asc`.

The `publish-apt` job runs after a successful `deb` job, for both a release
created by Release Please and a manual rebuild. It checks out the workflow's
revision, selects the three highest stable version tags after entering its
concurrency group, and downloads their .deb assets with `gh`. The group
queues publication jobs through deployment. Each `deb` job runs independently.
The publish job imports the `APT_SIGNING_KEY` Actions secret into a temporary
`GNUPGHOME` and uses the committed
`packaging/apt/ember-archive-keyring.asc` to verify the build.

`packaging/apt/build_repo.py` takes an input folder of .debs and a new or
empty output folder. It uses `dpkg` version ordering and `apt-ftparchive`
from `apt-utils` to generate `Packages` and `Release`. It gzip-compresses
the same `Packages` bytes, writes a clear-signed copy of `Release` to
`InRelease` and an armored detached signature to `Release.gpg`, and
copies the public key to the root of the apt tree. It checks both signatures
in a separate keyring containing the committed public key before returning
the tree for deployment.

```sh
python3 packaging/apt/build_repo.py \
  --input debs --out site/apt --gnupghome "$GNUPGHOME" \
  --key-id "$fingerprint" --public-key packaging/apt/ember-archive-keyring.asc
```

The job uploads `site/` as a Pages artifact and deploys it to the
`github-pages` environment. In the repository's **Settings → Pages**,
set **Build and deployment → Source** to **GitHub Actions**. The
repository is available without signing in once the GitHub repository
is public (#362).

### Signing key

Before the first release with apt, create an Ed25519 signing key that
expires after two years. The release job signs in batch mode, so the
key in its secret has an empty passphrase. List its fingerprint and use
the full fingerprint in the export commands.

```sh
gpg --batch --pinentry-mode loopback --passphrase '' \
  --quick-generate-key 'Ember Archive Signing' ed25519 sign 2y
gpg --list-secret-keys --fingerprint 'Ember Archive Signing'
```

```sh
fingerprint=FULL_KEY_FINGERPRINT
gpg --armor --export "$fingerprint" > packaging/apt/ember-archive-keyring.asc
gpg --batch --armor --export-secret-keys "$fingerprint" | gh secret set APT_SIGNING_KEY
```

Commit the armored public key at `packaging/apt/ember-archive-keyring.asc`.
The package build needs this file. Keep the private key in `APT_SIGNING_KEY`.

Before expiry, extend the same key for two years with
`gpg --quick-set-expire "$fingerprint" 2y`. Repeat the two export commands
to update the committed public key and the Actions secret. Publish a new
release before the old expiry date so the repository serves the renewed
key and clients can install the package containing it in time.

### Installing from a release asset

On an installation older than the apt source, install a repository-enabled
release from its GitHub release asset once. Subsequent upgrades are
available through apt and the app. Use the same asset commands when the
apt index cannot be read. In an empty folder, set `version` to the release
number and run the following commands.

```sh
version=X.Y.Z
curl -fLO "https://github.com/rearwindowvertigo/ember/releases/download/v${version}/ember_${version}_amd64.deb"
sudo apt install "./ember_${version}_amd64.deb"
```

For a first installation of the Ember skill, run `emb skill install`.
Package upgrades update the installed skill through its existing link.
