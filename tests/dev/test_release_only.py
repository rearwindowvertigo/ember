"""Check which pull requests the check workflow treats as release-only."""

import importlib.util
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "ember_dev_release_only",
    Path(__file__).resolve().parents[2] / "dev/release_only.py",
)
assert _spec is not None and _spec.loader is not None
release_only = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(release_only)

PYPROJECT = '[project]\nname = "ember-app"\nversion = "{v}"\ndependencies = []\n'
LOCK = (
    'version = 1\n\n[[package]]\nname = "ember-app"\nversion = "{v}"\n\n'
    '[[package]]\nname = "other"\nversion = "1.0.0"\n'
)
SKILL = "# Ember\n\nSkill version: {v} <!-- x-release-please-version -->\n\nText.\n"


def _files(version: str) -> dict[str, str]:
    return {
        "pyproject.toml": PYPROJECT.format(v=version),
        "uv.lock": LOCK.format(v=version),
        "skill/SKILL.md": SKILL.format(v=version),
        "CHANGELOG.md": f"# Changelog\n\n## {version}\n",
        ".release-please-manifest.json": f'{{".": "{version}"}}\n',
    }


def _decide(base: dict[str, str], head: dict[str, str]) -> tuple[bool, str]:
    changed = sorted(p for p in set(base) | set(head) if base.get(p) != head.get(p))
    sides = {"base": base, "head": head}
    return release_only.decide(changed, lambda side, path: sides[side][path])


def test_release_files_only_is_release_only() -> None:
    release, reason = _decide(_files("0.15.0"), _files("0.16.0"))
    assert release, reason


def test_changelog_only_with_unchanged_version_is_not() -> None:
    head = _files("0.15.0")
    head["CHANGELOG.md"] += "more\n"
    assert _decide(_files("0.15.0"), head) == (
        False,
        "the project version is unchanged",
    )


def test_other_file_runs_the_check() -> None:
    head = _files("0.16.0")
    head["src/ember/cli.py"] = "x = 1\n"
    base = {**_files("0.15.0"), "src/ember/cli.py": "x = 0\n"}
    assert _decide(base, head) == (False, "changes src/ember/cli.py")


@pytest.mark.parametrize(
    ("path", "edit"),
    [
        (
            "pyproject.toml",
            lambda t: t.replace("dependencies = []", 'dependencies = ["a"]'),
        ),
        ("uv.lock", lambda t: t.replace('version = "1.0.0"', 'version = "1.0.1"')),
        ("skill/SKILL.md", lambda t: t.replace("Text.", "Other text.")),
    ],
)
def test_version_file_with_another_change_runs_the_check(
    path: str, edit: Callable[[str], str]
) -> None:
    head = _files("0.16.0")
    head[path] = edit(head[path])
    assert _decide(_files("0.15.0"), head) == (
        False,
        f"{path} changes more than the version",
    )


def test_empty_change_runs_the_check() -> None:
    assert _decide(_files("0.15.0"), _files("0.15.0")) == (False, "no changed files")


def test_main_reads_git_and_writes_the_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

    def write(files: dict[str, str]) -> None:
        for name, text in files.items():
            path = repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")

    git("init", "-q", "-b", "main")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test")
    write(_files("0.15.0"))
    git("add", "-A")
    git("commit", "-q", "-m", "base")
    base = git("rev-parse", "HEAD")
    write(_files("0.16.0"))
    git("commit", "-q", "-am", "release")
    output = tmp_path / "output"
    monkeypatch.chdir(repo)
    assert release_only.main([base, "HEAD", "--github-output", str(output)]) == 0
    assert output.read_text(encoding="utf-8") == "release_only=true\n"
