"""Provide private test homes and generated eval fixtures."""

import os
import pwd
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from ember import paths


@pytest.fixture(autouse=True)
def private_run_tmp_root(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep run temporary folders inside each test's private state."""
    folder = tmp_path_factory.mktemp("run-tmp")
    monkeypatch.setattr(paths, "RUN_TMP_ROOT", folder)


@pytest.fixture(autouse=True)
def private_password_home(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Give the current user a private fallback home when HOME is omitted."""
    lookup: Callable[[int], pwd.struct_passwd] = pwd.getpwuid
    owner = os.getuid()
    folder = tmp_path_factory.mktemp("password-home")

    def getpwuid(uid: int) -> pwd.struct_passwd:
        entry = lookup(uid)
        if uid != owner:
            return entry
        values = list(entry)
        values[5] = str(folder)
        return pwd.struct_passwd(values)

    monkeypatch.setattr(pwd, "getpwuid", getpwuid)


@pytest.fixture(scope="session")
def generated_eval_fixtures(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Generate fixtures once in each pytest worker's private temporary folder."""
    top = Path(__file__).resolve().parents[1]
    folder = tmp_path_factory.mktemp("eval-fixtures")
    result = subprocess.run(
        ["bash", "skill/evals/_stubs/generate.sh", "all", str(folder)],
        cwd=top,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return folder
