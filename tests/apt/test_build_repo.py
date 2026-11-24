import gzip
import hashlib
import os
import shutil
import subprocess
import sys
from datetime import UTC
from email.utils import parsedate_to_datetime
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "packaging/apt/build_repo.py"
POOL = "pool/main/e/ember"
STABLE = "dists/stable"
INDEX = "main/binary-amd64"
KEYRING = "ember-archive-keyring.asc"
SELECTED = ("0.10.0", "0.10.0-2", "0.10.0-10")


@pytest.fixture(autouse=True)
def _require_tools() -> None:
    for tool in ("dpkg-deb", "dpkg", "apt-ftparchive", "gpg"):
        if shutil.which(tool) is None:
            pytest.skip(f"{tool} is not installed")


def _run(
    *args: str | Path, cwd: Path | None = None, env: dict[str, str] | None = None
) -> bytes:
    result = subprocess.run(args, cwd=cwd, env=env, capture_output=True, check=False)
    assert result.returncode == 0, (result.stdout + result.stderr).decode()
    return result.stdout


def _make_key(home: Path, identity: str) -> tuple[Path, str, Path]:
    home.mkdir(mode=0o700)
    gpg = ("gpg", "--no-options", "--batch", "--homedir", home)
    _run(
        *gpg, "--pinentry-mode", "loopback", "--passphrase", "",
        "--quick-generate-key", identity, "ed25519", "sign", "2y",
    )  # fmt: skip
    keys = _run(*gpg, "--with-colons", "--list-secret-keys").decode().splitlines()
    fingerprint = next(line.split(":")[9] for line in keys if line.startswith("fpr:"))
    public_key = home / KEYRING
    public_key.write_bytes(_run(*gpg, "--armor", "--export", fingerprint))
    return home, fingerprint, public_key


@pytest.fixture
def signing_key(tmp_path: Path) -> tuple[Path, str, Path]:
    return _make_key(tmp_path / "signing", "Ember test archive")


@pytest.fixture
def packages(tmp_path: Path) -> Path:
    source = tmp_path / "debs"
    source.mkdir()
    for version in ("0.8.0", "0.9.0", "0.10.0~rc1", *SELECTED):
        stage = tmp_path / f"build-{version}"
        control = stage / "DEBIAN/control"
        control.parent.mkdir(parents=True)
        control.write_text(
            "Package: ember\n"
            f"Version: {version}\n"
            "Architecture: amd64\n"
            "Maintainer: Ember tests\n"
            "Description: Ember archive test package\n"
            " Second line of the description.\n"
            " .\n"
            " Another paragraph.\n",
            encoding="utf-8",
        )
        _run("dpkg-deb", "--build", stage, source / f"ember_{version}_amd64.deb")
    return source


def _build(
    source: Path, out: Path, gnupghome: Path, key_id: str, public_key: Path
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--input",
            str(source),
            "--out",
            str(out),
            "--gnupghome",
            str(gnupghome),
            "--key-id",
            key_id,
            "--public-key",
            str(public_key),
        ],  # fmt: skip
        text=True,
        capture_output=True,
        check=False,
    )


@pytest.fixture
def repository(
    tmp_path: Path, packages: Path, signing_key: tuple[Path, str, Path]
) -> Path:
    out = tmp_path / "site/apt"
    out.mkdir(parents=True)
    result = _build(packages, out, *signing_key)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(out)
    return out


def _fields(text: str) -> dict[str, str]:
    return dict(
        line.split(": ", 1)
        for line in text.splitlines()
        if ": " in line and not line.startswith(" ")
    )


def _checksums(release: str, field: str) -> dict[str, tuple[str, int]]:
    checksums = {}
    for line in release.split(f"{field}:\n", 1)[1].splitlines():
        if not line.startswith(" "):
            break
        digest, size, path = line.split()
        checksums[path] = digest, int(size)
    return checksums


def test_repository_tree_and_hashes(
    repository: Path, packages: Path, signing_key: tuple[Path, str, Path]
) -> None:
    selected = {f"ember_{version}_amd64.deb" for version in SELECTED}
    stable = repository / STABLE
    index = stable / INDEX
    assert {
        str(path.relative_to(repository))
        for path in repository.rglob("*")
        if path.is_file()
    } == {
        *(f"{POOL}/{name}" for name in selected),
        f"{STABLE}/{INDEX}/Packages",
        f"{STABLE}/{INDEX}/Packages.gz",
        f"{STABLE}/Release",
        f"{STABLE}/InRelease",
        f"{STABLE}/Release.gpg",
        KEYRING,
    }
    assert (repository / KEYRING).read_bytes() == signing_key[2].read_bytes()
    package_index = (index / "Packages").read_bytes()
    assert gzip.decompress((index / "Packages.gz").read_bytes()) == package_index
    records = [
        _fields(paragraph) for paragraph in package_index.decode().strip().split("\n\n")
    ]
    assert {fields["Version"] for fields in records} == set(SELECTED)
    assert len(records) == len(selected)
    for fields in records:
        name = f"ember_{fields['Version']}_amd64.deb"
        assert fields["Filename"] == f"{POOL}/{name}"
        data = (repository / fields["Filename"]).read_bytes()
        assert data == (packages / name).read_bytes()
        assert fields["SHA256"] == hashlib.sha256(data).hexdigest()
    assert b"Description: Ember archive test package\n Second line" in package_index
    assert b"\n .\n Another paragraph.\n" in package_index

    release = (stable / "Release").read_text(encoding="utf-8")
    fields = _fields(release)
    assert parsedate_to_datetime(fields.pop("Date")).tzinfo == UTC
    assert fields == {
        "Origin": "Ember",
        "Label": "Ember",
        "Suite": "stable",
        "Codename": "stable",
        "Architectures": "amd64",
        "Components": "main",
    }
    for field, algorithm in (("MD5Sum", "md5"), ("SHA256", "sha256")):
        expected = {}
        for name in ("Packages", "Packages.gz"):
            data = (index / name).read_bytes()
            digest = hashlib.new(algorithm, data, usedforsecurity=False).hexdigest()
            expected[f"{INDEX}/{name}"] = digest, len(data)
        assert _checksums(release, field) == expected


