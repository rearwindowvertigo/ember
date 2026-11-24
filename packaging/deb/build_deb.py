"""Build Ember's `.deb` package (design M).

Run from the repository top as `python3 packaging/deb/build_deb.py [--out DIR]`;
`make deb` runs it. The package holds a virtual environment at
`/usr/lib/ember` on `/usr/bin/python3.14` with the locked runtime libraries
and Ember, the `/usr/bin/emb` wrapper, the desktop entry, the icon, the
`ember` skill, the copyright file, the apt source and the archive key. It
has no maintainer scripts.

Exit codes: 0 built; 1 a build command failed; 64 usage error; 65 the
version in `pyproject.toml` is not `X.Y.Z`; 66 `skill/SKILL.md` or
`packaging/apt/ember-archive-keyring.asc` is missing;
69 a tool the build needs is missing; 70 a Qt library or module file is
missing or unreadable, a needed library has no provider, or a staged file
holds the staging path.
"""

import argparse
import gzip
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Iterable, Sequence
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
"""The repository top."""

APP_ID = "io.github.rearwindowvertigo.Ember"
"""The app ID, which names the desktop entry and the icon."""

PYTHON = Path("/usr/bin/python3.14")
"""The system interpreter the package's environment runs on."""

PREFIX = "usr/lib/ember"
"""The environment's folder in the package, relative to the root."""

SITE = f"{PREFIX}/lib/python3.14/site-packages"
"""The environment's site-packages folder, relative to the root."""

ARCHIVE_KEY = Path("packaging/apt/ember-archive-keyring.asc")
"""The armored public archive key, relative to the repository top."""

KEYRING = f"usr/share/keyrings/{ARCHIVE_KEY.name}"
"""The installed archive key, relative to the package root."""

APT_SOURCE = "etc/apt/sources.list.d/ember.sources"
"""The apt source, relative to the package root."""

MAINTAINER = "rearwindowvertigo <254349504+rearwindowvertigo@users.noreply.github.com>"
"""The package's Maintainer field."""

DEPENDS = (
    "python3.14",
    "git",
    "tmux",
    "libc6",
    "libstdc++6",
    "libgcc-s1",
    "zlib1g",
    "libzstd1",
    "libbrotli1",
    "libglib2.0-0t64",
    "libdbus-1-3",
    "libfontconfig1",
    "libfreetype6",
    "libegl1",
    "libgl1",
    "libdrm2",
    "libgssapi-krb5-2",
    "libxkbcommon0",
    "libxkbcommon-x11-0",
    "libwayland-client0",
    "libwayland-cursor0",
    "libwayland-egl1",
    "libwayland-server0",
    "libx11-6",
    "libx11-xcb1",
    "libxcb1",
    "libxcb-cursor0",
    "libxcb-glx0",
    "libxcb-icccm4",
    "libxcb-image0",
    "libxcb-keysyms1",
    "libxcb-randr0",
    "libxcb-render0",
    "libxcb-render-util0",
    "libxcb-shape0",
    "libxcb-shm0",
    "libxcb-sync1",
    "libxcb-util1",
    "libxcb-xfixes0",
    "libxcb-xkb1",
)
"""The packages the package depends on: tools, then the libraries Qt loads."""

RECOMMENDS = ("bubblewrap", "xdg-terminal-exec")
"""The packages the package recommends."""

QT_LIBRARIES = (
    "Core",
    "Gui",
    "Widgets",
    "Network",
    "Svg",
    "DBus",
    "OpenGL",
    "WaylandClient",
    "XcbQpa",
)
"""The Qt libraries whose needs the library check reads."""

QT_PLUGINS = (
    "platforms",
    "wayland-shell-integration",
    "wayland-decoration-client",
    "wayland-graphics-integration-client",
    "xcbglintegrations",
    "imageformats",
    "iconengines",
    "platformthemes",
)
"""The Qt plugin folders whose libraries the library check reads."""

MODULES = ("QtCore", "QtGui", "QtWidgets", "QtNetwork", "QtSvg", "QtDBus")
"""The PySide6 modules Ember imports."""

