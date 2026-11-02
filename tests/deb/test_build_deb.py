import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "packaging" / "deb" / "build_deb.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("build_deb", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("build_deb", module)
    spec.loader.exec_module(module)
    return module


build_deb = _load()


# read_version


@pytest.mark.parametrize("version", ["0.0.0", "0.1.0", "12.34.567"])
def test_read_version_accepts_x_y_z(version: str) -> None:
    text = f'[project]\nname = "ember-app"\nversion = "{version}"\n'
    assert build_deb.read_version(text) == version


@pytest.mark.parametrize(
    "text",
    [
        '[project]\nversion = "0.1"\n',
        '[project]\nversion = "0.1.0rc1"\n',
        '[project]\nversion = "v0.1.0"\n',
        '[project]\nversion = " 0.1.0"\n',
        '[project]\nname = "ember-app"\n',
        '[tool]\nversion = "0.1.0"\n',
        "[project\n",
    ],
)
def test_read_version_refuses_other_versions(text: str) -> None:
    with pytest.raises(build_deb.BuildError) as caught:
        build_deb.read_version(text)
    assert caught.value.code == 65


# control


def _fields(text: str) -> dict[str, str]:
    fields = {}
    for line in text.splitlines():
        if line.startswith(" "):
            continue
        key, _, value = line.partition(": ")
        fields[key] = value
    return fields


def test_control_has_every_field() -> None:
    text = build_deb.control("0.1.0", 1234)
    fields = _fields(text)
    assert fields["Package"] == "ember"
    assert fields["Version"] == "0.1.0"
    assert fields["Architecture"] == "amd64"
    assert fields["Maintainer"] == (
        "rearwindowvertigo <254349504+rearwindowvertigo@users.noreply.github.com>"
    )
    assert fields["Installed-Size"] == "1234"
    assert fields["Section"] == "devel"
    assert fields["Priority"] == "optional"
    assert fields["Homepage"] == "https://github.com/rearwindowvertigo/ember"
    assert fields["Recommends"] == "bubblewrap, xdg-terminal-exec"
    assert fields["Description"] == (
        "Lets Claude lead AI coding workers on one Linux machine"
    )
    depends = fields["Depends"].split(", ")
    assert depends[:3] == ["python3.14", "git", "tmux"]
    assert {"libegl1", "libxcb-cursor0", "libxkbcommon0"} <= set(depends)
    assert text.endswith("\n")
    long_lines = [line for line in text.splitlines() if line.startswith(" ")]
    assert long_lines
    assert all(line.strip() for line in long_lines)


# wrapper and desktop entry


def test_wrapper_runs_the_package_interpreter_isolated() -> None:
    assert build_deb.wrapper() == (
        '#!/bin/sh\nexec /usr/lib/ember/bin/python -I -m ember.cli "$@"\n'
    )


def test_desktop_entry_keys() -> None:
    lines = build_deb.desktop_entry().splitlines()
    assert lines[0] == "[Desktop Entry]"
    entries = dict(line.split("=", 1) for line in lines[1:] if line)
    assert entries == {
        "Type": "Application",
        "Name": "Ember",
        "Comment": "Lets Claude lead AI coding workers on this machine",
        "Exec": "emb app",
        "Icon": "io.github.rearwindowvertigo.Ember",
        "Terminal": "false",
        "Categories": "Development;",
        "StartupNotify": "true",
        "StartupWMClass": "io.github.rearwindowvertigo.Ember",
    }


