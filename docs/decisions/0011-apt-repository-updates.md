# ADR 0011: Apt repository updates

- Status: Accepted, 2026-11-23.
- Release: Ember 0.33.0.

## Context

Ember ships an amd64 .deb as a GitHub release asset. Upgrading an existing
installation required downloading and installing that asset. The owner
settled the repository and update design in issue #363.

## Decision

1. The release workflow publishes a signed apt repository on GitHub Pages
   at `https://rearwindowvertigo.github.io/ember/apt/`. It uses suite
   `stable`, component `main` and architecture `amd64`, and holds the newest
   three stable versions. This replaces the apt repository line in P10 of
   [design 0002](../design/0002-after-0-2-0.md).
2. The owner creates an Ed25519 OpenPGP signing key that expires after two
   years. GitHub Actions stores the private key in `APT_SIGNING_KEY`. The
   public key is committed at `packaging/apt/ember-archive-keyring.asc`
   and used to verify the repository signatures before deployment.
3. The .deb installs the public key at
   `/usr/share/keyrings/ember-archive-keyring.asc` and a deb822 source at
   `/etc/apt/sources.list.d/ember.sources` that names it in `Signed-By`.
4. Ember checks the package index over HTTPS at startup and once a day
   while `updates.check` is on. The setting defaults to on and is
   available on the Settings screen. Checking requires no password.
5. On confirmation in the app, PackageKit verifies and installs the
   update. Ember talks to PackageKit over QtDBus, polkit handles
   authentication, and the app restarts through its existing restart path.

## Consequences

- GitHub Pages serves the repository without signing in once the project
  repository is public (#362).
- An installation older than the apt source moves onto it by installing
  a repository-enabled release from its GitHub asset once.
- The `ember` skill link points to `/usr/share/ember/skill`. A package
  upgrade updates the installed skill.
- The owner renews the archive key before expiry and publishes the
  renewed public key in a release as described in
  [Releasing](../releasing.md#signing-key).