KEEP_BIN = ("python", "python3", "python3.14")
"""The entries of the environment's `bin/` folder the package keeps."""

VENV_EXTRAS = (".gitignore", ".lock", "CACHEDIR.TAG")
"""The files `uv venv` writes at the environment's top that the package leaves out."""

TOOLS = ("uv", "dpkg-deb", "objdump")
"""The commands the build runs, besides the interpreter."""

DESCRIPTION = """\
Lets Claude lead AI coding workers on one Linux machine
 Claude splits the work, Codex, OpenCode or Claude workers do it in isolated
 git worktrees, and Claude reviews and merges the result. The owner watches
 and configures it in the desktop app. The emb command holds the run, merge,
 cleanup and dependency commands, and the package carries the ember skill for
 Claude Code.
"""


class BuildError(Exception):
    """A build failure with its exit code."""

    def __init__(self, code: int, message: str) -> None:
        """Keep the exit code and the message.

        Args:
            code: The exit code.
            message: The message for standard error.
        """
        super().__init__(message)
        self.code = code


def read_version(pyproject_text: str) -> str:
    """Return the project's version from `pyproject.toml`.

    Args:
        pyproject_text: The text of `pyproject.toml`.

    Returns:
        The `[project] version`.

    Raises:
        BuildError: 65 when the version is absent or not `X.Y.Z`.
    """
    try:
        version = tomllib.loads(pyproject_text).get("project", {}).get("version")
    except tomllib.TOMLDecodeError as error:
        raise BuildError(65, f"pyproject.toml cannot be read: {error}") from error
    if not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise BuildError(65, f"the version in pyproject.toml is not X.Y.Z: {version!r}")
    return version


def control(version: str, installed_kb: int) -> str:
    """Return the `DEBIAN/control` text.

    Args:
        version: The package version.
        installed_kb: The installed size in KiB.

    Returns:
        The control file's text.
    """
    return (
        "Package: ember\n"
        f"Version: {version}\n"
        "Architecture: amd64\n"
        f"Maintainer: {MAINTAINER}\n"
        f"Installed-Size: {installed_kb}\n"
        f"Depends: {', '.join(DEPENDS)}\n"
        f"Recommends: {', '.join(RECOMMENDS)}\n"
        "Section: devel\n"
        "Priority: optional\n"
        "Homepage: https://github.com/rearwindowvertigo/ember\n"
        f"Description: {DESCRIPTION}"
    )


def wrapper() -> str:
    """Return the text of `/usr/bin/emb`.

    `-I` keeps the caller's `PYTHONPATH` and user site out of Ember's
    interpreter.
    """
    return '#!/bin/sh\nexec /usr/lib/ember/bin/python -I -m ember.cli "$@"\n'


def desktop_entry() -> str:
    """Return the desktop entry's text, from `packaging/`."""
    return (REPO / "packaging" / f"{APP_ID}.desktop").read_text(encoding="utf-8")


def skill_files(skill_dir: Path) -> list[Path]:
    """Return the skill's files that the package holds.

    Args:
        skill_dir: The skill folder.

    Returns:
        The files' paths relative to `skill_dir`, sorted, without `evals/`,
        `__pycache__` folders and `.pyc` files.
    """
    found = []
    for path in skill_dir.rglob("*"):
        relative = path.relative_to(skill_dir)
        if relative.parts[0] == "evals" or "__pycache__" in relative.parts:
            continue
        if path.suffix == ".pyc" or not path.is_file():
            continue
        found.append(relative)
    return sorted(found)


def files_holding(root: Path, needle: bytes) -> list[Path]:
    """Return the regular files under `root` whose content holds `needle`.

    Args:
        root: The folder to search.
        needle: The bytes to look for.

    Returns:
        The matching files, sorted.
    """
    found = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        if needle in path.read_bytes():
            found.append(path)
    return found


def needed(objdump_output: str) -> list[str]:
    """Return the `NEEDED` entries of `objdump -p` output.

    Args:
        objdump_output: The output of `objdump -p <file>`.

    Returns:
        The library names, in order.
    """
    return re.findall(r"^\s*NEEDED\s+(\S+)\s*$", objdump_output, flags=re.MULTILINE)