def test_desktop_entry_validates(tmp_path: Path) -> None:
    tool = shutil.which("desktop-file-validate")
    if tool is None:
        pytest.skip("desktop-file-validate is not installed")
    entry = tmp_path / "io.github.rearwindowvertigo.Ember.desktop"
    entry.write_text(build_deb.desktop_entry())
    result = subprocess.run(
        [tool, str(entry)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == ""


# skill_files


def test_skill_files_leaves_out_evals_and_caches(tmp_path: Path) -> None:
    skill = tmp_path / "skill"
    for name in [
        "SKILL.md",
        "references/runs.md",
        "assets/task-brief.md",
        "scripts/verify_branch.py",
        "scripts/__pycache__/verify_branch.cpython-314.pyc",
        "scripts/stray.pyc",
        "evals/README.md",
        "evals/case/prompt.md",
    ]:
        path = skill / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x")
    (skill / "references" / "notes").mkdir()
    assert build_deb.skill_files(skill) == [
        Path("SKILL.md"),
        Path("assets/task-brief.md"),
        Path("references/runs.md"),
        Path("scripts/verify_branch.py"),
    ]


def test_skill_files_of_a_missing_folder_is_empty(tmp_path: Path) -> None:
    assert build_deb.skill_files(tmp_path / "skill") == []


# Checks


def test_files_holding_finds_a_planted_staging_path(tmp_path: Path) -> None:
    stage = tmp_path / "build" / "deb" / "root"
    (stage / "usr/lib/ember").mkdir(parents=True)
    (stage / "usr/lib/ember/clean.txt").write_text("home = /usr/bin\n")
    planted = stage / "usr/lib/ember/pyvenv.cfg"
    planted.write_bytes(b"prefix = " + str(stage).encode() + b"/usr/lib/ember\n")
    (stage / "usr/lib/ember/link").symlink_to(planted)
    assert build_deb.files_holding(stage, str(stage).encode()) == [planted]


def test_check_paths_refuses_a_planted_staging_path(tmp_path: Path) -> None:
    stage = tmp_path / "root"
    (stage / "usr/bin").mkdir(parents=True)
    (stage / "usr/bin/emb").write_text(f"#!/bin/sh\nexec {stage}/python\n")
    with pytest.raises(build_deb.BuildError) as caught:
        build_deb._check_paths(stage)
    assert caught.value.code == 70
    assert "usr/bin/emb" in str(caught.value)


def test_needed_reads_objdump_output() -> None:
    output = (
        "Dynamic Section:\n"
        "  NEEDED               libQt6Core.so.6\n"
        "  NEEDED               libc.so.6\n"
        "  SONAME               libQt6Gui.so.6\n"
    )
    assert build_deb.needed(output) == ["libQt6Core.so.6", "libc.so.6"]


def test_owners_reads_dpkg_output() -> None:
    output = (
        "libc6:amd64: /usr/lib/x86_64-linux-gnu/libc.so.6\n"
        "libc6-i386, libc6-x32:amd64: /usr/lib32/libc.so.6\n"
        "diversion by foo from: /usr/lib/libc.so.6\n"
    )
    assert build_deb.owners(output) == {"libc6", "libc6-i386", "libc6-x32"}


@pytest.mark.parametrize(
    ("folder", "libraries", "skipped"),
    [
        ("platformthemes", ["libQt6Gui.so.6", "libgtk-3.so.0"], True),
        ("platforms", ["libQt6Gui.so.6", "libgtk-3.so.0"], False),
        ("imageformats", ["libQt6Pdf.so.6", "libQt6Gui.so.6"], True),
        ("imageformats", ["libQt6Gui.so.6", "libc.so.6"], False),
    ],
)
def test_skip_plugin(folder: str, libraries: list[str], skipped: bool) -> None:
    bundled = {"libQt6Gui.so.6", "libQt6Core.so.6"}
    assert build_deb.skip_plugin(folder, libraries, bundled) is skipped


# Command line


def test_depends_option_prints_the_packages(capsys: pytest.CaptureFixture[str]) -> None:
    assert build_deb.main(["--depends"]) == 0
    assert capsys.readouterr().out.splitlines() == list(build_deb.DEPENDS)


def test_usage_error_exits_64(capsys: pytest.CaptureFixture[str]) -> None:
    assert build_deb.main(["--unknown"]) == 64
    assert "--unknown" in capsys.readouterr().err


def test_missing_tool_exits_69(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(build_deb, "PYTHON", tmp_path / "python3.14")
    assert build_deb.main(["--out", str(tmp_path / "dist")]) == 69
    assert "python3.14 is missing" in capsys.readouterr().err
    assert not (tmp_path / "dist").exists()


def test_missing_command_exits_69(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    python = tmp_path / "python3.14"
    python.write_text("")
    monkeypatch.setattr(build_deb, "PYTHON", python)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert build_deb.main(["--out", str(tmp_path / "dist")]) == 69
    assert "uv is missing" in capsys.readouterr().err
