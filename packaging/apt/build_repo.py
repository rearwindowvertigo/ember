"""Build a signed apt repository from the newest three Ember packages.

Exit codes are 0 on success, 1 on a build or verification failure, 64 on
a usage error, 66 for missing input or public key, and 69 for a missing tool.
"""

import argparse
import gzip
import shutil
import subprocess
import sys
from collections.abc import Sequence
from functools import cmp_to_key
from pathlib import Path
from tempfile import TemporaryDirectory

POOL = Path("pool/main/e/ember")
STABLE = Path("dists/stable")
KEYRING = "ember-archive-keyring.asc"
RETAIN = 3


class BuildError(Exception):
    """A build failure with its exit code."""

    def __init__(self, code: int, message: str) -> None:
        """Keep the exit code and the message for standard error."""
        super().__init__(message)
        self.code = code


def _run(*args: str | Path, cwd: Path | None = None) -> bytes:
    return subprocess.run(args, cwd=cwd, stdout=subprocess.PIPE, check=True).stdout


def _compare_versions(left: str, right: str) -> int:
    for operator, order in (("lt", -1), ("gt", 1)):
        result = subprocess.run(
            ["dpkg", "--compare-versions", left, operator, right], check=False
        )
        if result.returncode == 0:
            return order
        if result.returncode != 1:
            raise BuildError(
                1, f"cannot compare Debian versions {left!r} and {right!r}"
            )
    return 0


def _newest_packages(source: Path) -> list[Path]:
    packages: dict[str, Path] = {}
    for path in sorted(source.glob("*.deb")):
        metadata = _run(
            "dpkg-deb",
            "--show",
            r"--showformat=${Package}\t${Version}\t${Architecture}",
            path.absolute(),
        )
        package, version, arch = metadata.decode().split("\t")
        if package == "ember" and arch == "amd64":
            if version in packages:
                raise BuildError(1, f"multiple Ember packages have version {version}")
            packages[version] = path
    if not packages:
        raise BuildError(66, f"no Ember amd64 .deb files in {source}")
    versions = sorted(packages, key=cmp_to_key(_compare_versions), reverse=True)
    return [packages[version] for version in versions[:RETAIN]]


def _write_indexes(root: Path) -> None:
    index = root / STABLE / "main/binary-amd64"
    index.mkdir(parents=True)
    packages = _run("apt-ftparchive", "packages", POOL, cwd=root)
    (index / "Packages").write_bytes(packages)
    (index / "Packages.gz").write_bytes(gzip.compress(packages, mtime=0))
    fields = {
        "Origin": "Ember",
        "Label": "Ember",
        "Suite": "stable",
        "Codename": "stable",
        "Architectures": "amd64",
        "Components": "main",
    }
    options = []
    for key, value in fields.items():
        options.extend(("-o", f"APT::FTPArchive::Release::{key}={value}"))
    release = _run("apt-ftparchive", *options, "release", STABLE, cwd=root)
    (root / STABLE / "Release").write_bytes(release)


def _sign_and_verify(root: Path, gnupghome: Path, key_id: str) -> None:
    gpg = ("gpg", "--no-options", "--batch")
    signing = (*gpg, "--homedir", gnupghome, "--local-user", key_id, "--armor")
    release = root / STABLE / "Release"
    inrelease = release.with_name("InRelease")
    detached = release.with_name("Release.gpg")
    _run(*signing, "--output", inrelease, "--clearsign", release)
    _run(*signing, "--output", detached, "--detach-sign", release)

    with TemporaryDirectory(prefix="ember-apt-verify-") as home:
        verify = (*gpg, "--homedir", home, "--no-auto-key-retrieve")
        try:
            _run(*verify, "--import", root / KEYRING)
            _run(*verify, "--verify", inrelease)
            _run(*verify, "--verify", detached, release)
        except subprocess.CalledProcessError as error:
            raise BuildError(
                1, "cannot verify Release signatures with the public key"
            ) from error


def build(
    source: Path, out: Path, gnupghome: Path, key_id: str, public_key: Path
) -> None:
    """Build and verify the repository before moving it into the output folder."""
    if not source.is_dir():
        raise BuildError(66, f"input folder is missing: {source}")
    if not public_key.is_file():
        raise BuildError(66, f"public key is missing: {public_key}")
    for tool in ("dpkg-deb", "dpkg", "apt-ftparchive", "gpg"):
        if shutil.which(tool) is None:
            raise BuildError(69, f"{tool} is missing")
    packages = _newest_packages(source)
    out.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".ember-apt-", dir=out.parent) as stage:
        root = Path(stage) / "apt"
        pool = root / POOL
        pool.mkdir(parents=True)
        for package in packages:
            shutil.copyfile(package, pool / package.name)
        _write_indexes(root)
        shutil.copyfile(public_key, root / KEYRING)
        _sign_and_verify(root, gnupghome, key_id)
        root.rename(out)


def main(argv: Sequence[str] | None = None) -> int:
    """Build the repository and print its path on success."""
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        epilog=__doc__.split("\n\n", 1)[1],
    )
    parser.add_argument(
        "--input", type=Path, required=True, help="folder of .deb files"
    )
    parser.add_argument(
        "--out", type=Path, required=True, help="new or empty output folder"
    )
    parser.add_argument(
        "--gnupghome", type=Path, required=True, help="signing keyring folder"
    )
    parser.add_argument(
        "--key-id", required=True, help="GPG signing key ID or fingerprint"
    )
    parser.add_argument(
        "--public-key", type=Path, required=True, help="armored public key file"
    )
    try:
        args = parser.parse_args(argv)
    except SystemExit as error:
        return 0 if error.code == 0 else 64
    try:
        build(args.input, args.out, args.gnupghome, args.key_id, args.public_key)
    except BuildError as error:
        print(f"build_repo: {error}", file=sys.stderr)
        return error.code
    except (OSError, subprocess.CalledProcessError, ValueError) as error:
        print(f"build_repo: {error}", file=sys.stderr)
        return 1
    print(args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