def owners(dpkg_output: str) -> set[str]:
    """Return the package names in `dpkg -S` output, without architecture.

    Args:
        dpkg_output: The output of `dpkg -S <pattern>`.

    Returns:
        The names of the packages that own a matching path.
    """
    names: set[str] = set()
    for line in dpkg_output.splitlines():
        head, separator, _path = line.partition(": ")
        if not separator or head.startswith("diversion "):
            continue
        names.update(item.strip().split(":")[0] for item in head.split(","))
    return names


def _run(args: Sequence[str | Path], **kwargs: object) -> None:
    """Run a build command; raise `BuildError` 1 when it fails.

    The command's standard output goes to standard error, so that standard
    output holds only the built package's line.
    """
    printable = " ".join(str(arg) for arg in args)
    print(f"build_deb: {printable}", file=sys.stderr)
    sys.stderr.flush()
    kwargs.setdefault("stdout", sys.stderr.fileno())
    result = subprocess.run([str(arg) for arg in args], check=False, **kwargs)  # type: ignore[call-overload]
    if result.returncode != 0:
        raise BuildError(1, f"{printable} exited {result.returncode}")


def _check_tools() -> None:
    """Raise `BuildError` 69 naming the first missing tool."""
    if not PYTHON.is_file():
        raise BuildError(69, f"{PYTHON} is missing")
    for tool in TOOLS:
        if shutil.which(tool) is None:
            raise BuildError(69, f"{tool} is missing")


def _clean_environment(stage: Path) -> None:
    """Remove uv's files, launchers, install records and caches from the environment."""
    venv = stage / PREFIX
    for name in VENV_EXTRAS:
        (venv / name).unlink(missing_ok=True)
    bin_dir = venv / "bin"
    for item in bin_dir.iterdir():
        if item.name in KEEP_BIN:
            continue
        if item.is_dir() and not item.is_symlink():
            shutil.rmtree(item)
        else:
            item.unlink()
    site = stage / SITE
    for pattern in ("direct_url.json", "uv_cache.json"):
        for item in site.rglob(pattern):
            item.unlink()
    for item in sorted(site.rglob("__pycache__"), reverse=True):
        shutil.rmtree(item)