def test_both_signatures_verify(repository: Path, tmp_path: Path) -> None:
    verify_home = tmp_path / "verify"
    verify_home.mkdir(mode=0o700)
    gpg = ("gpg", "--no-options", "--batch", "--homedir", verify_home)
    _run(*gpg, "--import", repository / KEYRING)
    stable = repository / STABLE
    assert (
        (stable / "InRelease")
        .read_bytes()
        .startswith(b"-----BEGIN PGP SIGNED MESSAGE-----\n")
    )
    assert (
        (stable / "Release.gpg")
        .read_bytes()
        .startswith(b"-----BEGIN PGP SIGNATURE-----\n")
    )
    _run(*gpg, "--verify", stable / "InRelease")
    _run(*gpg, "--verify", stable / "Release.gpg", stable / "Release")


def test_rejects_signatures_from_a_different_key(
    tmp_path: Path, packages: Path, signing_key: tuple[Path, str, Path]
) -> None:
    _, _, other_key = _make_key(tmp_path / "other", "Other test archive")
    home, fingerprint, _ = signing_key
    out = tmp_path / "apt"
    result = _build(packages, out, home, fingerprint, other_key)
    assert result.returncode == 1
    assert "cannot verify Release signatures with the public key" in result.stderr
    assert result.stdout == ""
    assert not out.exists()


def test_rejects_an_empty_input(
    tmp_path: Path, signing_key: tuple[Path, str, Path]
) -> None:
    source = tmp_path / "empty"
    source.mkdir()
    out = tmp_path / "apt"
    result = _build(source, out, *signing_key)
    assert result.returncode == 66
    assert "no Ember amd64 .deb files" in result.stderr
    assert result.stdout == ""
    assert not out.exists()


def test_apt_fetches_the_newest_package(
    repository: Path, packages: Path, tmp_path: Path
) -> None:
    if shutil.which("apt-get") is None:
        pytest.skip("apt-get is not installed")
    home = tmp_path / "apt-state"
    for folder in ("lists", "cache", "config.d", "downloads"):
        (home / folder).mkdir(parents=True)
    (home / "status").touch()
    config = home / "apt.conf"
    config.write_text(
        f'Dir::Etc "{home}";\nDir::Etc::Parts "config.d";\n', encoding="utf-8"
    )
    source = home / "ember.sources"
    source.write_text(
        "Types: deb\n"
        f"URIs: {repository.as_uri()}\n"
        "Suites: stable\n"
        "Components: main\n"
        "Architectures: amd64\n"
        f"Signed-By: {repository / KEYRING}\n",
        encoding="utf-8",
    )
    apt = (
        "apt-get",
        "-o", f"Dir::Etc::SourceList={source}",
        "-o", "Dir::Etc::SourceParts=-",
        "-o", "Dir::Etc::Trusted=-",
        "-o", "Dir::Etc::TrustedParts=-",
        "-o", f"Dir::State={home}",
        "-o", f"Dir::State::status={home / 'status'}",
        "-o", f"Dir::Cache={home / 'cache'}",
        "-o", "APT::Architecture=amd64",
    )  # fmt: skip
    env = {**os.environ, "APT_CONFIG": str(config)}
    downloads = home / "downloads"
    _run(*apt, "update", cwd=downloads, env=env)
    _run(*apt, "download", "ember", cwd=downloads, env=env)
    name = f"ember_{SELECTED[-1]}_amd64.deb"
    assert [file.name for file in downloads.iterdir()] == [name]
    index = repository / STABLE / INDEX / "Packages"
    records = [
        _fields(paragraph) for paragraph in index.read_text().strip().split("\n\n")
    ]
    selected = next(record for record in records if record["Version"] == SELECTED[-1])
    digest = hashlib.sha256((downloads / name).read_bytes()).hexdigest()
    assert (
        digest
        == selected["SHA256"]
        == hashlib.sha256((packages / name).read_bytes()).hexdigest()
    )