def _install(path: Path, data: bytes, mode: int = 0o644) -> None:
    """Write one file into the staging tree with its folders."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    path.chmod(mode)


def _write_files(stage: Path) -> None:
    """Write the package files outside the virtual environment."""
    _install(stage / "usr/bin/emb", wrapper().encode(), 0o755)
    share = stage / "usr/share"
    _install(share / "applications" / f"{APP_ID}.desktop", desktop_entry().encode())
    icon = REPO / "src/ember/app/assets/ember" / f"{APP_ID}-plain.svg"
    _install(share / "icons/hicolor/scalable/apps" / f"{APP_ID}.svg", icon.read_bytes())
    skill = REPO / "skill"
    for relative in skill_files(skill):
        source = skill / relative
        mode = 0o755 if os.access(source, os.X_OK) else 0o644
        _install(share / "ember/skill" / relative, source.read_bytes(), mode)
    doc = share / "doc/ember"
    _install(doc / "copyright", (REPO / "packaging/deb/copyright").read_bytes())
    _install(doc / "icu-LICENSE", (REPO / "packaging/deb/icu-LICENSE").read_bytes())
    changelog = REPO / "CHANGELOG.md"
    if changelog.is_file():
        data = gzip.compress(changelog.read_bytes(), compresslevel=9, mtime=0)
        _install(doc / "changelog.gz", data)
    _install(stage / KEYRING, (REPO / ARCHIVE_KEY).read_bytes())
    apt_source = (
        "Types: deb\n"
        "URIs: https://rearwindowvertigo.github.io/ember/apt/\n"
        "Suites: stable\n"
        "Components: main\n"
        "Architectures: amd64\n"
        f"Signed-By: /{KEYRING}\n"
    )
    _install(stage / APT_SOURCE, apt_source.encode())


def _check_paths(stage: Path) -> None:
    """Raise `BuildError` 70 when a staged file holds the staging path."""
    holding = files_holding(stage, str(stage).encode())
    if holding:
        names = ", ".join(str(path.relative_to(stage)) for path in holding)
        raise BuildError(70, f"staged files hold the staging path: {names}")


def _binaries(site: Path) -> Iterable[Path]:
    """Yield the Qt libraries, plugins and modules the library check reads."""
    qt = site / "PySide6/Qt"
    for name in QT_LIBRARIES:
        yield qt / "lib" / f"libQt6{name}.so.6"
    for folder in QT_PLUGINS:
        yield from sorted((qt / "plugins" / folder).glob("*.so"))
    for module in MODULES:
        yield site / "PySide6" / f"{module}.abi3.so"


def skip_plugin(folder: str, libraries: Iterable[str], bundled: set[str]) -> bool:
    """Return whether the library check skips a Qt plugin.

    Qt skips a plugin it cannot load. A platform theme plugin that needs GTK
    is loaded only where GTK is installed, and a plugin that needs a Qt
    library the package does not carry belongs to a Qt module Ember does not
    use.

    Args:
        folder: The name of the plugin's folder, such as `platformthemes`.
        libraries: The plugin's `NEEDED` entries.
        bundled: The names of the libraries the package carries.

    Returns:
        True when the plugin is skipped.
    """
    for name in libraries:
        if folder == "platformthemes" and name.startswith("libgtk"):
            return True
        if name.startswith("libQt6") and name not in bundled:
            return True
    return False


def _check_libraries(stage: Path) -> None:
    """Raise `BuildError` 70 naming each needed library nothing provides.

    A library is provided when it is bundled in `PySide6/Qt/lib`, `PySide6/`
    or `shiboken6/`, or when `dpkg -S` finds it in a package named in
    `Depends`. Plugins that `skip_plugin` names are not read.
    """
    site = stage / SITE
    bundled = {
        path.name
        for folder in (site / "PySide6/Qt/lib", site / "PySide6", site / "shiboken6")
        for path in folder.iterdir()
    }
    depends = set(DEPENDS)
    known: dict[str, bool] = {}
    problems = []
    for binary in _binaries(site):
        shown = binary.relative_to(stage)
        if not binary.is_file():
            problems.append(f"{shown} is missing")
            continue
        result = subprocess.run(
            ["objdump", "-p", str(binary)], capture_output=True, text=True, check=False
        )
        if result.returncode != 0:
            problems.append(f"objdump cannot read {shown}")
            continue
        libraries = needed(result.stdout)
        plugin = binary.parent.parent.name == "plugins"
        if plugin and skip_plugin(binary.parent.name, libraries, bundled):
            print(f"build_deb: skipped {shown}", file=sys.stderr)
            continue
        for name in libraries:
            if name in bundled:
                continue
            if name not in known:
                found = subprocess.run(
                    ["dpkg", "-S", f"*/{name}"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                known[name] = bool(owners(found.stdout) & depends)
            if not known[name]:
                problems.append(
                    f"{shown} needs {name}, which no package in Depends provides"
                )
    if problems:
        raise BuildError(70, "; ".join(problems))


def _set_modes(stage: Path) -> None:
    """Set folders to 0755, executables to 0755 and other files to 0644."""
    for path in [stage, *stage.rglob("*")]:
        if path.is_symlink():
            continue
        if path.is_dir():
            path.chmod(0o755)
        else:
            path.chmod(0o755 if path.stat().st_mode & 0o111 else 0o644)


def _installed_kb(stage: Path) -> int:
    """Return the staged size in KiB, rounded up."""
    total = sum(
        path.lstat().st_size
        for path in stage.rglob("*")
        if path.is_symlink() or path.is_file()
    )
    return -(-total // 1024)


def _md5sums(stage: Path) -> str:
    """Return the `DEBIAN/md5sums` text for the staged regular files."""
    lines = []
    for path in sorted(stage.rglob("*")):
        relative = path.relative_to(stage)
        if relative.parts[0] == "DEBIAN" or path.is_symlink() or not path.is_file():
            continue
        digest = hashlib.md5(path.read_bytes(), usedforsecurity=False).hexdigest()
        lines.append(f"{digest}  {relative}\n")
    return "".join(lines)


def _source_date_epoch() -> str:
    """Return `SOURCE_DATE_EPOCH`, or the last commit's time when it is not set."""
    value = os.environ.get("SOURCE_DATE_EPOCH")
    if value:
        return value
    result = subprocess.run(
        ["git", "-C", str(REPO), "log", "-1", "--format=%ct"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise BuildError(1, "git log cannot read the last commit's time")
    return result.stdout.strip()


def build(out: Path) -> Path:
    """Build the package.

    Args:
        out: The folder the `.deb` is written to.

    Returns:
        The path of the `.deb`.
    """
    _check_tools()
    version = read_version((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    for required in ("skill/SKILL.md", ARCHIVE_KEY):
        if not (REPO / required).is_file():
            raise BuildError(66, f"{REPO / required} is missing")
    work = REPO / "build/deb"
    if work.exists():
        shutil.rmtree(work)
    stage = work / "root"
    stage.mkdir(parents=True)
    wheels = work / "wheel"
    _run(["uv", "build", "--wheel", "--out-dir", wheels], cwd=REPO)
    venv = stage / PREFIX
    python = venv / "bin/python"
    requirements = work / "requirements.txt"
    _run(["uv", "venv", "--python", PYTHON, venv], cwd=REPO)
    _run(
        [
            "uv", "export", "--frozen", "--no-dev", "--no-emit-project",
            "--format", "requirements.txt", "-o", requirements,
        ],
        cwd=REPO,
    )  # fmt: skip
    install = ["uv", "pip", "install", "--python", python, "--link-mode", "copy"]
    _run(
        [
            *install, "--no-deps", "--require-hashes", "--only-binary", ":all:",
            "-r", requirements,
        ],
        cwd=REPO,
    )  # fmt: skip
    wheel = wheels / f"ember_app-{version}-py3-none-any.whl"
    _run([*install, "--no-deps", wheel], cwd=REPO)
    _clean_environment(stage)
    _run(
        [
            PYTHON, "-m", "compileall", "-q", "-j", "0",
            "--invalidation-mode", "unchecked-hash", "-x", r"\.tmpl\.py$",
            "-s", stage, "-p", "/", stage / SITE,
        ]
    )  # fmt: skip
    _write_files(stage)
    _check_libraries(stage)
    _check_paths(stage)
    _set_modes(stage)
    debian = stage / "DEBIAN"
    debian.mkdir(mode=0o755)
    md5sums = _md5sums(stage)
    _install(debian / "control", control(version, _installed_kb(stage)).encode())
    _install(debian / "conffiles", f"/{APT_SOURCE}\n".encode())
    _install(debian / "md5sums", md5sums.encode())
    out.mkdir(parents=True, exist_ok=True)
    package = out / f"ember_{version}_amd64.deb"
    env = {**os.environ, "SOURCE_DATE_EPOCH": _source_date_epoch()}
    _run(["dpkg-deb", "--root-owner-group", "-Zxz", "--build", stage, package], env=env)
    return package


def main(argv: Sequence[str] | None = None) -> int:
    """Build the package and print its path and size.

    Args:
        argv: The arguments; `None` reads `sys.argv`.

    Returns:
        The exit code.
    """
    parser = argparse.ArgumentParser(
        prog="build_deb.py", description="Build Ember's .deb package."
    )
    parser.add_argument(
        "--out", type=Path, default=Path("dist"), help="output folder (default: dist)"
    )
    parser.add_argument(
        "--depends",
        action="store_true",
        help="print the packages in Depends, one per line, and build nothing",
    )
    try:
        args = parser.parse_args(argv)
    except SystemExit as error:
        return 0 if error.code == 0 else 64
    if args.depends:
        print("\n".join(DEPENDS))
        return 0
    try:
        package = build(args.out.resolve())
    except BuildError as error:
        print(f"build_deb: {error}", file=sys.stderr)
        return error.code
    print(f"{package} {package.stat().st_size} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
