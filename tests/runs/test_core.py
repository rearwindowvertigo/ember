import hashlib
import json
import os
import shlex
import stat
import subprocess
import sys
import threading
import time
import tomllib
from collections.abc import Callable, Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from ember import choices, engines, paths, processes, prune, sandbox
from ember.engines import Driver, LaunchSpec, models, opencode2, status
from ember.engines.codex import CodexDriver, codex_home
from ember.engines.opencode import OpenCodeDriver
from ember.engines.opencode2 import DeliveryUncertainError, OpenCode2Driver
from ember.runs import core, exits, record
from ember.runs.core import Core, RunError, StartOptions
from engines.test_opencode2 import STUB_SERVER  # pyrefly: ignore[missing-import]

from .stub_engine import (  # pyrefly: ignore[missing-import]
    StubDriver,
    engine_cache,
    git,
    kill_sessions,
    make_env,
    make_repo,
    script_path,
    session_folder,
)


@pytest.fixture
def env(tmp_path: Path) -> Iterator[dict[str, str]]:
    environment = make_env(tmp_path)
    yield environment
    kill_sessions(tmp_path)


@pytest.fixture
def repo(tmp_path: Path, env: dict[str, str]) -> Path:
    return make_repo(tmp_path, env)


def _driver(
    env: dict[str, str],
    name: str = "claude",
    **options: Any,  # noqa: ANN401
) -> StubDriver:
    return StubDriver(name, script_path(env), **options)


def _core(
    env: dict[str, str],
    driver: Driver | None = None,
    *,
    stall: float = 0.1,
    quiet: bool = False,
    admit: Callable[[record.Run], None] | None = None,
    prepare_stop: threading.Event | None = None,
) -> Core:
    chosen = driver or _driver(env)
    return Core(
        env,
        load_driver=lambda name: chosen,
        poll_seconds=0.02,
        stall_minutes=stall,
        quiet=quiet,
        admit=admit,
        prepare_stop=prepare_stop,
    )


def _brief(tmp_path: Path, text: str, name: str = "brief.md") -> str:
    path = tmp_path / name
    path.write_text(text)
    return str(path)


def _start(
    core_: Core,
    repo: Path,
    brief: str,
    **options: Any,  # noqa: ANN401
) -> int:
    options.setdefault("engine", "claude")
    options.setdefault("window", False)
    return core_.execute(
        lambda: core_.start(StartOptions(dir=str(repo), task_file=brief, **options))
    )


def _run_id(out: str) -> str:
    first = out.splitlines()[0]
    assert first.startswith("run ")
    return first.removeprefix("run ")


def _fields(out: str) -> dict[str, str]:
    result = {}
    for line in out.split(core.REPLY_MARK)[0].splitlines()[1:]:
        name, _, value = line.partition(" ")
        result[name] = value
    return result


def _events(tmp_path: Path, run_id: str) -> list[dict[str, str]]:
    text = (session_folder(tmp_path, run_id) / "input.jsonl").read_text()
    return [json.loads(line) for line in text.splitlines()]


# Starting an editing run


@pytest.mark.parametrize("name", [record.BRIEF_NAME, record.RECORD_NAME])
@pytest.mark.parametrize("read_only", [False, True])
def test_start_publishes_brief_and_record_together(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    name: str,
    read_only: bool,
) -> None:
    original = record.write_private
    written = threading.Event()
    release = threading.Event()
    brief = "Task\n\nKeep this brief unchanged.\n"
    task_file = _brief(tmp_path, brief)

    def write(path: Path, text: str) -> None:
        original(path, text)
        if path.parent.name.startswith(paths.CREATION_TMP_PREFIX) and path.name == name:
            written.set()
            assert release.wait(10)

    monkeypatch.setattr(record, "write_private", write)
    core_ = _core(env)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(_start, core_, repo, task_file, read_only=read_only)
        try:
            assert written.wait(10)
            assert record.list_runs(env) == ([], [])
            assert prune.select(0, env=env).records == {}
            result = prune.remove(0, env=env)
            assert not result.errors and not result.removed
            (temporary,) = paths.runs_dir(env).iterdir()
            assert temporary.name.startswith(paths.CREATION_TMP_PREFIX)
            assert stat.S_IMODE(temporary.stat().st_mode) == 0o700
            assert (temporary / record.BRIEF_NAME).read_text() == brief
            names = {record.BRIEF_NAME}
            if name == record.RECORD_NAME:
                names.add(record.RECORD_NAME)
                saved = (temporary / name).read_text()
                data = json.loads(saved)
                initial = record.from_json(data, record.run_dir(data["id"], env), env)
                assert saved == json.dumps(initial.to_json(), indent=2) + "\n"
                assert not initial.folder.exists()
            assert {item.name for item in temporary.iterdir()} == names
            assert all(
                stat.S_IMODE(item.stat().st_mode) == 0o600
                for item in temporary.iterdir()
            )
        finally:
            release.set()
        code = pending.result(timeout=10)
    assert code == 0, capsys.readouterr().err
    (run,), errors = record.list_runs(env)
    assert not errors and run.folder == record.run_dir(run.id, env)
    assert (run.folder / record.BRIEF_NAME).read_text() == brief
    assert list(paths.runs_dir(env).iterdir()) == [run.folder]


@pytest.mark.parametrize("name", [record.BRIEF_NAME, record.RECORD_NAME])
@pytest.mark.parametrize("error_type", [OSError, KeyboardInterrupt])
@pytest.mark.parametrize("engine", ["claude", "codex"])
def test_start_creation_failure_removes_unpublished_folder(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    name: str,
    error_type: type[BaseException],
    engine: str,
) -> None:
    original = record.write_private
    if engine == "codex":
        choices.set_settings({"sandbox.codex": "on"}, env=env)
        monkeypatch.setattr(sandbox, "require", lambda engine, env=None: None)

    def write(path: Path, text: str) -> None:
        original(path, text)
        if path.parent.name.startswith(paths.CREATION_TMP_PREFIX) and path.name == name:
            raise error_type("creation failed")

    monkeypatch.setattr(record, "write_private", write)
    driver = _driver(env, engine)
    core_ = _core(env, driver)
    code = _start(
        core_, repo, _brief(tmp_path, "Task\n"), engine=engine,
        model="gpt-5" if engine == "codex" else "opus",
    )  # fmt: skip
    assert code == (exits.INTERRUPTED if error_type is KeyboardInterrupt else 1)
    err = capsys.readouterr().err
    assert ("interrupted" if code == exits.INTERRUPTED else "creation failed") in err
    assert core_.last_run_id is None
    assert not driver.commands
    assert list(paths.runs_dir(env).iterdir()) == []
    assert record.list_runs(env) == ([], [])
    assert not paths.worktrees_dir(env).exists()
    assert not list(paths.RUN_TMP_ROOT.iterdir())


@pytest.mark.parametrize("populated", [False, True])
def test_start_refuses_existing_record_folder(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    populated: bool,
) -> None:
    ident = "claude-20260101-000000-aabbcc"
    monkeypatch.setattr(record, "new_id", lambda engine: ident)
    directory = record.make_run_dir(ident, env)
    if populated:
        (directory / record.RECORD_NAME).write_text("keep")
    driver = _driver(env)
    assert _start(_core(env, driver), repo, _brief(tmp_path, "Task\n")) == 1
    assert "already exists" in capsys.readouterr().err
    assert list(paths.runs_dir(env).iterdir()) == [directory]
    if populated:
        assert (directory / record.RECORD_NAME).read_text() == "keep"
    else:
        assert list(directory.iterdir()) == []
    assert not driver.commands


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("read_only", [False, True])
@pytest.mark.parametrize("linked", [False, True])
def test_codex_trust_refusal_precedes_launch_setup(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    dry_run: bool,
    read_only: bool,
    linked: bool,
) -> None:
    (codex_home(env) / "config.toml").unlink()
    checkout = repo
    if linked:
        checkout = tmp_path / "linked"
        git(repo, "worktree", "add", "-q", "--detach", str(checkout), env=env)
        # Trusting the source checkout does not trust a new worker worktree.
        (codex_home(env) / "config.toml").write_text(
            f'[projects.{json.dumps(str(checkout))}]\ntrust_level = "trusted"\n'
        )
    folder = checkout / "subfolder"
    folder.mkdir()
    brief = _brief(tmp_path, "Task\n")
    branches = git(repo, "branch", "--list", env=env)
    worktrees = git(repo, "worktree", "list", "--porcelain", env=env)
    before = set(tmp_path.rglob("*"))

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("untrusted repository reached launch setup")

    monkeypatch.setattr(record, "new_id", forbidden)
    monkeypatch.setattr(sandbox, "require", forbidden)
    monkeypatch.setattr(paths, "ensure_worker_cache_dir", forbidden)
    assert _start(
        _core(env, CodexDriver(env)), folder, brief,
        engine="codex", model="gpt-5", effort="high",
        dry_run=dry_run, read_only=read_only,
    ) == 69  # fmt: skip
    out, err = capsys.readouterr()
    assert out == ""
    assert err.splitlines() == [
        f"emb run: Codex does not trust {repo}; the owner trusts it by running "
        "codex there once and choosing to trust the folder",
        f"owner: run codex in {repo} once and choose to trust the folder",
    ]
    assert "next:" not in err
    assert not paths.runs_dir(env).exists()
    assert not paths.worktrees_dir(env).exists()
    assert git(repo, "branch", "--list", env=env) == branches
    assert git(repo, "worktree", "list", "--porcelain", env=env) == worktrees
    assert set(tmp_path.rglob("*")) - before <= {
        paths.settings_db_path(env).with_suffix(".db-wal"),
        paths.settings_db_path(env).with_suffix(".db-shm"),
    }


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("read_only", [False, True])
def test_codex_repository_trust_allows_a_linked_checkout(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    capsys: pytest.CaptureFixture[str],
    dry_run: bool,
    read_only: bool,
) -> None:
    linked = tmp_path / "linked"
    git(repo, "worktree", "add", "-q", "--detach", str(linked), env=env)
    assert _start(
        _core(env, _driver(env, "codex")), linked, _brief(tmp_path, "Task\n"),
        engine="codex", model="gpt-5", effort="high",
        dry_run=dry_run, read_only=read_only,
    ) == 0, capsys.readouterr().err  # fmt: skip


@pytest.mark.parametrize("read_only", [False, True])
def test_dry_run_resolves_team_without_creating_launch_artifacts(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    read_only: bool,
) -> None:
    choices.set_settings(
        {
            "shortlist.codex": ["gpt-6.1-sol@high"],
            "single.writer": "codex:gpt-6.1-sol",
            "single.reviewer": "codex:gpt-6.1-sol",
            "sandbox.codex": "on",
        },
        env=env,
    )
    brief = _brief(tmp_path, "Task\n")
    branches = git(repo, "branch", "--list", env=env)
    worktrees = git(repo, "worktree", "list", "--porcelain", env=env)
    before = set(tmp_path.rglob("*"))

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("dry run reached launch setup")

    monkeypatch.setattr(sandbox, "require", forbidden)
    monkeypatch.setattr(paths, "ensure_worker_cache_dir", forbidden)
    monkeypatch.setattr(record, "new_id", forbidden)
    driver = _driver(env, "codex")
    assert (
        _start(
            _core(env, driver), repo, brief,
            engine="codex", read_only=read_only, dry_run=True, window=True,
        )
        == 0
    )  # fmt: skip
    out, err = capsys.readouterr()
    role = "Reviewer" if read_only else "Writer"
    source = "single.reviewer" if read_only else "single.writer"
    assert out == f"Team, Single tasks {role}: codex gpt-6.1-sol · high ({source})\n"
    assert err == ""
    assert driver.commands == []
    # Queries may create SQLite sidecars and the repository worktree lock.
    lock_root = paths.runtime_dir(env)
    key = hashlib.sha256(os.fsencode((repo / ".git").resolve())).hexdigest()
    created = set(tmp_path.rglob("*")) - before
    assert created <= {
        paths.settings_db_path(env).with_suffix(".db-wal"),
        paths.settings_db_path(env).with_suffix(".db-shm"),
        lock_root.parent,
        lock_root,
        lock_root / "locks",
        lock_root / "locks" / f"worktrees-{key}.lock",
    }
    assert git(repo, "branch", "--list", env=env) == branches
    assert git(repo, "worktree", "list", "--porcelain", env=env) == worktrees


@pytest.mark.parametrize("connected", [False, True])
def test_dry_run_checks_engine_without_writing_a_missing_cache(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    connected: bool,
) -> None:
    engine_cache(env, {"claude": connected})
    held = status.cached(env)
    assert held is not None
    env["XDG_CACHE_HOME"] = str(tmp_path / "absent-cache")
    checked = []

    def check(engine: str, env: object) -> status.EngineStatus:
        checked.append(engine)
        return held[engine]

    monkeypatch.setattr(status, "check", check)
    assert _start(_core(env), repo, _brief(tmp_path, "Task\n"), dry_run=True) == (
        0 if connected else 69
    )
    out, err = capsys.readouterr()
    assert checked == ["claude"]
    assert not Path(env["XDG_CACHE_HOME"]).exists()
    assert not paths.runs_dir(env).exists()
    if connected:
        assert "claude opus · high" in out
        assert err == ""
    else:
        assert out == ""
        assert "claude is not connected" in err


@pytest.mark.parametrize("driver_type", [CodexDriver, OpenCodeDriver, OpenCode2Driver])
@pytest.mark.parametrize("expired_cache", [False, True])
@pytest.mark.parametrize("selection", ["saved", "default", "invalid"])
def test_dry_run_real_drivers_never_write_model_caches(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    driver_type: type[CodexDriver] | type[OpenCodeDriver],
    expired_cache: bool,
    selection: str,
) -> None:
    engine = driver_type.name
    model = "gpt-6.1-sol" if engine == "codex" else "openai/gpt-6.1-sol"
    choices.set_settings(
        {
            f"shortlist.{engine}": [f"{model}@high"],
            "single.writer": f"{engine}:{model}" if selection == "saved" else "ask",
        },
        env=env,
    )
    held = status.cached(env)
    assert held is not None
    env["XDG_CACHE_HOME"] = str(tmp_path / "model-cache")
    monkeypatch.setattr(status, "check", lambda engine, env: held[engine])
    discovered = []

    def codex_reply(argv: list[str], env: Mapping[str, str]) -> tuple[int, str, str]:
        assert argv[1:] == ["debug", "models"]
        discovered.append("codex")
        return 0, json.dumps({"models": [{
            "slug": model,
            "visibility": "list",
            "supported_reasoning_levels": [{"effort": "high"}, {"effort": "xhigh"}],
            "default_reasoning_level": "high",
        }]}), ""  # fmt: skip

    def unexpected_fetch(url: str, timeout: float) -> object:
        pytest.fail("OpenCode default effort does not discover models")

    monkeypatch.setattr(status, "locate", lambda engine, env: f"/mock/{engine}")
    monkeypatch.setattr(models, "_run_models", codex_reply)
    monkeypatch.setattr(models, "fetch_json", unexpected_fetch)
    cache = models.cache_path(engine, env)
    if expired_cache:
        status.save_cache_file(
            cache,
            {
                "fetched_at": 0,
                "models": [models.Model(model, model, ("low",), "low").to_json()],
            },
            env,
        )
    before = (cache.read_bytes(), cache.stat().st_mtime_ns) if expired_cache else None

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("dry run attempted a cache write")

    monkeypatch.setattr(status, "save_cache_file", forbidden)
    options: dict[str, object] = {} if selection == "saved" else {"model": model}
    if selection == "invalid":
        options["effort"] = "low" if engine == "codex" else "High"
    code = _start(
        _core(env, driver_type(env)), repo, _brief(tmp_path, "Task\n"),
        engine=engine, dry_run=True, **options,
    )  # fmt: skip
    out, err = capsys.readouterr()
    if selection == "invalid":
        assert code == 64
        assert out == ""
        assert "does not take the effort" in err
    else:
        assert code == 0, err
        effort = (
            "default" if engine == "opencode" and selection == "default" else "high"
        )
        assert f"{engine} {model} · {effort}" in out
        assert err == ""
    assert bool(discovered) == (engine == "codex")
    if before is None:
        assert not Path(env["XDG_CACHE_HOME"]).exists()
    else:
        assert (cache.read_bytes(), cache.stat().st_mtime_ns) == before
        assert set(cache.parent.iterdir()) == {cache}
    assert not paths.runs_dir(env).exists()
    assert not paths.worktrees_dir(env).exists()


def test_start_announces_resolved_worker_before_driver_command(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    expected = (
        "Team, Single tasks Writer: claude opus · low "
        "(model: single.writer, effort: --effort)\n"
    )
    brief = _brief(tmp_path, "Task\n")
    assert _start(_core(env), repo, brief, effort="low", dry_run=True) == 0
    assert capsys.readouterr().out == expected
    driver = _driver(env)
    command = driver.command
    observed = []

    def observe(run: record.Run, message: str) -> LaunchSpec:
        out, err = capsys.readouterr()
        assert out == f"run {run.id}\n"
        assert err.startswith(expected)
        observed.append(run.id)
        return command(run, message)

    monkeypatch.setattr(driver, "command", observe)
    assert _start(_core(env, driver, quiet=True), repo, brief, effort="low") == 0
    assert len(observed) == 1


@pytest.mark.parametrize("default", [None, "medium"])
@pytest.mark.parametrize("explicit", [False, True])
def test_dry_run_reports_driver_default_and_ignores_subagent_row(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    default: str | None,
    explicit: bool,
) -> None:
    engine = "claude" if explicit else "opencode"
    model = "opus" if explicit else "openrouter/model"
    choices.set_settings(
        {
            "shortlist.opencode": ["openrouter/model"],
            "single.writer": f"{engine}:{model}",
            "single.writer_as": "subagent" if explicit else "cli",
        },
        env=env,
    )
    driver = _driver(env, engine)
    monkeypatch.setattr(driver, "default_effort", lambda model: default)
    brief = _brief(tmp_path, "Task\n")
    options = {"model": model} if explicit else {}
    assert (
        _start(
            _core(env, driver), repo, brief,
            engine=engine, dry_run=True, as_json=True, **options,
        )
        == 0
    )  # fmt: skip
    out, err = capsys.readouterr()
    source = "--model" if explicit else "single.writer"
    data = json.loads(out)
    assert (data["model_source"], data["effort_source"]) == (source, source)
    assert data["effort"] == default
    assert err == ""
    assert (
        _start(_core(env, driver), repo, brief, engine=engine, dry_run=True, **options)
        == 0
    )
    assert capsys.readouterr().out == (
        f"Team, Single tasks Writer: {engine} {model} · "
        f"{default or 'default'} ({source})\n"
    )


@pytest.mark.parametrize("read_only", [False, True])
def test_start_from_linked_checkout_records_main_and_common(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    capsys: pytest.CaptureFixture[str],
    read_only: bool,
) -> None:
    linked = tmp_path / "linked"
    git(repo, "worktree", "add", "-q", "-b", "linked", str(linked), env=env)
    assert (
        _start(
            _core(env),
            linked,
            _brief(tmp_path, "Review\n@reply fine\n"),
            read_only=read_only,
        )
        == 0
    )
    run = record.load(_run_id(capsys.readouterr().out), env)
    assert run.repo == str(linked)
    assert run.main == str(repo)
    assert run.common == str(repo / ".git")


def test_editing_run_commits_its_turn(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    brief = _brief(
        tmp_path, "Add a greeting\n\n@write hello.txt hi there\n@reply all done\n"
    )
    driver = _driver(env)
    code = _start(
        _core(env, driver), repo, brief, trailers=("Co-Authored-By: X <x@y>",)
    )
    out, err = capsys.readouterr()
    assert code == 0, err
    run_id = _run_id(out)
    assert run_id.startswith("claude-")
    lines = out.splitlines()
    assert lines[1].startswith("claude opus · high · ")
    assert lines[1].endswith(" · exit 0 · 1 files · turn 1")
    run = record.load(run_id, env)
    worktree = paths.worktree_path(run_id, env)
    fields = _fields(out)
    assert fields["run"] == run_id
    assert fields["worktree"] == str(worktree)
    assert fields["branch"] == f"ember/claude/{run_id}"
    assert fields["commit"] == run.commits[0]
    assert fields["window"] == "-"
    assert fields["reply"] == str(run.folder / "turn-1.md")
    assert out.split(core.REPLY_MARK + "\n")[1] == "all done\n"
    assert run.status == record.WAITING
    assert run.turn == 1
    assert run.turn_state == record.COMMITTED
    assert run.repo == str(repo)
    assert run.main == str(repo)
    assert run.common == str(repo / ".git")
    assert run.worktree == str(worktree)
    assert run.base == git(repo, "rev-parse", "HEAD", env=env)
    assert run.base_branch == git(repo, "branch", "--show-current", env=env)
    assert run.expected_head == run.commits[0]
    assert run.sandbox == "none"
    assert run.network is False
    assert run.read_effective == ()
    assert run.subject == "Add a greeting"
    assert run.last_exit == 0
    assert (worktree / "hello.txt").read_text() == "hi there\n"
    message = git(worktree, "log", "-1", "--format=%B", env=env)
    assert message == "Add a greeting\n\nCo-Authored-By: X <x@y>"
    assert git(worktree, "rev-parse", "--abbrev-ref", "HEAD", env=env) == run.branch
    assert git(repo, "status", "--porcelain", env=env) == ""
    assert not (repo / ".ember").exists()
    common = Path(git(repo, "rev-parse", "--git-common-dir", env=env))
    exclude = repo / common / "info" / "exclude"
    assert not exclude.exists() or ".ember/" not in exclude.read_text()
    assert stat.S_IMODE(paths.worktrees_dir(env).stat().st_mode) == 0o700
    assert (run.folder / "brief.md").read_text() == Path(brief).read_text()
    assert (run.folder / "turn-1.md").read_text() == "all done"
    assert stat.S_IMODE(run.folder.stat().st_mode) == 0o700
    assert stat.S_IMODE((run.folder / "run.json").stat().st_mode) == 0o600
    pasted = "".join(item.get("text", "") for item in _events(tmp_path, run_id))
    assert pasted == core.first_message(run_id, Path(brief).read_text())
    assert pasted.startswith(
        f"Brief from Claude, the lead, for Ember run {run_id}.\n\n"
    )
    assert driver.commands[0][1] == pasted


def test_turn_without_changes_makes_no_commit(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = _start(_core(env), repo, _brief(tmp_path, "Look around\n"))
    out, _ = capsys.readouterr()
    assert code == 0
    assert " · exit 0 · 0 files · turn 1" in out
    run = record.load(_run_id(out), env)
    assert run.commits == ()
    assert run.turn_state == record.COMMITTED
    assert _fields(out)["commit"] == "-"


def test_quiet_prints_first_line_and_summary(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = _start(_core(env, quiet=True), repo, _brief(tmp_path, "Task\n"))
    out, _ = capsys.readouterr()
    assert code == 0
    lines = out.splitlines()
    assert len(lines) == 2
    assert lines[0].startswith("run claude-")
    assert lines[1].endswith("turn 1")


def test_long_subject_is_cut_and_later_turns_are_numbered(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    subject = "x" * 80
    brief = _brief(tmp_path, f"\n{subject}\n@write a.txt one\n")
    code = _start(_core(env), repo, brief)
    out, _ = capsys.readouterr()
    assert code == 0
    run_id = _run_id(out)
    run = record.load(run_id, env)
    log = git(Path(run.worktree), "log", "-1", "--format=%s", env=env)
    assert log == "x" * 72
    message = _brief(tmp_path, "@write b.txt two\n", "message.md")
    code = _core(env).execute(lambda: _core(env).send(run_id, message))
    out, err = capsys.readouterr()
    assert code == 0, err
    assert out.splitlines()[0].endswith(" · exit 0 · 1 files · turn 2")
    log = git(Path(run.worktree), "log", "-1", "--format=%s", env=env)
    assert log == "x" * 72 + " (turn 2)"
    run = record.load(run_id, env)
    assert len(run.commits) == 2
    assert run.turn == 2
    assert run.reply == str(run.folder / "turn-2.md")
    # The message goes in as written, with no line above it.
    texts = [item["text"] for item in _events(tmp_path, run_id) if "text" in item]
    assert texts[-1] == "@write b.txt two\n"


def test_effort_from_driver_when_menu_row_names_another_model(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = _start(_core(env), repo, _brief(tmp_path, "Task\n"), model="sonnet")
    out, _ = capsys.readouterr()
    assert code == 0
    run = record.load(_run_id(out), env)
    assert (run.model, run.effort) == ("sonnet", "medium")


def test_explicit_effort_wins(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = _start(_core(env), repo, _brief(tmp_path, "Task\n"), effort="low")
    out, _ = capsys.readouterr()
    assert code == 0
    run = record.load(_run_id(out), env)
    assert (run.model, run.effort) == ("opus", "low")


def test_brief_on_the_command_line_is_not_pasted(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    driver = _driver(env, paste=False)
    code = _start(_core(env, driver), repo, _brief(tmp_path, "Task\n@write c.txt c\n"))
    out, err = capsys.readouterr()
    assert code == 0, err
    run_id = _run_id(out)
    assert _events(tmp_path, run_id) == []
    assert " · 1 files · turn 1" in out


def test_window_opens_with_terminal_setting(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    choices.set_settings({"terminal": "true"}, env=env)
    code = _start(_core(env), repo, _brief(tmp_path, "Task\n"), window=True)
    out, _ = capsys.readouterr()
    assert code == 0
    assert _fields(out)["window"].isdigit()


def test_failed_window_warns_and_run_continues(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    choices.set_settings({"terminal": str(tmp_path / "no-terminal")}, env=env)
    code = _start(_core(env), repo, _brief(tmp_path, "Task\n"), window=True)
    out, err = capsys.readouterr()
    assert code == 0
    assert "WARNING: no window for claude-" in err
    assert _fields(out)["window"] == "-"


# Read-only runs


def test_read_only_run_works_in_a_detached_worktree(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (repo / "dirty.txt").write_text("before\n")
    code = _start(
        _core(env), repo, _brief(tmp_path, "Review\n@reply fine\n"), read_only=True
    )
    out, err = capsys.readouterr()
    assert code == 0, err
    run = record.load(_run_id(out), env)
    assert run.read_only
    assert run.worktree == str(paths.worktree_path(run.id, env))
    assert run.detached
    assert not (Path(run.worktree) / "dirty.txt").exists()
    assert (
        git(Path(run.worktree), "rev-parse", "--abbrev-ref", "HEAD", env=env) == "HEAD"
    )
    assert run.branch is None
    assert run.expected_head is None
    assert run.commits == ()
    assert run.turn_state == record.COLLECTED
    assert run.last_reason is None
    short_base = git(Path(run.worktree), "rev-parse", "--short", "HEAD", env=env)
    assert (
        f"WARNING: read-only run {run.id} does not see uncommitted dirty.txt "
        f"(reads {short_base})\n" in err
    )
    assert f"read-only run {run.id} changed" not in err
    assert not (repo / ".ember").exists()
    assert _fields(out)["branch"] == "-"


@pytest.mark.parametrize("saved_base", [False, True])
def test_read_only_warning_names_the_commit_used_when_source_head_moves(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    saved_base: bool,
) -> None:
    """Keep the warning and detached worktree on the same resolved commit."""
    base = git(repo, "rev-parse", "HEAD", env=env)
    git(repo, "config", "core.abbrev", "12", env=env)
    short_base = git(repo, "rev-parse", "--short", base, env=env)
    if saved_base:
        git(repo, "commit", "--allow-empty", "-m", "Later commit", env=env)
    (repo / "dirty.txt").write_text("Owner notes\n")
    core_ = _core(env)
    create_worktree = core_._create_worktree

    def move_head_then_create(run: record.Run, main: Path) -> None:
        """Move the source HEAD between the warning and worktree creation."""
        git(repo, "commit", "--allow-empty", "-m", "Moved before launch", env=env)
        create_worktree(run, main)

    monkeypatch.setattr(core_, "_create_worktree", move_head_then_create)
    assert _start(
        core_, repo, _brief(tmp_path, "Review\n@reply fine\n"),
        read_only=True, base=base if saved_base else None,
    ) == 0  # fmt: skip
    out, err = capsys.readouterr()
    run = record.load(_run_id(out), env)
    assert run.base == base
    assert git(repo, "rev-parse", "HEAD", env=env) != base
    assert git(Path(run.worktree), "rev-parse", "HEAD", env=env) == base
    assert (
        f"WARNING: read-only run {run.id} does not see uncommitted dirty.txt "
        f"(reads {short_base})\n"
    ) in err
    assert "WARNING:" not in out


@pytest.mark.parametrize("short_base", [None, ""])
def test_read_only_start_without_short_hash_uses_resolved_commit(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    short_base: str | None,
) -> None:
    """A missing short hash uses the resolved commit and lets the run finish."""
    base = git(repo, "rev-parse", "HEAD", env=env)
    (repo / "dirty.txt").write_text("Owner notes\n")
    core_ = _core(env)
    git_out = core_._git_out

    def without_short_hash(cwd: str | Path, *args: str) -> str | None:
        """Return no hash only for the warning's abbreviation lookup."""
        if args == ("rev-parse", "--short", base):
            return short_base
        return git_out(cwd, *args)

    monkeypatch.setattr(core_, "_git_out", without_short_hash)
    assert (
        _start(core_, repo, _brief(tmp_path, "Review\n@reply fine\n"), read_only=True)
        == 0
    )
    out, err = capsys.readouterr()
    run = record.load(_run_id(out), env)
    assert run.base == base
    assert run.turn_state == record.COLLECTED
    assert run.last_exit == 0
    assert (
        f"WARNING: read-only run {run.id} does not see uncommitted dirty.txt "
        f"(reads {base[:12]})\n"
    ) in err


def test_read_only_run_that_changes_files_is_reported(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    brief = _brief(tmp_path, "Review\n@write README.md changed\n@write new.txt n\n")
    core_ = _core(env)
    code = _start(core_, repo, brief, read_only=True)
    out, err = capsys.readouterr()
    assert code == 0
    run = record.load(_run_id(out), env)
    assert f"WARNING: read-only run {run.id} changed {run.worktree}/README.md\n" in err
    assert f"WARNING: read-only run {run.id} changed {run.worktree}/new.txt\n" in err
    assert (
        run.last_reason == f"changed {run.worktree}/README.md, {run.worktree}/new.txt"
    )
    assert (repo / "README.md").read_text() == "hello\n"
    assert git(repo, "log", "--format=%s", env=env) == "first"
    assert run.branch is None and run.commits == ()
    assert core_.close(run.id) == 0
    assert Path(run.worktree, "README.md").read_text() == "changed\n"
    assert Path(run.worktree, "new.txt").read_text() == "n\n"
    assert f"git worktree remove --force {run.worktree}" in capsys.readouterr().err


def test_read_only_run_can_build_ignored_files_without_changes_or_commits(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (repo / ".gitignore").write_text(".venv/\nbuild/\n")
    git(repo, "add", ".gitignore", env=env)
    git(repo, "commit", "-q", "-m", "build ignores", env=env)
    base = git(repo, "rev-parse", "HEAD", env=env)
    core_ = _core(env)
    brief = _brief(
        tmp_path, "Review\n@write .venv/pyvenv.cfg test\n@write build/result test\n"
    )
    assert _start(core_, repo, brief, read_only=True) == 0
    out, err = capsys.readouterr()
    saved = record.load(_run_id(out), env)
    assert saved.read_only and saved.detached
    assert saved.branch is None and saved.commits == ()
    assert saved.turn_state == record.COLLECTED and saved.last_reason is None
    assert git(Path(saved.worktree), "rev-parse", "HEAD", env=env) == base
    assert git(repo, "rev-parse", "HEAD", env=env) == base
    assert Path(saved.worktree, ".venv/pyvenv.cfg").read_text() == "test\n"
    assert not (repo / ".venv").exists() and not (repo / "build").exists()
    assert f"read-only run {saved.id} changed" not in err
    assert core_.close(saved.id) == 0
    assert not Path(saved.worktree).exists()
    assert not capsys.readouterr().err


# Turn failures


def test_stall_leaves_the_session_running_and_recover_collects(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    brief = _brief(tmp_path, "Slow\n@hang\n@write late.txt late\n@reply late\n")
    code = _start(_core(env, stall=0.01), repo, brief)
    out, err = capsys.readouterr()
    assert code == 124
    run_id = _run_id(out)
    assert "did not change for 0.01 min" in err
    assert err.splitlines()[-1] == (
        f"next: emb run --recover {run_id} "
        "(waits for the turn still running in its session, then collects it)"
    )
    run = record.load(run_id, env)
    assert run.status == record.RUNNING
    assert run.turn_state == record.SENT
    assert run.last_exit == 124
    unfinished = record.load_outcome(run, 1)
    assert unfinished["exit"] is None
    assert unfinished["state"] is None
    assert unfinished["seconds"] > 0
    (run.folder / "stub" / "release").write_text("")
    code = _core(env).execute(lambda: _core(env).recover(run_id))
    out, err = capsys.readouterr()
    assert code == 0, err
    assert " · 1 files · turn 1" in out
    run = record.load(run_id, env)
    assert run.status == record.WAITING
    assert run.turn_state == record.COMMITTED
    assert len(run.commits) == 1
    completed = record.load_outcome(run, 1)
    assert completed["exit"] == 0
    assert completed["seconds"] >= unfinished["seconds"]


def test_screen_changes_keep_a_turn_waiting(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    brief = _brief(tmp_path, "Busy\n@sleep 1.5\n")
    code = _start(_core(env, stall=0.01), repo, brief)
    _, err = capsys.readouterr()
    assert code == 0, err


@pytest.mark.parametrize(
    ("line", "code", "state", "reason"),
    [
        ("@provider rate limited", 73, record.FAILED, "provider error"),
        ("@fail model crashed", 71, record.FAILED, "engine failure"),
        ("@empty", 80, record.WAITING, "no reply"),
    ],
)
def test_turn_outcomes(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    capsys: pytest.CaptureFixture[str],
    line: str,
    code: int,
    state: str,
    reason: str,
) -> None:
    brief = _brief(tmp_path, f"Task\n@write work.txt w\n{line}\n")
    result = _start(_core(env), repo, brief)
    out, err = capsys.readouterr()
    assert result == code
    run = record.load(_run_id(out), env)
    assert run.status == state
    assert run.last_exit == code
    outcome = record.load_outcome(run, 1)
    assert outcome["exit"] == code
    assert outcome["seconds"] >= 0
    assert outcome["incomplete"] == reason
    assert f" · exit {code} · 1 files · turn 1" in out
    assert len(run.commits) == 1
    assert err.splitlines()[-1] == exits.line(code, run=run.id)
    subject = git(Path(run.worktree), "log", "-1", "--format=%s", env=env)
    assert subject == f"Task (turn 1, incomplete: {reason})"


def test_incomplete_later_turn_and_long_subject(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_id = _started(tmp_path, env, repo, capsys, text="y" * 80 + "\n")
    message = _brief(tmp_path, "@write m.txt m\n@fail broke\n", "m.md")
    core_ = _core(env)
    assert core_.execute(lambda: core_.send(run_id, message)) == 71
    run = record.load(run_id, env)
    subject = git(Path(run.worktree), "log", "-1", "--format=%s", env=env)
    assert subject == "y" * 72 + " (turn 2, incomplete: engine failure)"


@pytest.mark.parametrize("legacy", [False, True])
def test_recover_commits_a_collected_failed_turn_with_its_reason(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    capsys: pytest.CaptureFixture[str],
    legacy: bool,
) -> None:
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    brief = _brief(tmp_path, "Task\n@write p.txt p\n@provider rate limited\n")
    code = _start(_core(env), repo, brief)
    out, err = capsys.readouterr()
    assert code == 73
    assert "also exit 77" in err
    run_id = _run_id(out)
    run = record.load(run_id, env)
    assert run.turn_state == record.COLLECTED
    assert run.commits == ()
    saved = record.load_outcome(run, 1)
    assert saved["exit"] == 73
    if legacy:
        del saved["exit"]
        del saved["seconds"]
        record.outcome_path(run, 1).write_text(json.dumps(saved))
    hook.unlink()
    core_ = _core(env)
    assert core_.execute(lambda: core_.recover(run_id)) == 73
    run = record.load(run_id, env)
    assert run.turn_state == record.COMMITTED
    subject = git(Path(run.worktree), "log", "-1", "--format=%s", env=env)
    assert subject == "Task (turn 1, incomplete: provider error)"
    result = record.load_outcome(run, 1)
    assert result["exit"] == 73
    assert result["provider_error"] is True
    assert result["seconds"] >= saved.get("seconds", 0)


def test_signed_out_turn_refreshes_status_and_exits_69(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bin_dir = Path(env["PATH"].split(":")[0])
    for name in ("claude", "codex", "opencode"):
        (bin_dir / name).write_text("#!/bin/sh\nexit 1\n")
        (bin_dir / name).chmod(0o755)
    code = _start(
        _core(env),
        repo,
        _brief(tmp_path, "Task\n@write s.txt s\n@signedout not logged in\n"),
    )
    out, err = capsys.readouterr()
    assert code == 69
    assert "emb run: not logged in" in err
    worktree = Path(record.load(_run_id(out), env).worktree)
    subject = git(worktree, "log", "-1", "--format=%s", env=env)
    assert subject == "Task (turn 1, incomplete: sign-in failure)"
    held = json.loads(Path(env["XDG_CACHE_HOME"], "ember", "engines.json").read_text())
    assert held["engines"]["claude"]["checked_at"] != 1.0
    assert err.splitlines()[-1].startswith("owner: ")
    assert record.load(_run_id(out), env).status == record.FAILED


def test_engine_that_exits_mid_turn_is_71(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    brief = _brief(tmp_path, "Task\n@write e.txt e\n@exit\n")
    code = _start(_core(env), repo, brief)
    out, err = capsys.readouterr()
    assert code == 71
    assert "exited before the turn ended" in err
    run = record.load(_run_id(out), env)
    assert run.status == record.FAILED
    assert run.ended_at is not None
    assert run.turn_state == record.COMMITTED
    subject = git(Path(run.worktree), "log", "-1", "--format=%s", env=env)
    assert subject == "Task (turn 1, incomplete: engine failure)"


def test_read_only_failed_turn_commits_nothing(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    brief = _brief(tmp_path, "Review\n@write r.txt r\n@fail broke\n")
    assert _start(_core(env), repo, brief, read_only=True) == 71
    assert git(repo, "log", "--format=%s", env=env) == "first"


def test_moved_head_is_76(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    brief = _brief(
        tmp_path,
        "Task\n@write x.txt x\n@git commit -q --allow-empty -m own\n",
    )
    code = _start(_core(env), repo, brief)
    out, err = capsys.readouterr()
    assert code == 76
    run = record.load(_run_id(out), env)
    assert "nothing committed" in err
    assert err.splitlines()[-1].startswith(f"next: git -C {run.worktree} log")
    assert run.commits == ()
    assert run.status == record.WAITING
    assert run.turn_state == record.COLLECTED


def test_hook_refusal_is_77(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\necho refused by hook >&2\nexit 1\n")
    hook.chmod(0o755)
    code = _start(_core(env), repo, _brief(tmp_path, "Task\n@write y.txt y\n"))
    out, err = capsys.readouterr()
    assert code == 77
    assert "refused by hook" in err
    run = record.load(_run_id(out), env)
    assert run.commits == ()
    assert run.last_exit == 77
    assert (run.folder / core.HOOK_REFUSAL_NAME).read_text() == "refused by hook\n"
    assert err.splitlines()[-1].startswith(f"next: emb run --send {run.id}")


@pytest.mark.parametrize("retry_succeeds", [False, True])
@pytest.mark.parametrize("provider_error", [False, True])
def test_send_after_hook_refusal_commits_the_fixed_files(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    capsys: pytest.CaptureFixture[str],
    retry_succeeds: bool,
    provider_error: bool,
) -> None:
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nprintf 'first stdout\\n'\nexit 1\n")
    hook.chmod(0o755)
    driver = _driver(env)
    core_ = _core(env, driver)
    failure = "@provider rate limited\n" if provider_error else ""
    assert _start(
        core_, repo, _brief(tmp_path, f"Task\n@write y.txt y\n{failure}")
    ) == (73 if provider_error else 77)
    run_id = _run_id(capsys.readouterr().out)
    run = record.load(run_id, env)
    previous_seconds = record.load_outcome(run, 1)["seconds"]
    refusal = run.folder / core.HOOK_REFUSAL_NAME
    assert refusal.read_text() == "first stdout\n"
    assert stat.S_IMODE(refusal.stat().st_mode) == 0o600
    if retry_succeeds:
        hook.unlink()
    else:
        hook.write_text(
            "#!/bin/sh\n"
            'if [ "$(git show :y.txt)" = fixed ]; then exit 0; fi\n'
            "printf 'refreshed stdout\\n'\n"
            "printf 'refreshed stderr\\377\\n' >&2\nexit 1\n"
        )

    def delivered(run: record.Run) -> None:
        assert run.turn == 2
        assert not refusal.exists()

    driver.on_wait = delivered
    correction = "@write y.txt fixed\n@write next.txt next\n"
    message = _brief(tmp_path, correction, "correction.md")
    assert core_.execute(lambda: core_.send(run_id, message)) == 0
    run = record.load(run_id, env)
    sent = [event["text"] for event in _events(tmp_path, run_id) if "text" in event][-1]
    if retry_succeeds:
        assert sent == correction
        assert len(run.commits) == 2
    else:
        assert sent == (
            "The commit hook refused your last turn's commit:\n\n"
            "```\nrefreshed stdout\nrefreshed stderr\ufffd\n```\n\n" + correction
        )
        assert len(run.commits) == 1
        assert git(Path(run.worktree), "show", "--format=", "--name-only", env=env) == (
            "next.txt\ny.txt"
        )
    assert run.turn_state == record.COMMITTED
    first = record.load_outcome(run, 1)
    assert first["exit"] == (73 if provider_error else (0 if retry_succeeds else 77))
    assert first["provider_error"] is provider_error
    assert first["seconds"] >= previous_seconds
    assert not refusal.exists()
    assert (
        git(Path(run.worktree), "log", "-1", "--format=%s", env=env) == "Task (turn 2)"
    )


@pytest.mark.parametrize("has_refusal", [False, True])
def test_send_stops_on_git_add_failure(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    has_refusal: bool,
) -> None:
    if has_refusal:
        hook = repo / ".git" / "hooks" / "pre-commit"
        hook.write_text("#!/bin/sh\necho hook refused\nexit 1\n")
        hook.chmod(0o755)
    core_ = _core(env)
    real_git = core_._git

    def fail_add(cwd: str | Path, *args: str) -> subprocess.CompletedProcess[str]:
        if args[0] == "add":
            return subprocess.CompletedProcess(args, 1, "", "cannot stage files")
        return real_git(cwd, *args)

    if not has_refusal:
        monkeypatch.setattr(core_, "_git", fail_add)
    assert _start(core_, repo, _brief(tmp_path, "Task\n@write y.txt y\n")) == 77
    run_id = _run_id(capsys.readouterr().out)
    monkeypatch.setattr(core_, "_git", fail_add)
    before = _events(tmp_path, run_id)
    message = _brief(tmp_path, "Fix the files", "correction.md")
    assert core_.execute(lambda: core_.send(run_id, message)) == 77
    err = capsys.readouterr().err
    assert "git add failed" in err
    assert err.splitlines()[-1].startswith(f"next: emb run --recover {run_id}")
    run = record.load(run_id, env)
    assert run.turn == 1
    assert run.turn_state == record.COLLECTED
    assert (run.folder / core.HOOK_REFUSAL_NAME).exists() == has_refusal
    assert _events(tmp_path, run_id) == before


def test_ctrl_c_while_waiting_is_74_and_turn_keeps_running(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def stop(run: record.Run) -> None:
        raise KeyboardInterrupt

    driver = _driver(env, on_wait=stop)
    code = _start(_core(env, driver), repo, _brief(tmp_path, "Task\n@hang\n"))
    out, err = capsys.readouterr()
    assert code == 74
    run_id = _run_id(out)
    assert err.splitlines()[-1].startswith(f"next: emb run --recover {run_id}")
    run = record.load(run_id, env)
    assert run.status == record.RUNNING
    assert run.last_exit == 74


def test_ctrl_c_while_committing_is_74_and_recover_finishes(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def stop(run: record.Run) -> object:
        raise KeyboardInterrupt

    core_ = _core(env)
    monkeypatch.setattr(core_, "_commit", stop)
    code = _start(core_, repo, _brief(tmp_path, "Task\n@write k.txt k\n"))
    out, err = capsys.readouterr()
    assert code == 74
    run_id = _run_id(out)
    assert err.splitlines()[-1] == (
        f"next: emb run --recover {run_id} (finishes the saved turn)"
    )
    run = record.load(run_id, env)
    assert run.turn_state == record.COLLECTED
    assert run.last_exit == 74
    assert _core(env).execute(lambda: _core(env).recover(run_id)) == 0
    run = record.load(run_id, env)
    assert run.turn_state == record.COMMITTED
    assert len(run.commits) == 1


def test_recovery_accumulates_seconds_for_one_result(
    tmp_path: Path,
    env: dict[str, str],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _bare_run(env, tmp_path, "claude", "none")
    run = replace(
        run,
        folder=record.make_run_dir(run.id, env),
        turn=1,
        turn_state=record.SENT,
        status=record.RUNNING,
    )
    record.save(run)
    clock = [10.0]
    monkeypatch.setattr(core.time, "monotonic", lambda: clock[0])
    core_ = _core(env)

    def ended(run: record.Run, driver: Driver) -> engines.TurnOutcome:
        clock[0] = 13.0
        return engines.TurnOutcome(engines.DONE, reply="done")

    def interrupted(run: record.Run) -> object:
        saved = record.load_outcome(run, run.turn)
        assert saved["exit"] is None
        assert saved["seconds"] == 3.0
        clock[0] = 15.0
        raise KeyboardInterrupt

    monkeypatch.setattr(core_, "_wait_turn", ended)
    monkeypatch.setattr(core_, "_commit", interrupted)
    assert core_.execute(lambda: core_._turn(run, _driver(env), 10.0, None)) == 74
    saved = record.load_outcome(run, 1)
    assert saved["exit"] is None
    assert saved["seconds"] == 5.0
    assert saved["state"] == engines.DONE

    def committed(
        run: record.Run,
    ) -> tuple[record.Run, None, int, int, None, None]:
        clock[0] = 24.0
        run = record.update(run, {"turn_state": record.COMMITTED})
        return run, None, 0, 0, None, None

    clock[0] = 20.0
    monkeypatch.setattr(core_, "_commit", committed)
    assert core_.execute(lambda: core_.recover(run.id)) == 0
    turns, errors = record.turn_results(run)
    assert not errors
    assert turns == [{"turn": 1, **saved, "exit": 0, "seconds": 9.0}]
    assert " · 9s · exit 0" in capsys.readouterr().out
    assert stat.S_IMODE(record.outcome_path(run, 1).stat().st_mode) == 0o600


def test_turn_lock_releases_with_a_duplicate_descriptor(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    original = paths.open_private_file
    duplicates: list[int] = []

    def duplicate(path: Path, flags: int = os.O_RDWR) -> int:
        fd = original(path, flags)
        if not duplicates:
            duplicates.append(os.dup(fd))
        return fd

    monkeypatch.setattr(paths, "open_private_file", duplicate)
    core_ = _core(env)
    try:
        with (
            core_._turn_lock("claude-test-000000"),
            pytest.raises(RunError, match="another emb run command"),
            core_._turn_lock("claude-test-000000"),
        ):
            pytest.fail("the turn lock was not held")
        with core_._turn_lock("claude-test-000000"):
            pass
    finally:
        for fd in duplicates:
            os.close(fd)


@pytest.mark.parametrize("operation", ["start", "add", "remove"])
def test_worktree_operations_wait_for_other_registrations(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    core_ = _core(env)
    common = repo / ".git"
    saved = replace(
        _bare_run(env, tmp_path, "claude", "none"),
        common=str(common),
        worktree=str(paths.worktree_path("worker", env)),
        read_only=True,
        detached=True,
        base=git(repo, "rev-parse", "HEAD", env=env),
        status=record.CLOSED,
        folder=record.make_run_dir("claude-20260101-000000-abcdef", env),
    )
    record.save(saved)
    waiting = threading.Event()
    entered = threading.Event()
    flock = core.fcntl.flock
    run_git = core_._git

    def observe_lock(fd: int, flags: int) -> None:
        if flags == core.fcntl.LOCK_EX:
            waiting.set()
        flock(fd, flags)

    def observe_git(
        directory: str | Path, *args: str
    ) -> subprocess.CompletedProcess[str]:
        if args[0] == "worktree":
            entered.set()
        return run_git(directory, *args)

    def remove(run: record.Run) -> bool:
        entered.set()
        return True

    def work() -> None:
        if operation == "start":
            core_.start(
                StartOptions("claude", dir=str(repo), task_file=brief, window=False)
            )
        elif operation == "add":
            core_._create_worktree(saved, repo)
        else:
            core_._remove_detached(saved)

    brief = _brief(tmp_path, "Task\n")
    monkeypatch.setattr(core_, "_git", observe_git)
    monkeypatch.setattr(core.worktrees, "remove_detached", remove)
    with ThreadPoolExecutor(max_workers=1) as executor:
        with core_._worktree_lock(common):
            monkeypatch.setattr(core.fcntl, "flock", observe_lock)
            future = executor.submit(work)
            assert waiting.wait(20), "the operation did not wait for registration"
            assert not entered.is_set(), "the operation saw an unfinished registration"
        future.result(20)
    assert entered.is_set()


@pytest.mark.parametrize("read_only", [False, True])
@pytest.mark.parametrize("completion", ["recover", "send"])
def test_recovery_completes_result_after_commit_handling(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    read_only: bool,
    completion: str,
) -> None:
    core_ = _core(env)
    original = core_._record_exit
    stopped = False

    def stop_once(
        run: record.Run, code: int, message: str | None, **changes: object
    ) -> record.Run:
        nonlocal stopped
        if code == 0 and not stopped:
            stopped = True
            raise KeyboardInterrupt
        return original(run, code, message, **changes)

    monkeypatch.setattr(core_, "_record_exit", stop_once)
    assert (
        _start(
            core_, repo, _brief(tmp_path, "Task\n@reply done\n"), read_only=read_only
        )
        == 74
    )
    run_id = _run_id(capsys.readouterr().out)
    run = record.load(run_id, env)
    assert run.turn_state == (record.COLLECTED if read_only else record.COMMITTED)
    before = record.load_outcome(run, 1)
    assert before["exit"] is None
    if completion == "send":
        message = _brief(tmp_path, "@reply next\n", "next.md")
        assert core_.execute(lambda: core_.send(run_id, message)) == 0
    else:
        assert core_.execute(lambda: core_.recover(run_id)) == 0
    after = record.load_outcome(run, 1)
    assert after["exit"] == 0
    assert after["seconds"] >= before["seconds"]
    turns, errors = record.turn_results(record.load(run_id, env))
    assert not errors
    assert [turn["turn"] for turn in turns] == ([1, 2] if completion == "send" else [1])


def test_driver_error_after_forced_close_is_72(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[int] = []

    def close_then_fail(run: record.Run) -> None:
        calls.append(1)
        if len(calls) == 2:
            _core(env).close(run.id, force=True)
            raise engines.DriverError("the session's files are gone")

    driver = _driver(env, on_wait=close_then_fail)
    code = _start(_core(env, driver), repo, _brief(tmp_path, "Task\n@hang\n"))
    out, err = capsys.readouterr()
    assert code == 72
    assert "was closed" in err
    assert record.load(_run_id(out), env).status == record.CLOSED


def test_close_while_waiting_stops_the_waiting_command_with_72(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[int] = []

    def close_once(run: record.Run) -> None:
        calls.append(1)
        if len(calls) == 2:
            _core(env).close(run.id, force=True)

    driver = _driver(env, on_wait=close_once)
    code = _start(_core(env, driver), repo, _brief(tmp_path, "Task\n@hang\n"))
    out, err = capsys.readouterr()
    assert code == 72
    run = record.load(_run_id(out), env)
    assert run.status == record.CLOSED
    assert "was closed" in err


# Start refusals


@pytest.mark.parametrize("dry_run", [False, True])
def test_missing_files_are_66(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    capsys: pytest.CaptureFixture[str],
    dry_run: bool,
) -> None:
    assert _start(_core(env), repo, str(tmp_path / "none.md"), dry_run=dry_run) == 66
    brief = _brief(tmp_path, "Task\n")
    assert (
        _start(_core(env), repo, brief, read=(str(tmp_path / "gone"),), dry_run=dry_run)
        == 66
    )
    assert _start(_core(env), tmp_path / "nowhere", brief, dry_run=dry_run) == 66
    err = capsys.readouterr().err
    assert "does not exist" in err
    assert not paths.runs_dir(env).exists()


@pytest.mark.parametrize("dry_run", [False, True])
def test_not_a_checkout_is_65(
    tmp_path: Path,
    env: dict[str, str],
    capsys: pytest.CaptureFixture[str],
    dry_run: bool,
) -> None:
    folder = tmp_path / "plain"
    folder.mkdir()
    assert _start(_core(env), folder, _brief(tmp_path, "Task\n"), dry_run=dry_run) == 65
    assert "not in a git checkout" in capsys.readouterr().err


@pytest.mark.parametrize("dry_run", [False, True])
def test_empty_brief_is_64(
    tmp_path: Path, env: dict[str, str], repo: Path, dry_run: bool
) -> None:
    assert _start(_core(env), repo, _brief(tmp_path, "\n  \n"), dry_run=dry_run) == 64


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("trailer", ["", "\n", "Co-Authored-By: X\nOther: Y"])
def test_invalid_trailer_is_64(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    capsys: pytest.CaptureFixture[str],
    dry_run: bool,
    trailer: str,
) -> None:
    assert (
        _start(
            _core(env), repo, _brief(tmp_path, "Task\n"),
            trailers=(trailer,), dry_run=dry_run,
        )
        == 64
    )  # fmt: skip
    assert "is not one line" in capsys.readouterr().err
    assert not paths.runs_dir(env).exists()


@pytest.mark.parametrize(
    ("settings", "options", "needle"),
    [
        ({"engine.claude": "off"}, {}, "claude is switched off"),
        ({"single.writer": "ask"}, {}, "menu row single.writer is ask"),
        ({"single.writer": "codex:gpt-5"}, {}, "names codex, not claude"),
        ({"single.writer_as": "subagent"}, {}, "runs as a subagent inside Claude"),
        ({}, {"effort": "extreme"}, "does not take the effort 'extreme'"),
        ({}, {"claim": "never-claimed"}, "claim never-claimed is unknown"),
        ({}, {"claim": "../bad"}, "is not a valid id"),
    ],
)
@pytest.mark.parametrize("dry_run", [False, True])
def test_selection_refusals_are_64(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    capsys: pytest.CaptureFixture[str],
    settings: dict[str, object],
    options: dict[str, object],
    needle: str,
    dry_run: bool,
) -> None:
    if settings:
        choices.set_settings(settings, env=env)
    assert (
        _start(_core(env), repo, _brief(tmp_path, "Task\n"), dry_run=dry_run, **options)
        == 64
    )
    assert needle in capsys.readouterr().err
    assert not paths.runs_dir(env).exists()


def test_switched_off_engine_named_explicitly_is_64(
    tmp_path: Path, env: dict[str, str], repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    choices.set_settings({"engine.claude": "off"}, env=env)
    code = _start(_core(env), repo, _brief(tmp_path, "Task\n"), model="opus")
    assert code == 64
    err = capsys.readouterr().err
    assert err.splitlines()[-1] == (
        "owner: switch claude on: emb choices --set engine.claude=on"
    )


def test_claimed_choice_selects_the_model(
    tmp_path: Path, env: dict[str, str], repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    choices.set_settings({"shortlist.claude": ["opus@high", "haiku@low"]}, env=env)
    choices.set_once({"single.writer": "claude:haiku"}, env=env)
    choices.claim("run-claim-1", env=env)
    before = choices.read("run-claim-1", env=env)
    assert (
        _start(
            _core(env), repo, _brief(tmp_path, "Task\n"),
            claim="run-claim-1", dry_run=True,
        )
        == 0
    )  # fmt: skip
    assert capsys.readouterr().out == (
        "Team, Single tasks Writer: claude haiku · low (single.writer)\n"
    )
    assert choices.read("run-claim-1", env=env) == before
    code = _start(_core(env), repo, _brief(tmp_path, "Task\n"), claim="run-claim-1")
    out, _ = capsys.readouterr()
    assert code == 0
    run = record.load(_run_id(out), env)
    assert (run.model, run.effort, run.claim) == ("haiku", "low", "run-claim-1")


def test_disconnected_engine_is_69_before_menu_row(
    tmp_path: Path, env: dict[str, str], repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    engine_cache(env, {"claude": False})
    choices.set_settings({"single.writer": "ask"}, env=env)
    assert _start(_core(env), repo, _brief(tmp_path, "Task\n")) == 69
    err = capsys.readouterr().err
    assert "claude is not connected: signed out" in err
    assert err.splitlines()[-1] == "owner: claude login"


def test_missing_status_cache_is_refreshed_once(
    tmp_path: Path, env: dict[str, str], repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    Path(env["XDG_CACHE_HOME"], "ember", "engines.json").unlink()
    bin_dir = Path(env["PATH"].split(":")[0])
    for name in ("claude", "codex", "opencode"):
        (bin_dir / name).write_text("#!/bin/sh\nexit 1\n")
        (bin_dir / name).chmod(0o755)
    assert _start(_core(env), repo, _brief(tmp_path, "Task\n")) == 69
    assert Path(env["XDG_CACHE_HOME"], "ember", "engines.json").exists()
    assert "claude is not connected" in capsys.readouterr().err


def test_missing_driver_is_69(
    tmp_path: Path, env: dict[str, str], repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def missing(name: str) -> Driver:
        raise engines.DriverMissingError(f"Ember has no driver for {name}")

    core_ = Core(env, load_driver=missing)
    assert _start(core_, repo, _brief(tmp_path, "Task\n")) == 69
    err = capsys.readouterr().err
    assert "Ember has no driver for claude" in err
    assert err.splitlines()[-1] == (
        "owner: this Ember build has no driver for claude; update Ember"
    )


def test_subagent_row_is_ignored_with_explicit_model(
    tmp_path: Path, env: dict[str, str], repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    choices.set_settings({"single.writer_as": "subagent"}, env=env)
    code = _start(_core(env), repo, _brief(tmp_path, "Task\n"), model="opus")
    out, err = capsys.readouterr()
    assert code == 0, err
    run = record.load(_run_id(out), env)
    # The row's effort is high; the driver's default is medium.
    assert (run.model, run.effort) == ("opus", "medium")


@pytest.mark.parametrize(
    ("error", "code", "last"),
    [
        (
            paths.RuntimeDirError("XDG_RUNTIME_DIR is unset"),
            69,
            "owner: set XDG_RUNTIME_DIR to a private folder of this user "
            "(the message names the problem)",
        ),
        (
            sandbox.SandboxError("no bwrap"),
            69,
            "owner: install or connect claude; emb engines shows what is missing",
        ),
        (OSError("disk full"), 1, "next: emb run --status"),
        (ValueError("bad"), 1, "next: emb run --status"),
    ],
)
def test_start_failure_after_the_run_folder_exists_is_recorded(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    capsys: pytest.CaptureFixture[str],
    error: Exception,
    code: int,
    last: str,
) -> None:
    class Raising(StubDriver):
        def command(self, run: record.Run, first_message: str) -> LaunchSpec:
            raise error

    driver = Raising("claude", script_path(env))
    assert _start(_core(env, driver), repo, _brief(tmp_path, "Task\n")) == code
    out, err = capsys.readouterr()
    assert err.splitlines()[-1] == last
    run = record.load(_run_id(out), env)
    assert run.status == record.FAILED
    assert run.last_exit == code
    assert str(error) in (run.last_reason or "")
    if last.startswith("owner:"):
        assert run.owner_wait == (last,)
        assert _core(env).step(run) == ("owner", None)


def test_failed_start_calls_driver_close_and_keeps_the_start_error(
    tmp_path: Path, env: dict[str, str], repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    closed: list[str] = []

    class Raising(StubDriver):
        def command(self, run: record.Run, first_message: str) -> LaunchSpec:
            raise OSError("disk full")

        def close(self, run: record.Run) -> None:
            closed.append(run.id)
            raise RuntimeError("clean-up failed too")

    driver = Raising("claude", script_path(env))
    assert _start(_core(env, driver), repo, _brief(tmp_path, "Task\n")) == 1
    out = capsys.readouterr().out
    run_id = _run_id(out)
    assert closed == [run_id]
    assert "disk full" in (record.load(run_id, env).last_reason or "")


def test_preparation_settings_database_error_is_69(
    tmp_path: Path, env: dict[str, str], repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = paths.settings_db_path(env)
    path.unlink()
    path.write_bytes(b"not a database at all, just bytes" * 10)
    assert _start(_core(env), repo, _brief(tmp_path, "Task\n")) == 69
    err = capsys.readouterr().err
    assert "settings database" in err
    assert "worktree.prepare" in err
    assert err.splitlines()[-1].startswith("owner: ")


@pytest.mark.parametrize("dry_run", [False, True])
def test_repository_without_main_checkout_is_70(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    capsys: pytest.CaptureFixture[str],
    dry_run: bool,
) -> None:
    bare = tmp_path / "bare.git"
    git(tmp_path, "clone", "-q", "--bare", str(repo), str(bare), env=env)
    linked = tmp_path / "linked"
    git(bare, "worktree", "add", "-q", str(linked), "main", env=env)
    assert _start(_core(env), linked, _brief(tmp_path, "Task\n"), dry_run=dry_run) == 70
    assert "has no main checkout" in capsys.readouterr().err
    assert not paths.runs_dir(env).exists()


def test_read_only_run_from_bare_backed_checkout(
    tmp_path: Path, env: dict[str, str], repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bare = tmp_path / "bare.git"
    git(tmp_path, "clone", "-q", "--bare", str(repo), str(bare), env=env)
    linked = tmp_path / "linked"
    git(bare, "worktree", "add", "-q", str(linked), "main", env=env)
    assert (
        _start(
            _core(env),
            linked,
            _brief(tmp_path, "Review\n@reply fine\n"),
            read_only=True,
        )
        == 0
    )
    captured = capsys.readouterr()
    run = record.load(_run_id(captured.out), env)
    assert run.status == record.WAITING
    assert run.read_only and run.branch is None
    assert run.repo == str(linked)
    assert run.worktree == str(paths.worktree_path(run.id, env))
    assert run.detached
    assert run.main is None
    assert run.common == str(bare)
    assert (run.folder / "turn-1.md").read_text() == "fine"


def test_run_from_linked_worktree_uses_main_checkout(
    tmp_path: Path, env: dict[str, str], repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    linked = tmp_path / "linked"
    git(repo, "worktree", "add", "-q", "-b", "side", str(linked), env=env)
    code = _start(_core(env), linked / ".", _brief(tmp_path, "Task\n"))
    out, err = capsys.readouterr()
    assert code == 0, err
    run = record.load(_run_id(out), env)
    assert run.repo == str(linked)
    assert run.worktree == str(paths.worktree_path(run.id, env))
    assert not Path(run.worktree).is_relative_to(repo)


def test_tmux_missing_is_69(
    tmp_path: Path, env: dict[str, str], repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (Path(env["PATH"].split(":")[0]) / "tmux").unlink()
    assert _start(_core(env), repo, _brief(tmp_path, "Task\n")) == 69
    assert capsys.readouterr().err.splitlines()[-1] == "owner: install tmux"


def test_engine_that_exits_before_ready_is_71(
    tmp_path: Path, env: dict[str, str], repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    class Broken(StubDriver):
        def command(self, run: record.Run, first_message: str) -> LaunchSpec:
            return LaunchSpec(
                argv=("false",), cwd=Path(run.worktree), first_message="x"
            )

    code = _start(
        _core(env, Broken("claude", script_path(env))), repo, _brief(tmp_path, "T\n")
    )
    out, err = capsys.readouterr()
    assert code == 71
    assert "exited before it took its brief" in err
    run = record.load(_run_id(out), env)
    assert run.status == record.FAILED
    assert run.turn == 0


# Sandbox


def test_sandbox_none_warns_and_is_recorded(
    tmp_path: Path, env: dict[str, str], repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    core_ = _core(env, _driver(env, "codex"))
    code = _start(
        core_, repo, _brief(tmp_path, "Task\n"), engine="codex", model="gpt-5"
    )
    out, err = capsys.readouterr()
    assert code == 0, err
    assert "WARNING: codex runs without a sandbox" in err
    run = record.load(_run_id(out), env)
    assert run.sandbox == "none"
    assert (run.model, run.effort) == ("gpt-5", "medium")


def test_claude_sandbox_off_warns(
    tmp_path: Path, env: dict[str, str], repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = _start(_core(env), repo, _brief(tmp_path, "Task\n"))
    out, err = capsys.readouterr()
    assert code == 0, err
    assert "WARNING: claude runs without a sandbox" in err
    assert record.load(_run_id(out), env).sandbox == "none"


@pytest.mark.parametrize(("engine", "model"), [("claude", "opus"), ("codex", "gpt-5")])
def test_sandbox_on_without_bwrap_is_69(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    capsys: pytest.CaptureFixture[str],
    engine: str,
    model: str,
) -> None:
    choices.set_settings({f"sandbox.{engine}": "on"}, env=env)
    core_ = _core(env, _driver(env, engine))
    code = _start(core_, repo, _brief(tmp_path, "Task\n"), engine=engine, model=model)
    assert code == 69
    err = capsys.readouterr().err
    assert "bwrap is not" in err
    assert err.splitlines()[-1] == (
        "owner: install bubblewrap (the bwrap command) or make it work, "
        f"or set sandbox.{engine} to off"
    )
    assert not paths.runs_dir(env).exists()


def test_sandbox_on_with_a_blocked_bwrap_is_69(
    tmp_path: Path, env: dict[str, str], repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bwrap = Path(env["PATH"].split(":")[0]) / "bwrap"
    bwrap.write_text(
        "#!/bin/sh\necho 'bwrap: setting up uid map: denied' >&2\nexit 1\n"
    )
    bwrap.chmod(0o755)
    choices.set_settings({"sandbox.claude": "on"}, env=env)
    code = _start(_core(env), repo, _brief(tmp_path, "Task\n"))
    assert code == 69
    err = capsys.readouterr().err
    assert "setting up uid map: denied" in err
    assert err.splitlines()[-1].endswith("or set sandbox.claude to off")
    assert not paths.runs_dir(env).exists()


def test_claude_run_records_its_sandbox_network_and_read_paths(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sandbox, "require", lambda engine, env=None: None)
    shared = tmp_path / "shared"
    shared.mkdir()
    extra = tmp_path / "extra"
    extra.mkdir()
    choices.set_settings(
        {
            "sandbox.claude": "on",
            "worker.network": "on",
            "sandbox.read": [str(shared)],
        },
        env=env,
    )
    code = _start(_core(env), repo, _brief(tmp_path, "Task\n"), read=(str(extra),))
    out, err = capsys.readouterr()
    assert code == 0, err
    assert sandbox.none_warning("claude") not in err
    run = record.load(_run_id(out), env)
    assert run.sandbox == "bubblewrap"
    assert run.network is True
    assert run.read == (str(extra),)
    assert run.read_effective == (str(shared), str(extra))


def test_session_runs_the_command_from_a_launch_script(
    tmp_path: Path, env: dict[str, str], repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = _start(_core(env), repo, _brief(tmp_path, "Task\n"))
    out, err = capsys.readouterr()
    assert code == 0, err
    run = record.load(_run_id(out), env)
    launch = run.folder / core.LAUNCH_NAME
    assert stat.S_IMODE(launch.stat().st_mode) == 0o600
    text = launch.read_text()
    assert text.startswith("exec ")
    assert str(script_path(env)) in text


def test_sandboxed_run_creates_the_workers_cache(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sandbox, "require", lambda engine, env=None: None)
    choices.set_settings({"sandbox.claude": "on"}, env=env)
    code = _start(_core(env), repo, _brief(tmp_path, "Task\n"))
    assert code == 0, capsys.readouterr().err
    folder = paths.worker_cache_dir(env)
    assert folder.is_dir()
    assert stat.S_IMODE(folder.stat().st_mode) == 0o700


def test_sandboxed_run_whose_workers_cache_cannot_be_made_is_69(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sandbox, "require", lambda engine, env=None: None)
    choices.set_settings({"sandbox.claude": "on"}, env=env)
    folder = paths.worker_cache_dir(env)
    folder.parent.mkdir(parents=True, exist_ok=True)
    folder.write_text("")
    code = _start(_core(env), repo, _brief(tmp_path, "Task\n"))
    assert code == 69
    err = capsys.readouterr().err
    assert f"cannot create the workers' cache folder {folder}" in err
    assert err.splitlines()[-1] == (
        f"owner: make {folder} a folder you can write, or set XDG_CACHE_HOME"
    )
    assert not paths.runs_dir(env).exists()


def test_sandboxed_workers_cache_inside_embers_config_folder_is_69(
    tmp_path: Path, env: dict[str, str], repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    env["XDG_CACHE_HOME"] = env["XDG_CONFIG_HOME"]
    engine_cache(env, {})
    choices.set_settings({"sandbox.claude": "on"}, env=env)
    code = _start(_core(env), repo, _brief(tmp_path, "Task\n"))
    assert code == 69
    err = capsys.readouterr().err
    assert err.splitlines()[-1] == (
        f"owner: set XDG_CACHE_HOME so that {paths.worker_cache_dir(env)} is "
        f"outside {paths.config_dir(env)}, or set sandbox.claude to off"
    )


def test_codex_sandbox_conflict_is_69(
    tmp_path: Path, env: dict[str, str], repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    choices.set_settings({"sandbox.codex": "on"}, env=env)
    (repo / ".codex").mkdir()
    (repo / ".codex" / "config.toml").write_text('sandbox_mode = "danger"\n')
    core_ = _core(env, _driver(env, "codex"))
    code = _start(
        core_, repo, _brief(tmp_path, "Task\n"), engine="codex", model="gpt-5"
    )
    assert code == 69
    err = capsys.readouterr().err
    assert str(repo / ".codex" / "config.toml") in err
    assert err.splitlines()[-1].startswith("owner: remove the sandbox_mode key from ")
    assert not paths.runs_dir(env).exists()


@pytest.mark.parametrize("inside", ["checkout", "linked"])
def test_worktree_folder_inside_the_repository_is_69(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    capsys: pytest.CaptureFixture[str],
    inside: str,
) -> None:
    linked = tmp_path / "linked"
    git(repo, "worktree", "add", "-q", "-b", "side", str(linked), env=env)
    folder = repo if inside == "checkout" else linked
    env["XDG_DATA_HOME"] = str(folder / "data")
    code = _start(_core(env), repo, _brief(tmp_path, "Task\n"))
    assert code == 69
    err = capsys.readouterr().err
    worktrees = folder / "data" / "ember" / "worktrees"
    assert (
        f"the worktree folder {worktrees} is inside the repository folder {folder}; "
        "XDG_DATA_HOME places it there"
    ) in err
    assert err.splitlines()[-1] == (
        f"owner: set XDG_DATA_HOME so that {worktrees} is outside every git checkout"
    )
    assert not paths.runs_dir(env).exists()
    assert not (folder / "data").exists()
    assert git(repo, "branch", "--list", "ember/*", env=env) == ""


def test_sandboxed_read_path_inside_embers_folder_is_69(
    tmp_path: Path, env: dict[str, str], repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    inside = paths.config_dir(env) / "notes"
    inside.mkdir(parents=True)
    choices.set_settings({"sandbox.claude": "on"}, env=env)
    code = _start(_core(env), repo, _brief(tmp_path, "Task\n"), read=(str(inside),))
    assert code == 69
    err = capsys.readouterr().err
    assert (
        f"the claude sandbox cannot start: the kept path {inside} is inside "
        f"{paths.config_dir(env)}, which the sandbox hides from every worker"
    ) in err
    assert err.splitlines()[-1] == (
        f"owner: leave {inside} out of sandbox.read and --read, "
        "or set sandbox.claude to off"
    )
    assert not paths.runs_dir(env).exists()


def test_sandboxed_worktree_inside_embers_state_folder_is_69(
    tmp_path: Path, env: dict[str, str], repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    env["XDG_DATA_HOME"] = env["XDG_STATE_HOME"]
    choices.set_settings({"sandbox.claude": "on"}, env=env)
    code = _start(_core(env), repo, _brief(tmp_path, "Task\n"))
    assert code == 69
    err = capsys.readouterr().err
    assert f"is inside {paths.state_dir(env)}, which the sandbox hides" in err
    assert err.splitlines()[-1] == (
        f"owner: set XDG_DATA_HOME so that {paths.worktrees_dir(env)} is outside "
        f"{paths.state_dir(env)}, or set sandbox.claude to off"
    )
    assert not paths.runs_dir(env).exists()


def test_unsandboxed_read_path_inside_embers_folder_starts(
    tmp_path: Path, env: dict[str, str], repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    inside = paths.config_dir(env) / "notes"
    inside.mkdir(parents=True)
    code = _start(_core(env), repo, _brief(tmp_path, "Task\n"), read=(str(inside),))
    assert code == 0, capsys.readouterr().err


def test_codex_sandbox_conflict_in_the_new_worktree_is_69(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sandbox, "require", lambda engine, env=None: None)
    choices.set_settings({"sandbox.codex": "on"}, env=env)
    (repo / ".codex").mkdir()
    (repo / ".codex" / "config.toml").write_text('sandbox_mode = "danger"\n')
    git(repo, "add", "-A", env=env)
    git(repo, "commit", "-q", "-m", "codex config", env=env)
    # The main checkout no longer has the file; the commit still does.
    (repo / ".codex" / "config.toml").unlink()
    core_ = _core(env, _driver(env, "codex"))
    code = _start(
        core_, repo, _brief(tmp_path, "Task\n"), engine="codex", model="gpt-5"
    )
    out, err = capsys.readouterr()
    assert code == 69
    run = record.load(_run_id(out), env)
    conflict = Path(run.worktree) / ".codex" / "config.toml"
    assert str(conflict) in err
    assert err.splitlines()[-1].startswith("owner: remove the sandbox_mode key from ")
    assert run.status == record.FAILED
    assert conflict.exists()
    assert git(repo, "branch", "--list", run.branch or "", env=env)


def _bare_run(
    env: dict[str, str], tmp_path: Path, engine: str, provider: str
) -> record.Run:
    return record.Run(
        id=f"{engine}-20260101-000000-abcdef",
        engine=engine,
        model="m",
        effort=None,
        read_only=False,
        repo=str(tmp_path),
        worktree=str(tmp_path / "wt"),
        branch="b",
        base="0" * 40,
        sandbox=provider,
        network=False,
        read=(str(tmp_path / "extra"),),
        read_effective=(str(tmp_path / "setting"), str(tmp_path / "extra")),
        trailers=(),
        claim=None,
        status=record.STARTING,
        turn=0,
        turn_state=None,
        reply=None,
        expected_head=None,
        started_at=1.0,
        updated_at=1.0,
        ended_at=None,
        last_exit=None,
        last_reason=None,
        commits=(),
        subject="s",
        folder=tmp_path,
        env=env,
    )


def test_codex_command_gets_the_permission_profile(
    tmp_path: Path, env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "wt").mkdir()
    (tmp_path / "setting").mkdir()
    monkeypatch.setattr(sandbox, "codex_bwrap", lambda binary, env=None: ("b", None))
    run = replace(_bare_run(env, tmp_path, "codex", "codex"), network=True)
    spec = LaunchSpec(argv=("codex", "--model", "m"), cwd=tmp_path / "wt")
    wrapped = _core(env)._sandboxed(spec, run, tmp_path / ".git")
    assert wrapped.argv[0] == "codex"
    assert wrapped.argv[1] == "-c"
    assert wrapped.argv[-2:] == ("--model", "m")
    assert any("default_permissions" in arg for arg in wrapped.argv)
    assert "permissions.ember.network.enabled=true" in wrapped.argv
    assert "PATH" not in wrapped.env


@pytest.mark.parametrize("engine", ["codex", "opencode"])
@pytest.mark.parametrize(
    ("read_only", "detached"), [(False, False), (True, False), (True, True)]
)
def test_sandbox_permissions_allow_detached_review_builds(
    tmp_path: Path,
    env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    engine: str,
    read_only: bool,
    detached: bool,
) -> None:
    worktree = tmp_path / "wt"
    env["TMPDIR"] = str(tmp_path / "owner-tmp")
    worktree.mkdir()
    common = tmp_path / ".git"
    common.mkdir()
    monkeypatch.setattr(sandbox, "codex_bwrap", lambda binary, env=None: ("b", None))
    saved = replace(
        _bare_run(
            env, tmp_path, engine, "codex" if engine == "codex" else "bubblewrap"
        ),
        read_only=read_only,
        detached=detached,
    )
    spec = LaunchSpec(argv=(engine,), cwd=worktree)
    wrapped = _core(env)._sandboxed(spec, saved, common)
    assert "TMPDIR" not in wrapped.env
    writable = detached or not read_only
    if engine == "codex":
        config = tomllib.loads(
            "\n".join(
                wrapped.argv[i + 1] for i, arg in enumerate(wrapped.argv) if arg == "-c"
            )
        )
        profile = config["permissions"]["ember"]
        assert profile["extends"] == (":workspace" if writable else ":read-only")
        assert profile["filesystem"][str(worktree)] == ("write" if writable else "read")
        assert profile["filesystem"][str(common)] == "read"
        assert ":slash_tmp" not in profile["filesystem"]
        temporary = paths.run_tmp_dir(saved.id, env)
        assert config["shell_environment_policy"]["set"]["TMPDIR"] == str(temporary)
        assert profile["filesystem"][str(temporary)] == "write"
        assert profile["filesystem"][str(worktree / ".git")] == "read"
    else:
        mounts = list(
            zip(wrapped.argv, wrapped.argv[1:], wrapped.argv[2:], strict=False)
        )
        assert (("--bind", str(worktree), str(worktree)) in mounts) is writable
        assert ("--ro-bind", str(common), str(common)) in mounts


def test_codex_without_a_system_bwrap_uses_the_bundled_one(
    tmp_path: Path, env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "wt").mkdir()
    folder = tmp_path / "bundled"
    monkeypatch.setattr(sandbox, "codex_bwrap", lambda binary, env=None: ("b", folder))
    run = _bare_run(env, tmp_path, "codex", "codex")
    spec = LaunchSpec(argv=("codex",), cwd=tmp_path / "wt")
    wrapped = _core(env)._sandboxed(spec, run, tmp_path / ".git")
    assert wrapped.env["PATH"] == f"{folder}:{env['PATH']}"


def test_opencode_command_runs_inside_bwrap(
    tmp_path: Path, env: dict[str, str]
) -> None:
    (tmp_path / "wt").mkdir()
    (tmp_path / "extra").mkdir()
    run = _bare_run(env, tmp_path, "opencode", "bubblewrap")
    spec = LaunchSpec(argv=("opencode", "run"), cwd=tmp_path / "wt")
    wrapped = _core(env)._sandboxed(spec, run, tmp_path / ".git")
    assert wrapped.argv[0] == "bwrap"
    assert wrapped.argv[-2:] == ("opencode", "run")
    extra = str(tmp_path / "extra")
    assert ("--ro-bind", extra, extra) in zip(
        wrapped.argv, wrapped.argv[1:], wrapped.argv[2:], strict=False
    )


def test_a_launch_names_the_engine_binary_the_sandbox_keeps(
    tmp_path: Path, env: dict[str, str]
) -> None:
    home = Path(env["HOME"])
    folder = home / ".opencode" / "bin"
    folder.mkdir(parents=True)
    binary = folder / "opencode"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    (tmp_path / "wt").mkdir()
    run = _bare_run(env, tmp_path, "opencode", "bubblewrap")
    spec = LaunchSpec(
        argv=("sh", "-c", "script", "ember", str(binary)),
        cwd=tmp_path / "wt",
        binary=str(binary),
    )
    wrapped = _core(env)._sandboxed(spec, run, tmp_path / ".git")
    assert ("--ro-bind", str(folder), str(folder)) in zip(
        wrapped.argv, wrapped.argv[1:], wrapped.argv[2:], strict=False
    )
    without = _core(env)._sandboxed(
        LaunchSpec(argv=spec.argv, cwd=spec.cwd), run, tmp_path / ".git"
    )
    assert ("--ro-bind", str(folder), str(folder)) not in zip(
        without.argv, without.argv[1:], without.argv[2:], strict=False
    )


@pytest.mark.parametrize(
    ("engine", "provider"), [("claude", "none"), ("claude", "bubblewrap")]
)
def test_claude_and_no_sandbox_leave_the_command(
    tmp_path: Path, env: dict[str, str], engine: str, provider: str
) -> None:
    run = _bare_run(env, tmp_path, engine, provider)
    spec = LaunchSpec(argv=("claude",), cwd=tmp_path)
    assert _core(env)._sandboxed(spec, run, tmp_path) == spec
    codex = _bare_run(env, tmp_path, "codex", "none")
    assert _core(env)._sandboxed(spec, codex, tmp_path) == spec


# Commands on a run


def _started(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    capsys: pytest.CaptureFixture[str],
    text: str = "Task\n",
    driver: Driver | None = None,
) -> str:
    code = _start(_core(env, driver), repo, _brief(tmp_path, text))
    out, err = capsys.readouterr()
    assert code == 0, err
    return _run_id(out)


def test_second_command_on_a_run_is_75(
    tmp_path: Path, env: dict[str, str], repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run_id = _started(tmp_path, env, repo, capsys)
    holder = _core(env)
    with holder._turn_lock(run_id):
        core_ = _core(env)
        assert core_.execute(lambda: core_.recover(run_id)) == 75
        message = _brief(tmp_path, "more\n", "m.md")
        assert core_.execute(lambda: core_.send(run_id, message)) == 75
        assert core_.execute(lambda: core_.close(run_id)) == 75
        assert core_.execute(lambda: core_.status(run_id)) == 0
        assert core_.execute(lambda: core_.screen(run_id)) == 0
    err = capsys.readouterr().err
    assert f"another emb run command is working on {run_id}" in err


def test_send_while_busy_is_75(
    tmp_path: Path, env: dict[str, str], repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    driver = _driver(env, idle=engines.BUSY)
    run_id = _started(tmp_path, env, repo, capsys, driver=driver)
    message = _brief(tmp_path, "more\n", "m.md")
    core_ = _core(env, driver)
    assert core_.execute(lambda: core_.send(run_id, message)) == 75
    err = capsys.readouterr().err
    assert err.splitlines()[-1].startswith(f"next: emb run --recover {run_id}")


def test_send_to_dead_session_is_72(
    tmp_path: Path, env: dict[str, str], repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run_id = _started(tmp_path, env, repo, capsys)
    kill_sessions(tmp_path)
    folder = session_folder(tmp_path, run_id)
    deadline = time.monotonic() + 5
    while (Path("/proc") / (folder / "pid").read_text()).exists():
        assert time.monotonic() < deadline
        time.sleep(0.02)
    message = _brief(tmp_path, "more\n", "m.md")
    core_ = _core(env)
    assert core_.execute(lambda: core_.send(run_id, message)) == 72
    err = capsys.readouterr().err
    assert "has ended" in err
    status_out = _status(env, run_id, capsys)
    assert status_out["session_state"] == "dead"


class _SendFails(StubDriver):
    def __init__(self, *args: Any, **kwargs: Any) -> None:  # noqa: ANN401
        super().__init__(*args, **kwargs)
        self.failure: BaseException | None = None

    def send(self, run: record.Run, text: str) -> None:
        if self.failure is not None:
            raise self.failure
        super().send(run, text)


@pytest.mark.parametrize(
    ("failure", "code"),
    [(engines.DriverError("paste failed"), 71), (KeyboardInterrupt(), 74)],
)
def test_failed_send_keeps_hook_output_until_delivery(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    capsys: pytest.CaptureFixture[str],
    failure: BaseException,
    code: int,
) -> None:
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\necho first refusal\nexit 1\n")
    hook.chmod(0o755)
    driver = _SendFails("claude", script_path(env))
    core_ = _core(env, driver)
    assert _start(core_, repo, _brief(tmp_path, "Task\n@write y.txt y\n")) == 77
    run_id = _run_id(capsys.readouterr().out)
    run = record.load(run_id, env)
    refusal = run.folder / core.HOOK_REFUSAL_NAME
    hook.write_text("#!/bin/sh\necho refreshed refusal\nexit 1\n")
    driver.failure = failure
    message = _brief(tmp_path, "@write y.txt fixed\n", "correction.md")
    assert core_.execute(lambda: core_.send(run_id, message)) == code
    assert refusal.read_text() == "refreshed refusal\n"
    after = record.load(run_id, env)
    assert after.turn == run.turn
    assert after.turn_state == record.COLLECTED
    assert after.expected_head == run.expected_head
    driver.failure = None
    assert core_.execute(lambda: core_.send(run_id, message)) == 77
    sent = [event["text"] for event in _events(tmp_path, run_id) if "text" in event][-1]
    assert "refreshed refusal\n```\n\n@write y.txt fixed\n" in sent
    assert record.load(run_id, env).turn == 2
    hook.unlink()
    assert core_.execute(lambda: core_.recover(run_id)) == 0
    assert not refusal.exists()


@pytest.mark.parametrize(
    ("output", "tail", "cut", "fence"),
    [
        (
            "\n".join(map(str, range(205))),
            "\n".join(map(str, range(5, 205))),
            True,
            "```",
        ),
        ("a" * 70000, "a" * 65536, True, "```"),
        ("\u20ac" * 30000, "\u20ac" * (65536 // 3), True, "```"),
        ("a\n" * 201 + "z" * 70000, "z" * 65536, True, "```"),
        ("```\nstdout\n```\nstderr  \n", "```\nstdout\n```\nstderr  \n", False, "````"),
    ],
    ids=["lines", "bytes", "utf8", "both", "fences"],
)
def test_hook_output_message_limits(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    capsys: pytest.CaptureFixture[str],
    output: str,
    tail: str,
    cut: bool,
    fence: str,
) -> None:
    run_id = _started(tmp_path, env, repo, capsys)
    run = record.load(run_id, env)
    refusal = run.folder / core.HOOK_REFUSAL_NAME
    record.write_private(refusal, output)
    message = _core(env)._hook_message(run, "Correction\n")
    end = "" if tail.endswith("\n") else "\n"
    assert message.startswith(
        "The commit hook refused your last turn's commit:\n\n"
        f"{fence}\n{tail}{end}{fence}"
    )
    assert ("[Hook output cut" in message) == cut
    assert message.endswith("\n\nCorrection\n")
    assert len(tail.encode("utf-8")) <= 65536
    assert len(tail.splitlines()) <= 200
    assert refusal.read_text() == output


@pytest.mark.parametrize(
    ("failure", "code"),
    [(engines.DriverError("paste failed"), 71), (KeyboardInterrupt(), 74)],
)
def test_send_that_fails_before_delivery_restores_the_turn(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    capsys: pytest.CaptureFixture[str],
    failure: BaseException,
    code: int,
) -> None:
    driver = _SendFails("claude", script_path(env))
    run_id = _started(tmp_path, env, repo, capsys, driver=driver)
    before = record.load(run_id, env)
    driver.failure = failure
    message = _brief(tmp_path, "@write n.txt n\n", "m.md")
    core_ = _core(env, driver)
    assert core_.execute(lambda: core_.send(run_id, message)) == code
    err = capsys.readouterr().err
    assert err.splitlines()[-1] == (
        f"next: emb run --send {run_id} --message-file <file> "
        "(the message was not sent)"
    )
    run = record.load(run_id, env)
    assert (run.status, run.turn, run.turn_state, run.reply, run.expected_head) == (
        before.status,
        before.turn,
        before.turn_state,
        before.reply,
        before.expected_head,
    )
    assert run.last_exit == code
    driver.failure = None
    assert core_.execute(lambda: core_.send(run_id, message)) == 0
    assert record.load(run_id, env).turn == 2


def test_send_first_commits_a_collected_turn(
    tmp_path: Path, env: dict[str, str], repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run_id = _started(tmp_path, env, repo, capsys)
    run = record.load(run_id, env)
    run = record.update(run, {"turn_state": record.COLLECTED})
    Path(run.worktree, "left.txt").write_text("left\n")
    message = _brief(tmp_path, "@write next.txt n\n", "m.md")
    core_ = _core(env)
    assert core_.execute(lambda: core_.send(run_id, message)) == 0
    run = record.load(run_id, env)
    assert len(run.commits) == 2
    subjects = git(Path(run.worktree), "log", "--format=%s", env=env).splitlines()
    assert subjects[:2] == ["Task (turn 2)", "Task"]


FOREIGN = "1 message(s) in the session were not sent by Ember"


def test_send_refuses_a_session_holding_foreign_messages_before_committing(
    tmp_path: Path, env: dict[str, str], repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    driver = _driver(env)
    run_id = _started(tmp_path, env, repo, capsys, driver=driver)
    run = record.update(record.load(run_id, env), {"turn_state": record.COLLECTED})
    Path(run.worktree, "left.txt").write_text("left\n")
    driver.foreign = FOREIGN
    message = _brief(tmp_path, "@write next.txt n\n", "m.md")
    core_ = _core(env, driver)
    assert core_.execute(lambda: core_.send(run_id, message)) == 75
    err = capsys.readouterr().err.splitlines()
    assert err[0] == (
        f"emb run: the session of {run_id} holds messages Ember did not send "
        f"({FOREIGN}); Claude asks the owner before going on"
    )
    assert err[1] == (
        f"next: ask the owner about the messages in the session of {run_id}; "
        "with their go-ahead, repeat the command with --force"
    )
    after = record.load(run_id, env)
    assert (after.turn, after.turn_state, after.commits) == (
        1,
        record.COLLECTED,
        run.commits,
    )
    assert core_.execute(lambda: core_.send(run_id, message, force=True)) == 0
    after = record.load(run_id, env)
    assert after.turn == 2
    assert len(after.commits) == 2
    assert driver.foreign is None


def test_send_with_force_keeps_the_busy_check(
    tmp_path: Path, env: dict[str, str], repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    driver = _driver(env, idle=engines.BUSY, foreign=FOREIGN)
    run_id = _started(tmp_path, env, repo, capsys, driver=driver)
    message = _brief(tmp_path, "more\n", "m.md")
    core_ = _core(env, driver)
    assert core_.execute(lambda: core_.send(run_id, message, force=True)) == 75
    err = capsys.readouterr().err
    assert err.splitlines()[-1].startswith(f"next: emb run --recover {run_id}")
    assert driver.foreign_checks == 0


def test_close_refuses_a_session_holding_foreign_messages_unless_forced(
    tmp_path: Path, env: dict[str, str], repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    driver = _driver(env, foreign=FOREIGN)
    run_id = _started(tmp_path, env, repo, capsys, driver=driver)
    core_ = _core(env, driver)
    assert core_.execute(lambda: core_.close(run_id)) == 75
    assert "repeat the command with --force" in capsys.readouterr().err
    assert record.load(run_id, env).status != record.CLOSED
    assert driver.closed == []
    assert core_.execute(lambda: core_.close(run_id, force=True)) == 0
    assert record.load(run_id, env).status == record.CLOSED


def test_recover_adopts_a_commit_it_made_but_did_not_record(
    tmp_path: Path, env: dict[str, str], repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run_id = _started(tmp_path, env, repo, capsys)
    run = record.update(record.load(run_id, env), {"turn_state": record.COLLECTED})
    worktree = Path(run.worktree)
    (worktree / "z.txt").write_text("z\n")
    git(worktree, "add", "-A", env=env)
    git(worktree, "commit", "-q", "-m", "Task", env=env)
    head = git(worktree, "rev-parse", "HEAD", env=env)
    core_ = _core(env)
    assert core_.execute(lambda: core_.recover(run_id)) == 0
    run = record.load(run_id, env)
    assert run.commits == (head,)
    assert run.turn_state == record.COMMITTED
    assert " · 1 files · turn 1" in capsys.readouterr().out


def test_recover_with_nothing_to_collect(
    tmp_path: Path, env: dict[str, str], repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run_id = _started(tmp_path, env, repo, capsys)
    core_ = _core(env)
    assert core_.execute(lambda: core_.recover(run_id)) == 0
    assert "nothing to recover" in capsys.readouterr().err


def test_unknown_run_is_64(
    env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    core_ = _core(env)
    assert core_.execute(lambda: core_.status("claude-none-000000")) == 64
    err = capsys.readouterr().err
    assert err.splitlines() == [
        "emb run: no run claude-none-000000",
        "next: emb run --status",
    ]


def test_screen_interrupt_and_attach(
    tmp_path: Path, env: dict[str, str], repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    choices.set_settings({"terminal": "true"}, env=env)
    run_id = _started(tmp_path, env, repo, capsys)
    core_ = _core(env)
    assert core_.execute(lambda: core_.screen(run_id)) == 0
    assert capsys.readouterr().out == "turn 1 done\n"
    assert core_.execute(lambda: core_.interrupt(run_id)) == 0
    assert _events(tmp_path, run_id)[-1] == {"key": "C-c"}
    assert core_.execute(lambda: core_.attach(run_id)) == 0
    assert capsys.readouterr().out.startswith("window ")


def test_close_keeps_worktree_and_branch(
    tmp_path: Path, env: dict[str, str], repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    driver = _driver(env)
    run_id = _started(tmp_path, env, repo, capsys, driver=driver)
    core_ = _core(env, driver)
    assert core_.execute(lambda: core_.close(run_id)) == 0
    assert capsys.readouterr().out == f"closed {run_id}\n"
    run = record.load(run_id, env)
    assert run.status == record.CLOSED
    assert run.ended_at is not None
    assert driver.closed == [run_id]
    assert not session_folder(tmp_path, run_id).exists()
    assert Path(run.worktree).is_dir()
    assert git(repo, "branch", "--list", run.branch or "", env=env)
    assert core_.execute(lambda: core_.close(run_id)) == 0
    message = _brief(tmp_path, "more\n", "m.md")
    assert core_.execute(lambda: core_.send(run_id, message)) == 72


def test_close_ends_a_process_the_worker_detached(
    tmp_path: Path, env: dict[str, str], repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    marker = tmp_path / "detached.pid"
    run_id = _started(tmp_path, env, repo, capsys, f"Task\n@detach {marker}\n")
    pid = int(marker.read_text())
    child = processes.tree(pid)
    assert processes.running(child[0])
    core_ = _core(env)
    assert core_.execute(lambda: core_.close(run_id)) == 0
    out, err = capsys.readouterr()
    assert out == f"closed {run_id}\n"
    assert err == ""
    assert not processes.running(child[0])


def test_close_refused_while_a_turn_is_collected_but_not_committed(
    tmp_path: Path, env: dict[str, str], repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run_id = _started(tmp_path, env, repo, capsys)
    run = record.update(record.load(run_id, env), {"turn_state": record.COLLECTED})
    Path(run.worktree, "late.txt").write_text("late\n")
    core_ = _core(env)
    assert core_.execute(lambda: core_.close(run_id)) == 75
    err = capsys.readouterr().err
    assert err.splitlines()[-1] == (
        f"next: emb run --recover {run_id} (commits the collected turn), "
        f"then emb run --close {run_id}"
    )
    assert core_.execute(lambda: core_.recover(run_id)) == 0
    assert len(record.load(run_id, env).commits) == 1
    assert core_.execute(lambda: core_.close(run_id)) == 0
    assert record.load(run_id, env).status == record.CLOSED


def test_close_refused_while_turn_runs_unless_forced(
    tmp_path: Path, env: dict[str, str], repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = _start(_core(env, stall=0.01), repo, _brief(tmp_path, "Task\n@hang\n"))
    out, _ = capsys.readouterr()
    assert code == 124
    run_id = _run_id(out)
    core_ = _core(env)
    assert core_.execute(lambda: core_.close(run_id)) == 75
    err = capsys.readouterr().err
    assert err.splitlines()[-1].endswith(f"or emb run --close {run_id} --force")
    assert core_.execute(lambda: core_.close(run_id, force=True)) == 0
    assert record.load(run_id, env).status == record.CLOSED


def _status(
    env: dict[str, str], run_id: str | None, capsys: pytest.CaptureFixture[str]
) -> Any:  # noqa: ANN401
    core_ = _core(env)
    assert core_.execute(lambda: core_.status(run_id, as_json=True)) == 0
    return json.loads(capsys.readouterr().out)


def test_status_one_and_all(
    tmp_path: Path, env: dict[str, str], repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run_id = _started(tmp_path, env, repo, capsys)
    one = _status(env, run_id, capsys)
    assert one["id"] == run_id
    assert one["session_state"] == "live"
    assert one["status"] == "waiting"
    assert one["step"] == "close"
    assert one["next"] == f"next: emb run --close {run_id}"
    assert len(one["turns"]) == 1
    assert one["turns"][0]["turn"] == 1
    assert one["turns"][0]["exit"] == 0
    broken = paths.runs_dir(env) / "claude-broken-000000"
    broken.mkdir()
    (broken / "run.json").write_text("{")
    core_ = _core(env)
    assert core_.execute(lambda: core_.status(None, as_json=True)) == 0
    out, err = capsys.readouterr()
    listing = json.loads(out)
    assert [item["id"] for item in listing] == [run_id]
    assert listing[0]["turns"] == one["turns"]
    assert listing[0]["step"] == one["step"]
    assert listing[0]["next"] == one["next"]
    assert "skipped claude-broken-000000: run.json is not valid JSON" in err
    _core(env).close(run_id, force=True)
    capsys.readouterr()
    assert _status(env, run_id, capsys)["session_state"] == "missing"


@pytest.mark.parametrize("read_only", [False, True])
def test_status_keeps_failed_turn_before_success(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    capsys: pytest.CaptureFixture[str],
    read_only: bool,
) -> None:
    core_ = _core(env)
    assert (
        _start(
            core_,
            repo,
            _brief(tmp_path, "Task\n@provider rate limited\n"),
            read_only=read_only,
        )
        == 73
    )
    run_id = _run_id(capsys.readouterr().out)
    message = _brief(tmp_path, "@reply done\n", "next.md")
    assert core_.execute(lambda: core_.send(run_id, message)) == 0
    capsys.readouterr()
    result = _status(env, run_id, capsys)
    assert result["last_exit"] == 0
    first, second = result["turns"]
    assert first == {
        "turn": 1,
        "exit": 73,
        "state": "failed",
        "reason": "rate limited",
        "provider_kind": "outage",
        "attempts": first["attempts"],
        "provider_error": True,
        "signed_out": False,
        "incomplete": "provider error",
        "seconds": first["seconds"],
        "denied_paths": [],
    }
    assert second == {
        "turn": 2,
        "exit": 0,
        "provider_kind": None,
        "attempts": second["attempts"],
        "state": "done",
        "reason": None,
        "provider_error": False,
        "signed_out": False,
        "incomplete": None,
        "seconds": second["seconds"],
        "denied_paths": [],
    }
    assert first["seconds"] > 0
    assert second["seconds"] > 0
    assert _status(env, None, capsys)[0]["turns"] == result["turns"]


@pytest.mark.parametrize("all_runs", [False, True])
@pytest.mark.parametrize("broken", [None, b"{", b"\xff", b"[]"])
def test_status_warns_about_broken_outcome(
    tmp_path: Path,
    env: dict[str, str],
    capsys: pytest.CaptureFixture[str],
    all_runs: bool,
    broken: bytes | None,
) -> None:
    run = _bare_run(env, tmp_path, "claude", "none")
    run = replace(run, folder=record.make_run_dir(run.id, env), turn=3)
    record.save(run)
    good = {
        "state": "done",
        "reason": None,
        "provider_error": False,
        "signed_out": False,
        "incomplete": None,
    }
    for turn in (1, 3):
        record.outcome_path(run, turn).write_text(json.dumps(good))
    path = record.outcome_path(run, 2)
    if broken is not None:
        path.write_bytes(broken)
    core_ = _core(env)
    assert (
        core_.execute(lambda: core_.status(None if all_runs else run.id, as_json=True))
        == 0
    )
    out, err = capsys.readouterr()
    result = json.loads(out)
    if all_runs:
        result = result[0]
    assert result["turns"] == [
        {"turn": 1, "exit": None, **good, "seconds": None, "denied_paths": []},
        {"turn": 2, **dict.fromkeys(("exit", "seconds", "denied_paths", *good))},
        {"turn": 3, "exit": None, **good, "seconds": None, "denied_paths": []},
    ]
    assert len(err.splitlines()) == 1
    assert err.startswith("WARNING: ")
    assert str(path) in err


def test_status_every_run_includes_empty_turns(
    tmp_path: Path, env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert _status(env, None, capsys) == []
    for engine in ("claude", "codex"):
        run = _bare_run(env, tmp_path, engine, "none")
        record.save(replace(run, folder=record.make_run_dir(run.id, env)))
    assert [run["turns"] for run in _status(env, None, capsys)] == [[], []]


def test_status_text(
    tmp_path: Path, env: dict[str, str], repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run_id = _started(tmp_path, env, repo, capsys)
    core_ = _core(env)
    assert core_.execute(lambda: core_.status(None)) == 0
    assert capsys.readouterr().out == f"{run_id}  waiting  live  turn 1  Task\n"
    assert core_.execute(lambda: core_.status(run_id)) == 0
    out, err = capsys.readouterr()
    assert f"id: {run_id}\n" in out
    assert "session_state: live\n" in out
    assert "step: close\n" in out
    assert "next:" not in out and "next:" not in err


def test_run_error_carries_context() -> None:
    error = RunError(75, "busy", "close", run="r")
    assert (error.code, str(error), error.variant, error.context) == (
        75,
        "busy",
        "close",
        {"run": "r"},
    )


def test_subject_helpers() -> None:
    assert core.brief_subject("\n  \nFirst line  \nsecond") == "First line"
    assert core.brief_subject("") == ""
    assert core.commit_subject("a" * 80, 1) == "a" * 72
    assert core.commit_subject("abc", 3) == "abc (turn 3)"
    assert core.commit_subject("a" * 80, 1, "no reply") == (
        "a" * 72 + " (turn 1, incomplete: no reply)"
    )
    done = engines.TurnOutcome(engines.DONE, reply="ok")
    assert core.incomplete_reason(done) is None
    assert core.incomplete_reason(replace(done, reply="")) == "no reply"
    failed = engines.TurnOutcome(engines.FAILED, signed_out=True)
    assert core.incomplete_reason(failed) == "sign-in failure"
    assert core.first_message("r", "brief") == (
        "Brief from Claude, the lead, for Ember run r.\n\nbrief"
    )


def test_read_only_tree_ignores_source_edits_during_turn(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def edit_source(run: record.Run) -> None:
        (repo / "README.md").write_text("lead edit\n")
        (repo / "lead.txt").write_text("lead work\n")
        assert Path(run.worktree, "README.md").read_text() == "hello\n"
        assert not Path(run.worktree, "lead.txt").exists()

    driver = _driver(env, on_wait=edit_source)
    assert (
        _start(
            _core(env, driver),
            repo,
            _brief(tmp_path, "Review\n@reply fine\n"),
            read_only=True,
        )
        == 0
    )
    out, err = capsys.readouterr()
    run = record.load(_run_id(out), env)
    assert run.last_reason is None
    assert f"read-only run {run.id} changed" not in err
    assert (repo / "README.md").read_text() == "lead edit\n"


@pytest.mark.parametrize("force", [False, True])
@pytest.mark.parametrize("dirty", [False, True])
def test_close_detached_worktree(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    capsys: pytest.CaptureFixture[str],
    force: bool,
    dirty: bool,
) -> None:
    core_ = _core(env)
    assert _start(core_, repo, _brief(tmp_path, "Review\n"), read_only=True) == 0
    run = record.load(_run_id(capsys.readouterr().out), env)
    worktree = Path(run.worktree)
    if dirty:
        (worktree / "keep.txt").write_text("worker evidence\n")
    assert core_.execute(lambda: core_.close(run.id, force=force)) == 0
    out, err = capsys.readouterr()
    assert out == f"closed {run.id}\n"
    assert record.load(run.id, env).status == record.CLOSED
    assert record.load(run.id, env).worktree == str(worktree)
    assert worktree.exists() == dirty
    listing = git(repo, "worktree", "list", "--porcelain", env=env)
    assert (str(worktree) in listing) == dirty
    if dirty:
        assert (worktree / "keep.txt").read_text() == "worker evidence\n"
        assert f"git worktree remove --force {worktree}" in err
    else:
        assert not err
    assert core_.execute(lambda: core_.close(run.id, force=True)) == 0
    assert worktree.exists() == dirty


def test_close_legacy_read_only_run_keeps_source_checkout(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    core_ = _core(env)
    assert _start(core_, repo, _brief(tmp_path, "Review\n"), read_only=True) == 0
    run = record.load(_run_id(capsys.readouterr().out), env)
    document = run.to_json()
    document.pop("detached")
    document["worktree"] = str(repo)
    (run.folder / record.RECORD_NAME).write_text(json.dumps(document))
    assert core_.execute(lambda: core_.close(run.id)) == 0
    assert (repo / "README.md").read_text() == "hello\n"


@pytest.mark.parametrize("dry_run", [False, True])
def test_read_only_start_without_commit_is_70(
    tmp_path: Path,
    env: dict[str, str],
    capsys: pytest.CaptureFixture[str],
    dry_run: bool,
) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    git(empty, "init", "-q", env=env)
    assert (
        _start(
            _core(env), empty, _brief(tmp_path, "Review\n"),
            read_only=True, dry_run=dry_run,
        )
        == 70
    )  # fmt: skip
    assert "has no commit to start from" in capsys.readouterr().err
    assert not paths.runs_dir(env).exists()


def test_close_keeps_a_commit_made_in_a_detached_worktree(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    core_ = _core(env)
    assert _start(core_, repo, _brief(tmp_path, "Review\n"), read_only=True) == 0
    saved = record.load(_run_id(capsys.readouterr().out), env)
    folder = Path(saved.worktree)
    git(folder, "commit", "-q", "--allow-empty", "-m", "worker evidence", env=env)
    assert core_.close(saved.id, force=True) == 0
    assert folder.exists()
    assert git(folder, "log", "-1", "--format=%s", env=env) == "worker evidence"
    assert f"git worktree remove --force {folder}" in capsys.readouterr().err


@pytest.mark.parametrize("waiting_ready", [False, True])
def test_wait_loops_persist_screen_changes_only(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    waiting_ready: bool,
) -> None:
    core_ = _core(env)
    assert _start(core_, repo, _brief(tmp_path, "Task\n@reply done\n")) == 0
    run_id = _run_id(capsys.readouterr().out)
    saved = record.update(record.load(run_id, env), {"last_screen_change_at": None})
    screens = [None, "first screen", "first screen", "changed screen"]
    expected = [None, None, 101.0, 101.0]
    index = 0
    now = 100.0

    class ObservedDriver(StubDriver):
        def ready(self, run: record.Run) -> bool:
            return index == len(screens)

        def wait(self, run: record.Run) -> engines.TurnOutcome:
            return engines.TurnOutcome(
                engines.DONE if index == len(screens) else engines.RUNNING
            )

    def capture(run_id: str, previous: str | None) -> str | None:
        nonlocal index, now
        assert record.load(run_id, env).last_screen_change_at == expected[index]
        now = 100.0 + index
        screen = screens[index]
        index += 1
        return screen

    monkeypatch.setattr(core_, "_screen_text", capture)
    monkeypatch.setattr(core_, "_session_state", lambda run_id: core.LIVE)
    monkeypatch.setattr(core.time, "time", lambda: now)
    monkeypatch.setattr(core.time, "sleep", lambda seconds: None)
    driver = ObservedDriver("claude", script_path(env))
    if waiting_ready:
        core_._wait_ready(saved, driver)
    else:
        assert core_._wait_turn(saved, driver).state == engines.DONE
    latest = record.load(run_id, env)
    assert latest.last_screen_change_at == 103.0
    assert latest.status == saved.status
    assert latest.turn_state == saved.turn_state


class _RetryClock:
    def __init__(self, driver: _ProviderDriver) -> None:
        self.driver = driver
        self.elapsed = 0.0
        self.on_sleep: Callable[[], None] | None = None

    def time(self) -> float:
        return time.time() + self.elapsed

    def monotonic(self) -> float:
        return time.monotonic() + self.elapsed

    def sleep(self, seconds: float) -> None:
        if not self.driver.messages:
            time.sleep(seconds)
            return
        if self.on_sleep is not None:
            self.on_sleep()
        self.elapsed += seconds


class _ProviderDriver(StubDriver):
    def __init__(self, env: dict[str, str], kinds: list[str | None]) -> None:
        super().__init__("codex", script_path(env))
        self.kinds = kinds
        self.messages: list[tuple[record.Run, str]] = []
        self.on_send: Callable[[], None] | None = None
        self.on_observe: Callable[[], None] | None = None

    def send(self, run: record.Run, text: str) -> None:
        if self.messages:
            saved = record.load_outcome(run, run.turn)
            assert len(saved["attempts"]) == len(self.messages) + 1
            pending = saved["attempts"][-1]
            assert pending["id"] and pending["sending"] and not pending["sent"]
            if self.on_send is not None:
                self.on_send()
        self.messages.append((run, text))

    def wait(self, run: record.Run) -> engines.TurnOutcome:
        if self.on_observe is not None:
            self.on_observe()
        number = len(self.messages)
        Path(run.worktree, f"part-{number}.txt").write_text(f"attempt {number}")
        kind = self.kinds[min(number - 1, len(self.kinds) - 1)]
        if kind == "done":
            return engines.TurnOutcome(engines.DONE, reply="done")
        return engines.TurnOutcome(
            engines.FAILED,
            reason="provider refused",
            provider_error=True,
            provider_kind=kind,
        )


class _RetryCrash(BaseException):
    pass


@pytest.mark.parametrize(
    ("kinds", "retries", "expected", "sends"),
    [
        (["capacity", "done"], "2", 0, 2),
        (["capacity"], "0", 73, 1),
        (["capacity"], "2", 73, 3),
        (["usage_limit"], "2", 73, 1),
        (["outage"], "2", 73, 1),
        ([None], "2", 73, 1),
    ],
)
def test_provider_retry_policy_commits_all_attempts_once(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    kinds: list[str | None],
    retries: str,
    expected: int,
    sends: int,
) -> None:
    choices.set_settings(
        {"provider.capacity_retries": retries, "provider.capacity_wait": "5"}, env=env
    )
    driver = _ProviderDriver(env, kinds)
    clock = _RetryClock(driver)
    monkeypatch.setattr(core, "time", clock)
    core_ = _core(env, driver)
    assert (
        _start(core_, repo, _brief(tmp_path, "Task"), engine="codex", model="gpt-5")
        == expected
    )
    out, err = capsys.readouterr()
    run = record.load(_run_id(out), env)
    saved = record.load_outcome(run, 1)
    assert len(driver.messages) == sends
    assert len({text for _, text in driver.messages}) == 1
    assert {(r.model, r.effort, r.turn) for r, _ in driver.messages} == {
        (run.model, run.effort, 1)
    }
    assert len(run.commits) == 1
    assert {p.name for p in Path(run.worktree).glob("part-*.txt")} == {
        f"part-{n}.txt" for n in range(1, sends + 1)
    }
    attempts = saved["attempts"]
    assert len(attempts) == sends
    assert len({attempt["id"] for attempt in attempts}) == sends
    assert all(a["sent"] and not a["sending"] for a in attempts)
    assert [a["result"] for a in attempts] == [
        "done" if kind == "done" else "failed" for kind in (kinds * sends)[:sends]
    ]
    assert saved["seconds"] >= 5 * (sends - 1)
    assert sum(a["seconds"] for a in attempts) < saved["seconds"]
    assert err.count("at capacity; retry") == sends - 1
    for number in range(1, sends):
        assert (
            f"WARNING: codex {run.model} at capacity; retry {number} of {retries} in 5s"
        ) in err
    if expected:
        assert (kinds[-1] or "outage") in err.split("next:")[-1]


def test_recovery_keeps_pending_retry_budget_settings_and_wait_seconds(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    choices.set_settings({"provider.capacity_wait": "5"}, env=env)
    driver = _ProviderDriver(env, ["capacity"])
    clock = _RetryClock(driver)
    monkeypatch.setattr(core, "time", clock)
    core_ = _core(env, driver)

    def crash() -> None:
        raise _RetryCrash

    clock.on_sleep = crash
    with pytest.raises(_RetryCrash):
        _start(core_, repo, _brief(tmp_path, "Task"), engine="codex", model="gpt-5")
    run = record.load(_run_id(capsys.readouterr().out), env)
    before = record.load_outcome(run, 1)
    assert len(before["attempts"]) == 2
    assert not before["attempts"][-1]["sent"]
    assert not before["attempts"][-1]["sending"]
    clock.elapsed += 40
    clock.on_sleep = None
    choices.set_settings(
        {"provider.capacity_retries": "0", "provider.capacity_wait": "600"}, env=env
    )
    assert _core(env, driver).execute(lambda: _core(env, driver).recover(run.id)) == 73
    saved = record.load_outcome(run, 1)
    assert len(driver.messages) == 3
    assert len(saved["attempts"]) == 3
    assert saved["attempts"][1]["id"] == before["attempts"][1]["id"]
    assert saved["seconds"] >= before["seconds"] + 45
    assert saved["seconds"] > sum(a["seconds"] for a in saved["attempts"]) + 44
    frozen = record.load(run.id, env)
    assert (frozen.capacity_retries, frozen.capacity_wait) == (2, 5)
    assert len(frozen.commits) == 1
    assert "retry 2 of 2 in 5s" in capsys.readouterr().err
    # Later turns keep this run's original settings too.
    driver.messages.clear()
    driver.kinds = ["capacity", "done"]
    driver.idle = engines.IDLE
    message = _brief(tmp_path, "Follow up", "follow-up.md")
    assert core_.execute(lambda: core_.send(run.id, message)) == 0
    assert len(driver.messages) == 2
    assert all(r.turn == 2 for r, _ in driver.messages)
    assert [text for _, text in driver.messages] == ["Follow up", "Follow up"]
    assert record.load_outcome(run, 2)["seconds"] >= 5
    assert "retry 1 of 2 in 5s" in capsys.readouterr().err
    # The next new run reads the changed Team values.
    next_driver = _ProviderDriver(env, ["capacity"])
    monkeypatch.setattr(core, "time", _RetryClock(next_driver))
    assert (
        _start(
            _core(env, next_driver),
            repo,
            _brief(tmp_path, "Next", "next.md"),
            engine="codex",
            model="gpt-5",
        )
        == 73
    )
    next_run = record.load(_run_id(capsys.readouterr().out), env)
    assert (next_run.capacity_retries, next_run.capacity_wait) == (0, 600)


@pytest.mark.parametrize("external", [False, True])
def test_interrupt_cancels_a_pending_capacity_resend(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    external: bool,
) -> None:
    driver = _ProviderDriver(env, ["capacity", "done"])
    clock = _RetryClock(driver)
    monkeypatch.setattr(core, "time", clock)
    core_ = _core(env, driver)

    def interrupt() -> None:
        if external:
            clock.on_sleep = None
            assert _core(env).interrupt(driver.messages[0][0].id) == 0
        else:
            raise KeyboardInterrupt

    clock.on_sleep = interrupt
    assert _start(
        core_, repo, _brief(tmp_path, "Task"), engine="codex", model="gpt-5"
    ) == (73 if external else 74)
    run = record.load(_run_id(capsys.readouterr().out), env)
    clock.on_sleep = None
    assert core_.execute(lambda: core_.recover(run.id)) == (0 if external else 73)
    assert len(driver.messages) == 1
    assert not record.load_outcome(run, 1)["attempts"][-1]["sent"]


@pytest.mark.parametrize("failure", ["driver", "crash", "interrupt"])
def test_uncertain_capacity_delivery_is_never_resent_on_recovery(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: str,
) -> None:
    driver = _ProviderDriver(env, ["capacity", "done"])
    clock = _RetryClock(driver)
    monkeypatch.setattr(core, "time", clock)
    core_ = _core(env, driver)

    def fail() -> None:
        if failure == "crash":
            raise _RetryCrash
        if failure == "interrupt":
            raise KeyboardInterrupt
        raise engines.DriverError("transport disconnected")

    driver.on_send = fail
    if failure == "crash":
        with pytest.raises(_RetryCrash):
            _start(core_, repo, _brief(tmp_path, "Task"), engine="codex", model="gpt-5")
    else:
        assert _start(
            core_, repo, _brief(tmp_path, "Task"), engine="codex", model="gpt-5"
        ) == (74 if failure == "interrupt" else 73)
    output = capsys.readouterr()
    run = record.load(_run_id(output.out), env)
    driver.on_send = None
    assert core_.execute(lambda: core_.recover(run.id)) == 73
    error = output.err + capsys.readouterr().err
    assert "next: inspect the session" in error
    assert "delivery is uncertain" in error
    assert record.load(run.id, env).commits == ()
    assert Path(run.worktree, "part-1.txt").read_text() == "attempt 1"
    assert len(driver.messages) == 1
    saved = record.load_outcome(run, 1)
    assert saved["attempts"][-1]["sending"]
    assert not saved["attempts"][-1]["sent"]
    core.tmux.kill(run.id, env=env)
    record.update(run, {"last_reason": None})
    assert core_.execute(lambda: core_.close(run.id)) == 75
    assert "delivery is uncertain" in capsys.readouterr().err
    assert record.load(run.id, env).status == record.FAILED
    assert core_.close(run.id, force=True) == 0


def test_recovery_observes_a_confirmed_retry_without_sending_again(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    driver = _ProviderDriver(env, ["capacity", "done"])
    clock = _RetryClock(driver)
    monkeypatch.setattr(core, "time", clock)
    core_ = _core(env, driver)

    def crash() -> None:
        if len(driver.messages) == 2:
            raise _RetryCrash

    driver.on_observe = crash
    with pytest.raises(_RetryCrash):
        _start(core_, repo, _brief(tmp_path, "Task"), engine="codex", model="gpt-5")
    run = record.load(_run_id(capsys.readouterr().out), env)
    before = record.load_outcome(run, 1)
    driver.on_observe = None
    assert core_.execute(lambda: core_.recover(run.id)) == 0
    after = record.load_outcome(run, 1)
    assert after["attempts"][-1]["id"] == before["attempts"][-1]["id"]
    assert len(driver.messages) == 2
    assert after["seconds"] >= before["seconds"]
    assert after["exit"] == 0


def test_corrupt_retry_journal_does_not_reset_the_budget(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    driver = _ProviderDriver(env, ["capacity", "done"])
    clock = _RetryClock(driver)
    monkeypatch.setattr(core, "time", clock)
    core_ = _core(env, driver)

    def crash() -> None:
        raise _RetryCrash

    clock.on_sleep = crash
    with pytest.raises(_RetryCrash):
        _start(core_, repo, _brief(tmp_path, "Task"), engine="codex", model="gpt-5")
    run = record.load(_run_id(capsys.readouterr().out), env)
    path = record.outcome_path(run, 1)
    saved = json.loads(path.read_text())
    saved["attempts"][-1]["sent"] = "unknown"
    path.write_text(json.dumps(saved))
    before = path.read_bytes()
    clock.on_sleep = None
    assert core_.execute(lambda: core_.recover(run.id)) == 71
    assert path.read_bytes() == before
    assert len(driver.messages) == 1


@pytest.mark.parametrize("location", ["hidden", "ssh", "config"])
@pytest.mark.parametrize("result", ["success", "worker_failure", "commit_failure"])
def test_sandbox_denial_is_recorded_warned_and_allowed_on_next_launch(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    location: str,
    result: str,
) -> None:
    """Report denial facts and preserve the owner's explicit read setting."""
    if location == "config":
        config = Path(env["HOME"]) / ".config"
        Path(env["XDG_CONFIG_HOME"]).rename(config)
        env["XDG_CONFIG_HOME"] = str(config)
    secret = location != "hidden"
    folder = {
        "hidden": Path(env["HOME"]) / ".toolchain, version",
        "ssh": Path(env["HOME"]) / ".ssh",
        "config": Path(env["XDG_CONFIG_HOME"]) / "gh",
    }[location]
    folder.mkdir()
    monkeypatch.setattr(sandbox, "require", lambda *args, **kwargs: None)
    choices.set_settings({"sandbox.claude": "on"}, env=env)
    text = f"Permission denied: '{folder}/python'"
    command = "@fail" if result == "worker_failure" else "@reply"
    brief = _brief(tmp_path, f"Task\n@write result.txt done\n{command} {text}\n")
    if result == "commit_failure":
        hook = repo / ".git" / "hooks" / "pre-commit"
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
    expected = {"success": 0, "worker_failure": 71, "commit_failure": 77}[result]
    core_ = _core(env)
    assert _start(core_, repo, brief) == expected
    out, err = capsys.readouterr()
    run = record.load(_run_id(out), env)
    item = {
        "path": str(folder),
        "reason": "secret location" if secret else "hidden",
        "allow_path": None if secret else str(folder),
    }
    assert record.load_outcome(run, 1)["denied_paths"] == [item]
    assert err.count(f"WARNING: {run.id} was denied {folder}") == 1
    assert record.load_outcome(run, 1)["exit"] == expected
    owners = [line for line in err.splitlines() if line.startswith("owner:")]
    assert len(owners) == 1
    owner = owners[0]
    assert run.owner_wait == (owner,)
    assert core_.printed_owner == {owner}
    assert run.last_exit == expected
    assert core_.step(run) == ("owner", None)
    assert "next:" not in err
    qualification = (
        "the turn finished (exit 0); read access is needed only if "
        "the worker's report says the work needed the path; "
        if expected == 0
        else ""
    )
    notice = (
        f"Overview offers no Allow reading for {folder}; "
        "folders inside $XDG_CONFIG_HOME are never offered; "
        if secret
        else ""
    )
    assert owner.startswith(
        f"owner: {notice}{qualification}Settings, Sandbox read paths; "
        "emb choices --set "
    )
    command = shlex.split(owner.rsplit("; ", 1)[1])
    key, value = command[-1].split("=", 1)
    assert key == "sandbox.read"
    assert choices.parse_value(key, value) == [str(folder)]
    assert ("(secret location)" in err) is secret
    assert _status(env, run.id, capsys)["turns"][0]["denied_paths"] == [item]
    # A permission change applies to the next launch, never the saved snapshot.
    choices.set_settings({"sandbox.read": [str(folder)]}, env=env)
    assert sandbox.denied_paths(text, run.sandbox_paths) == [item]
    assert core_.step(run)[0] != "owner"
    assert record.load(run.id, env).owner_wait == ()
    assert _start(core_, repo, brief) == expected
    out, err = capsys.readouterr()
    next_run = record.load(_run_id(out), env)
    assert record.load_outcome(next_run, 1)["denied_paths"] == []
    assert "was denied" not in err
    assert sandbox.denied_paths(text, next_run.sandbox_paths) == []


@pytest.mark.parametrize("source", ["read", "tracked"])
def test_diagnostic_the_worker_was_given_is_not_a_denial(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    source: str,
) -> None:
    """A denial line read from a supplied file is not recorded or held."""
    folder = Path(env["HOME"]) / ".toolchain"
    folder.mkdir()
    monkeypatch.setattr(sandbox, "require", lambda *args, **kwargs: None)
    choices.set_settings({"sandbox.claude": "on"}, env=env)
    line = f"touch: setting times of '{folder}/list': Permission denied"
    read: tuple[str, ...] = ()
    if source == "read":
        evidence = tmp_path / "evidence"
        evidence.mkdir()
        (evidence / "notes.md").write_text(f"# Notes\n\n    {line}\n")
        read = (str(evidence),)
    else:
        (repo / "notes.md").write_text(f"{line}\n")
        git(repo, "add", "notes.md", env=env)
        git(repo, "commit", "-q", "-m", "notes", env=env)
    brief = _brief(tmp_path, f"Task\n@reply {line}\n")
    core_ = _core(env)
    assert _start(core_, repo, brief, read=read) == 0
    out, err = capsys.readouterr()
    run = record.load(_run_id(out), env)
    assert record.load_outcome(run, 1)["denied_paths"] == []
    assert "was denied" not in err
    assert "owner:" not in err
    assert run.owner_wait == ()


@pytest.mark.parametrize("when", ["before", "during"])
def test_hidden_notice_folder_needs_no_owner_answer(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    when: str,
) -> None:
    """A folder in Settings, Hidden notices is reported but holds no run."""
    folder = Path(env["HOME"]) / ".toolchain"
    folder.mkdir()
    alias = folder.with_name("alias")
    alias.symlink_to(folder, target_is_directory=True)
    monkeypatch.setattr(sandbox, "require", lambda *args, **kwargs: None)
    choices.set_settings({"sandbox.claude": "on"}, env=env)
    if when == "before":
        choices.set_settings({"sandbox.hidden_notices": [str(alias)]}, env=env)
    brief = _brief(tmp_path, f"Task\n@reply cat: {folder}/python: Permission denied\n")
    core_ = _core(env)
    assert _start(core_, repo, brief) == 0
    out, err = capsys.readouterr()
    run = record.load(_run_id(out), env)
    item = {"path": str(folder), "reason": "hidden", "allow_path": str(folder)}
    assert record.load_outcome(run, 1)["denied_paths"] == [item]
    assert err.count(f"WARNING: {run.id} was denied {folder}") == 1
    if when == "before":
        assert "owner:" not in err
        assert run.owner_wait == ()
    else:
        assert core_.step(run)[0] == "owner"
        choices.set_settings({"sandbox.hidden_notices": [str(folder)]}, env=env)
        assert core_.step(run)[0] != "owner"
        assert record.load(run.id, env).owner_wait == ()


def test_hiding_a_child_folder_keeps_the_parent_denial_wait(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Only a hidden folder holding the denied path decides that denial."""
    folder = Path(env["HOME"]) / ".toolchain"
    (folder / "cache").mkdir(parents=True)
    monkeypatch.setattr(sandbox, "require", lambda *args, **kwargs: None)
    choices.set_settings({"sandbox.claude": "on"}, env=env)
    brief = _brief(tmp_path, f"Task\n@reply cat: {folder}/python: Permission denied\n")
    core_ = _core(env)
    assert _start(core_, repo, brief) == 0
    run = record.load(_run_id(capsys.readouterr().out), env)
    assert run.owner_denied == (str(folder.resolve()),)
    choices.set_settings({"sandbox.hidden_notices": [str(folder / "cache")]}, env=env)
    assert core_.step(run)[0] == "owner"
    assert record.load(run.id, env).owner_wait != ()


def test_hiding_a_denial_without_an_offer_ends_its_wait(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A hidden denial that offers no folder is decided by Hidden notices."""
    rc = Path(env["HOME"]) / ".toolrc"
    rc.write_text("x")
    monkeypatch.setattr(sandbox, "require", lambda *args, **kwargs: None)
    choices.set_settings({"sandbox.claude": "on"}, env=env)
    brief = _brief(tmp_path, f"Task\n@reply cat: {rc}: Permission denied\n")
    core_ = _core(env)
    assert _start(core_, repo, brief) == 0
    out, err = capsys.readouterr()
    run = record.load(_run_id(out), env)
    assert f"owner: {rc} is hidden; no folder can be offered" in err
    assert run.owner_read == ("",)
    assert core_.step(run)[0] == "owner"
    choices.set_settings({"sandbox.hidden_notices": [str(rc)]}, env=env)
    assert core_.step(run)[0] != "owner"
    assert record.load(run.id, env).owner_wait == ()


@pytest.mark.parametrize("retry_succeeds", [False, True])
def test_denials_union_all_capacity_attempts_with_one_warning_each(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    retry_succeeds: bool,
) -> None:
    folders = [Path(env["HOME"]) / name for name in (".rustup", ".pyenv")]
    for folder in folders:
        folder.mkdir()
    choices.set_settings(
        {"sandbox.codex": "on", "provider.capacity_wait": "5"}, env=env
    )
    driver = _ProviderDriver(env, ["capacity", "done" if retry_succeeds else "outage"])
    monkeypatch.setattr(
        driver,
        "turn_output",
        lambda run: "\n".join(
            f"{folder}/bin: EACCES" for folder in folders[: len(driver.messages)]
        ),
    )
    monkeypatch.setattr(core, "time", _RetryClock(driver))
    monkeypatch.setattr(sandbox, "require", lambda *args, **kwargs: None)
    core_ = _core(env, driver)
    monkeypatch.setattr(core_, "_sandboxed", lambda spec, run, common: spec)
    assert _start(
        core_, repo, _brief(tmp_path, "Task"), engine="codex", model="gpt-5"
    ) == (0 if retry_succeeds else 73)
    out, err = capsys.readouterr()
    run = record.load(_run_id(out), env)
    denials = record.load_outcome(run, 1)["denied_paths"]
    assert {item["path"] for item in denials} == {str(folder) for folder in folders}
    for folder in folders:
        assert err.count(f"WARNING: {run.id} was denied {folder}") == 1

    assert record.load_outcome(run, 1)["exit"] == (0 if retry_succeeds else 73)
    owners = [line for line in err.splitlines() if line.startswith("owner:")]
    assert len(owners) == len(folders)
    for owner in owners:
        if retry_succeeds:
            assert owner.startswith(
                "owner: the turn finished (exit 0); read access is needed only if "
                "the worker's report says the work needed the path; "
            )
        else:
            assert owner.startswith("owner: Settings, Sandbox read paths; ")


def test_protected_runtime_denial_has_no_settings_suggestion(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sandbox, "require", lambda *args, **kwargs: None)
    choices.set_settings({"sandbox.claude": "on"}, env=env)
    path = Path(env["XDG_RUNTIME_DIR"]) / "socket"
    assert (
        _start(_core(env), repo, _brief(tmp_path, f"Task\n@reply {path}: EACCES")) == 0
    )
    out, err = capsys.readouterr()
    run = record.load(_run_id(out), env)
    assert record.load_outcome(run, 1)["denied_paths"] == [
        {"path": str(path.parent), "reason": "protected", "allow_path": None}
    ]
    assert "was denied" in err and "(protected)" in err
    assert "sandbox.read=" not in err
    assert run.owner_wait == ()


@pytest.mark.parametrize("engine", ["codex", "claude", "opencode"])
def test_clean_followup_does_not_reuse_denial_left_in_pane(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    engine: str,
) -> None:
    folder = Path(env["HOME"]) / ".toolchain"
    folder.mkdir()
    pane = [""]
    error = f"cat: {folder}/python: Permission denied"

    def observed(run: record.Run) -> None:
        pane[0] = (
            f"old heading\n{error}\ndone"
            if run.turn == 1
            else f"{error}\ndone\nfollow-up succeeded"
        )

    driver = _driver(env, engine, on_wait=observed)
    monkeypatch.setattr(driver, "screen", lambda run: pane[0])
    monkeypatch.setattr(sandbox, "require", lambda *args, **kwargs: None)
    choices.set_settings({f"sandbox.{engine}": "on"}, env=env)
    core_ = _core(env, driver)
    monkeypatch.setattr(core_, "_sandboxed", lambda spec, run, common: spec)
    assert (
        _start(
            core_,
            repo,
            _brief(tmp_path, "Task\n@reply done"),
            engine=engine,
            model="model",
        )
        == 0
    )
    out, err = capsys.readouterr()
    run = record.load(_run_id(out), env)
    assert len(record.load_outcome(run, 1)["denied_paths"]) == 1
    assert f"was denied {folder}" in err
    # A new Core simulates collection in a fresh command process. The persisted
    # pre-delivery pane boundary excludes old diagnostics even after scrolling.
    following = _core(env, driver)
    message = _brief(tmp_path, "Follow up\n@reply success", "follow-up.md")
    assert following.execute(lambda: following.send(run.id, message)) == 0
    _, err = capsys.readouterr()
    assert record.load_outcome(record.load(run.id, env), 2)["denied_paths"] == []
    assert "was denied" not in err
    assert "owner: Settings" not in err
    # A repeated denial in the actual turn transcript is still a new occurrence.
    monkeypatch.setattr(
        driver, "turn_output", lambda run: error if run.turn == 3 else ""
    )
    assert following.execute(lambda: following.send(run.id, message)) == 0
    _, err = capsys.readouterr()
    assert len(record.load_outcome(record.load(run.id, env), 3)["denied_paths"]) == 1
    assert err.count(f"was denied {folder}") == 1


def test_pane_denial_union_across_retries_does_not_leak_to_next_attempt(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    folder = Path(env["HOME"]) / ".toolchain"
    folder.mkdir()
    driver = _ProviderDriver(env, ["capacity", "done"])
    pane = [""]
    driver.on_observe = lambda: pane.__setitem__(
        0, f"{folder}/python: EACCES\nattempt {len(driver.messages)}"
    )
    monkeypatch.setattr(driver, "screen", lambda run: pane[0])
    monkeypatch.setattr(core, "time", _RetryClock(driver))
    monkeypatch.setattr(sandbox, "require", lambda *args, **kwargs: None)
    choices.set_settings(
        {"sandbox.codex": "on", "provider.capacity_wait": "5"}, env=env
    )
    core_ = _core(env, driver)
    monkeypatch.setattr(core_, "_sandboxed", lambda spec, run, common: spec)
    assert (
        _start(core_, repo, _brief(tmp_path, "Task"), engine="codex", model="gpt-5")
        == 0
    )
    out, err = capsys.readouterr()
    run = record.load(_run_id(out), env)
    assert len(record.load_outcome(run, 1)["denied_paths"]) == 1
    assert err.count(f"was denied {folder}") == 1


def test_recovery_keeps_pre_delivery_pane_boundary(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    folder = Path(env["HOME"]) / ".toolchain"
    folder.mkdir()
    pane = [""]
    driver = _driver(
        env, on_wait=lambda run: pane.__setitem__(0, f"{folder}/python: EACCES")
    )
    monkeypatch.setattr(driver, "screen", lambda run: pane[0])
    monkeypatch.setattr(sandbox, "require", lambda *args, **kwargs: None)
    choices.set_settings({"sandbox.claude": "on"}, env=env)
    core_ = _core(env, driver)
    assert _start(core_, repo, _brief(tmp_path, "Task\n@reply done")) == 0
    run = record.load(_run_id(capsys.readouterr().out), env)
    followup = _core(env, driver, stall=0.003)
    assert (
        followup.execute(
            lambda: followup.send(
                run.id, _brief(tmp_path, "@hang\n@reply clean", "followup.md")
            )
        )
        == 124
    )
    capsys.readouterr()
    driver.out(run).joinpath("release").touch()
    recovered = _core(env, driver)
    assert recovered.execute(lambda: recovered.recover(run.id)) == 0
    _, err = capsys.readouterr()
    assert "was denied" not in err
    assert record.load_outcome(record.load(run.id, env), 2)["denied_paths"] == []


@pytest.mark.parametrize("spelling", ["relative", "absolute", "home"])
def test_run_reports_only_fully_stated_denial_paths(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    spelling: str,
) -> None:
    folder = Path(env["HOME"]) / ".toolchain"
    folder.mkdir()
    name = {
        "relative": "../.toolchain/python",
        "absolute": str(folder / "python"),
        "home": "~/.toolchain/python",
    }[spelling]
    diagnostic = f"cat: {name}: Permission denied"
    driver = _driver(env)
    monkeypatch.setattr(driver, "turn_output", lambda run: diagnostic)
    monkeypatch.setattr(sandbox, "require", lambda *args, **kwargs: None)
    choices.set_settings({"sandbox.claude": "on"}, env=env)
    assert (
        _start(_core(env, driver), repo, _brief(tmp_path, f"Task\n@reply {diagnostic}"))
        == 0
    )
    out, err = capsys.readouterr()
    run = record.load(_run_id(out), env)
    denied = record.load_outcome(run, 1)["denied_paths"]
    if spelling == "relative":
        assert denied == []
        assert "was denied" not in err
        assert "owner: Settings" not in err
    else:
        assert denied == [
            {"path": str(folder), "reason": "hidden", "allow_path": str(folder)}
        ]
        assert err.count(f"was denied {folder}") == 1


def test_absolute_denial_in_reply_pane_and_structured_output_is_reported_once(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    folder = Path(env["HOME"]) / ".toolchain"
    folder.mkdir()
    alias = Path(env["HOME"]) / "toolchain-alias"
    alias.symlink_to(folder, target_is_directory=True)
    pane = [""]
    diagnostic = f"cat: {folder}/python: Permission denied"
    driver = _driver(env, on_wait=lambda run: pane.__setitem__(0, diagnostic))
    monkeypatch.setattr(driver, "screen", lambda run: pane[0])
    monkeypatch.setattr(
        driver,
        "turn_output",
        lambda run: json.dumps({"output": f"{alias}/python: EACCES"}),
    )
    monkeypatch.setattr(sandbox, "require", lambda *args, **kwargs: None)
    choices.set_settings({"sandbox.claude": "on"}, env=env)
    assert (
        _start(_core(env, driver), repo, _brief(tmp_path, f"Task\n@reply {diagnostic}"))
        == 0
    )
    out, err = capsys.readouterr()
    run = record.load(_run_id(out), env)
    assert record.load_outcome(run, 1)["denied_paths"] == [
        {"path": str(folder), "reason": "hidden", "allow_path": str(folder)}
    ]
    assert err.count(f"WARNING: {run.id} was denied ") == 1


@pytest.mark.parametrize("source", ["structured", "extracted"])
def test_structured_path_first_diagnostic_is_recorded_and_warned(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    source: str,
) -> None:
    from ember.engines.diagnostics import output_text

    folder = Path(env["HOME"]) / ".tools"
    folder.mkdir()
    structured = {"output": f"{folder}: Permission denied"}
    text = json.dumps(structured) if source == "structured" else output_text(structured)
    driver = _driver(env)
    monkeypatch.setattr(driver, "turn_output", lambda run: text)
    monkeypatch.setattr(sandbox, "require", lambda *args, **kwargs: None)
    choices.set_settings({"sandbox.claude": "on"}, env=env)
    assert _start(_core(env, driver), repo, _brief(tmp_path, "Task\n@reply done")) == 0
    out, err = capsys.readouterr()
    run = record.load(_run_id(out), env)
    assert record.load_outcome(run, 1)["denied_paths"] == [
        {"path": str(folder), "reason": "hidden", "allow_path": str(folder)}
    ]
    assert err.count(f"WARNING: {run.id} was denied {folder}") == 1


@pytest.mark.parametrize("reverse", [False, True])
def test_denial_reason_is_merged_across_sources_before_warning(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    reverse: bool,
) -> None:
    folder = Path(env["HOME"]) / ".docker"
    folder.mkdir()
    secret = folder / "config.json"
    secret.touch()
    diagnostics = [f"{folder}: Permission denied", f"{secret}: Permission denied"]
    if reverse:
        diagnostics.reverse()
    driver = _driver(env)
    monkeypatch.setattr(driver, "turn_output", lambda run: diagnostics[1])
    monkeypatch.setattr(sandbox, "require", lambda *args, **kwargs: None)
    choices.set_settings({"sandbox.claude": "on"}, env=env)
    assert (
        _start(
            _core(env, driver), repo, _brief(tmp_path, f"Task\n@reply {diagnostics[0]}")
        )
        == 0
    )
    out, err = capsys.readouterr()
    run = record.load(_run_id(out), env)
    assert record.load_outcome(run, 1)["denied_paths"] == [
        {"path": str(folder), "reason": "secret location", "allow_path": None}
    ]
    assert err.count(f"WARNING: {run.id} was denied {folder} (secret location)") == 1


@pytest.mark.parametrize(
    ("first", "second", "expected"),
    [
        ("hidden", "secret location", "secret location"),
        ("secret location", "hidden", "secret location"),
        ("hidden", "protected", "protected"),
        ("protected", "hidden", "protected"),
        ("secret location", "protected", "protected"),
        ("protected", "secret location", "protected"),
    ],
)
def test_denial_accumulation_keeps_strongest_reason_across_attempts(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    first: str,
    second: str,
    expected: str,
) -> None:
    folder = Path(env["HOME"]) / ".docker"
    folder.mkdir()
    secret = folder / "config.json"
    secret.touch()
    runtime = folder / "runtime"
    runtime.mkdir(mode=0o700)
    env["XDG_RUNTIME_DIR"] = str(runtime)
    names = {
        "hidden": folder,
        "secret location": secret,
        "protected": runtime / "socket",
    }
    driver = _ProviderDriver(env, ["capacity", "done"])
    observed: list[str] = []

    def transcript(run: record.Run) -> str:
        kind = first if len(driver.messages) == 1 else second
        observed.append(kind)
        return f"{names[kind]}: Permission denied"

    monkeypatch.setattr(driver, "turn_output", transcript)
    monkeypatch.setattr(core, "time", _RetryClock(driver))
    monkeypatch.setattr(sandbox, "require", lambda *args, **kwargs: None)
    choices.set_settings(
        {"sandbox.codex": "on", "provider.capacity_wait": "5"}, env=env
    )
    core_ = _core(env, driver)
    monkeypatch.setattr(core_, "_sandboxed", lambda spec, run, common: spec)
    assert (
        _start(core_, repo, _brief(tmp_path, "Task"), engine="codex", model="gpt-5")
        == 0
    )
    out, err = capsys.readouterr()
    run = record.load(_run_id(out), env)
    assert list(dict.fromkeys(observed)) == [first, second]
    assert len(driver.messages) == 2
    assert record.load_outcome(run, 1)["denied_paths"] == [
        {"path": str(folder), "reason": expected, "allow_path": None}
    ]
    assert err.count(f"WARNING: {run.id} was denied {folder}") == 1


def test_forced_close_keeps_detached_worktree_while_command_holds_lock(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    core_ = _core(env)
    assert _start(core_, repo, _brief(tmp_path, "Review\n"), read_only=True) == 0
    saved = record.load(_run_id(capsys.readouterr().out), env)
    with core_._turn_lock(saved.id):
        assert core_.close(saved.id, force=True) == 0
    assert Path(saved.worktree).exists()
    assert record.load(saved.id, env).status == record.CLOSED
    assert "another emb run command" in capsys.readouterr().err
    assert core_.close(saved.id) == 0
    assert not Path(saved.worktree).exists()


@pytest.mark.parametrize("command", ["gapcheck", "panel"])
def test_start_records_the_parent_and_status_reports_an_orphan(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    parent = record.parent(command)
    assert parent is not None
    code = _start(_core(env), repo, _brief(tmp_path, "Task\n"), parent=parent)
    assert code == 0
    run_id = _run_id(capsys.readouterr().out)
    one = _status(env, run_id, capsys)
    assert one["parent"] == parent and one["orphaned"] is False
    assert one["step"] == "parent" and one["next"] is None
    run = record.load(run_id, env)
    record.save(replace(run, parent={**parent, "start": parent["start"] + 1}))
    core_ = _core(env)
    assert core_.execute(lambda: core_.status(run_id, as_json=True)) == 0
    out, err = capsys.readouterr()
    assert json.loads(out)["orphaned"] is True
    assert json.loads(out)["waiting_for_flow"] is None
    assert f"WARNING: run {run_id} is orphaned: emb {command} ended" in err
    assert err.splitlines()[-1] == (
        f"next: emb run --recover {run_id}, then emb run --close {run_id}"
    )
    _core(env).close(run_id, force=True)


@pytest.mark.parametrize(
    ("state", "warning", "next_line"),
    [
        (
            record.ParentState(
                record.PARENT_RESUME,
                "flow",
                "flow-20260101-120000-abcdef",
                "failed",
                1.0,
                ended=True,
            ),
            " is waiting for flow flow-20260101-120000-abcdef to resume (failed ",
            "next: emb flow --resume flow-20260101-120000-abcdef",
        ),
        (
            record.ParentState(
                record.PARENT_UNKNOWN,
                "panel",
                "panel-20260101-120000-abcdef",
                error="panel.json is not valid JSON",
            ),
            ": cannot read its parent panel panel-20260101-120000-abcdef: "
            "panel.json is not valid JSON",
            "next: emb panel --status panel-20260101-120000-abcdef",
        ),
    ],
)
def test_status_names_the_parent_that_holds_the_run(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    state: record.ParentState,
    warning: str,
    next_line: str,
) -> None:
    started = record.parent(state.command or "flow", state.id)
    assert started is not None
    parent = {**started, "start": 1}
    code = _start(_core(env), repo, _brief(tmp_path, "Task\n"), parent=parent)
    assert code == 0
    run_id = _run_id(capsys.readouterr().out)
    monkeypatch.setattr(record, "parent_state", lambda run, proc_locks=None: state)
    one = _status(env, run_id, capsys)
    assert one["orphaned"] is False and one["waiting_for_flow"] is None
    assert one["parent_state"]["state"] == state.state
    assert one["parent_state"]["error"] == state.error
    core_ = _core(env)
    assert core_.execute(lambda: core_.status(run_id)) == 0
    out, err = capsys.readouterr()
    assert f"WARNING: run {run_id}{warning}" in err
    assert err.splitlines()[-1] == next_line
    assert "emb run --close" not in err
    if state.state == record.PARENT_RESUME:
        assert "status: waiting for flow" in out
    _core(env).close(run_id, force=True)


def test_a_run_started_by_emb_run_has_no_parent(
    tmp_path: Path, env: dict[str, str], repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run_id = _started(tmp_path, env, repo, capsys)
    one = _status(env, run_id, capsys)
    assert one["parent"] is None and one["orphaned"] is False
    _core(env).close(run_id, force=True)


@pytest.mark.parametrize("engine", ["codex", "claude"])
@pytest.mark.parametrize(
    "name, reason", [(".profile", "startup file"), (".toolrc", "hidden")]
)
def test_home_file_denial_is_recorded_without_read_suggestion(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    engine: str,
    name: str,
    reason: str,
) -> None:
    file = Path(env["HOME"]) / name
    file.touch()
    monkeypatch.setattr(sandbox, "require", lambda *args, **kwargs: None)
    choices.set_settings({f"sandbox.{engine}": "on"}, env=env)
    brief = _brief(tmp_path, f"Task\n@reply bash: {file}: Permission denied\n")
    monkeypatch.setattr(Core, "_sandboxed", lambda self, spec, run, common: spec)
    core_ = _core(env, _driver(env, engine))
    assert _start(core_, repo, brief, engine=engine, model="test") == 0
    out, err = capsys.readouterr()
    run = record.load(_run_id(out), env)
    item = {"path": str(file), "reason": reason, "allow_path": None}
    assert run.sandbox == ("codex" if engine == "codex" else "bubblewrap")
    assert record.load_outcome(run, 1)["denied_paths"] == [item]
    assert _status(env, run.id, capsys)["turns"][0]["denied_paths"] == [item]
    assert core_.status(run.id) == 0
    status_out, _ = capsys.readouterr()
    assert json.dumps(item) in status_out
    assert f"was denied {file}" in err
    assert "sandbox.read" not in err
    assert "Settings, Sandbox read paths" not in err
    if reason == "startup file":
        assert "(startup file)" in err
        assert "owner:" not in err
    else:
        assert f"owner: {file} is hidden" in err
        assert (
            "no folder can be offered because each folder that holds it "
            "also holds private files" in err
        )


@pytest.mark.parametrize("failure", ["refused", "absent", "unreadable"])
def test_opencode_failed_send_stops_server_and_client_and_keeps_worktree(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    folder = tmp_path / "server"
    folder.mkdir()
    (folder / "view-hang").write_text("true")
    if failure == "refused":
        (folder / "prompt-code").write_text("400")
    else:
        (folder / "prompt-hang").write_text("true")
    if failure == "unreadable":
        (folder / "messages-code").write_text("500")
    binary = Path(env["PATH"].split(os.pathsep)[0]) / "opencode"
    binary.write_text(f"#!{sys.executable}\n" + STUB_SERVER)
    binary.chmod(0o755)
    for name, target in (
        ("sh", "/bin/sh"),
        ("python3", sys.executable),
        ("sleep", "/bin/sleep"),
    ):
        (binary.parent / name).symlink_to(target)
    monkeypatch.setattr(opencode2, "PROMPT_TIMEOUT", 0.05)
    monkeypatch.setattr(opencode2, "DELIVERY_TIMEOUT", 0.1)
    children: list[processes.Process] = []

    class FailingStart(OpenCode2Driver):
        def command(self, run: record.Run, first_message: str) -> LaunchSpec:
            spec = super().command(run, first_message)
            return replace(
                spec,
                env={
                    **spec.env,
                    "STUB_FOLDER": str(folder),
                    "STUB_STATE": str(run.folder / opencode2.opencode.STATE_NAME),
                },
            )

        def ready(self, run: record.Run) -> bool:
            if not super().ready(run) or not (folder / "view.json").exists():
                return False
            pid = core.tmux.pane_pid(run.id, env=env)
            assert pid is not None
            children.extend(processes.tree(pid))
            return True

    driver = FailingStart(env, poll_seconds=0.01, stop_seconds=1)
    core_ = _core(env, driver)
    assert (
        _start(
            core_,
            repo,
            _brief(tmp_path, "Review\n"),
            engine="opencode",
            model="openrouter/test",
            read_only=True,
        )
        == 71
    )
    out, err = capsys.readouterr()
    saved = record.load(_run_id(out), env)
    assert saved.status == record.FAILED
    assert saved.turn_state == record.SENT
    assert Path(saved.worktree).is_dir()
    assert children and not any(processes.running(child) for child in children)
    assert driver._server_state(saved) == opencode2.DOWN
    assert not core.tmux.exists(saved.id, env=env)
    requests = [
        json.loads(line)
        for line in (folder / "requests.jsonl").read_text().splitlines()
    ]
    assert sum(request["path"].endswith("/prompt") for request in requests) == 1
    assert driver._state(saved)["turns"]["1"]
    if failure != "refused":
        assert "delivery is uncertain" in err
        assert "message was not resent" in err
    assert core_.close(saved.id) == 0
    assert record.load(saved.id, env).status == record.CLOSED
    assert not Path(saved.worktree).exists()


def test_uncertain_followup_keeps_new_turn_pending(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class Uncertain(StubDriver):
        def send(self, run: record.Run, text: str) -> None:
            if run.turn > 1:
                raise DeliveryUncertainError("OpenCode message delivery is uncertain")
            super().send(run, text)

    driver = Uncertain("opencode", script_path(env))
    core_ = _core(env, driver)
    assert (
        _start(core_, repo, _brief(tmp_path, "Task\n"), engine="opencode", model="a/b")
        == 0
    )
    saved = record.load(_run_id(capsys.readouterr().out), env)
    message = _brief(tmp_path, "Next\n", "next.md")
    assert core_.execute(lambda: core_.send(saved.id, message)) == 71
    err = capsys.readouterr().err
    assert "delivery is uncertain" in err
    assert "the message was not sent" not in err
    saved = record.load(saved.id, env)
    assert saved.turn == 2 and saved.turn_state == record.SENT
    assert core_.execute(lambda: core_.send(saved.id, message)) == 75
    assert Path(saved.worktree).is_dir()


@pytest.mark.parametrize("parent", [False, True])
@pytest.mark.parametrize("closed", [False, True])
def test_answered_clears_wait_and_records_time(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    capsys: pytest.CaptureFixture[str],
    parent: bool,
    closed: bool,
) -> None:
    core_ = _core(env)
    assert _start(core_, repo, _brief(tmp_path, "Task\n")) == 0
    run = record.load(_run_id(capsys.readouterr().out), env)
    if parent:
        run = record.update(run, {"parent": record.parent("flow", "flow-test")})
    run = core_._wait_owner(run, "owner: install tmux")
    assert bool(run.owner_wait) is not parent
    if closed:
        run = record.update(run, {"status": record.CLOSED})
    assert core_.answered(run.id) == 0
    answered = record.load(run.id, env)
    assert answered.owner_wait == ()
    assert (answered.answered_at is None) is parent
    assert core_.step(answered)[0] != "owner"
    assert core_.answered(run.id) == 0
    assert record.load(run.id, env).answered_at == answered.answered_at


def test_owner_wait_cleared_when_later_turn_is_sent(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    core_ = _core(env)
    assert _start(core_, repo, _brief(tmp_path, "Task\n")) == 0
    run = record.load(_run_id(capsys.readouterr().out), env)
    core_._wait_owner(run, "owner: install tmux")
    original = core_._turn

    def collect(
        pending: record.Run, driver: Driver, started: float, window: str | None
    ) -> int:
        assert pending.turn == 2
        current = record.load(pending.id, env)
        assert current.owner_wait == current.owner_read == ()
        assert current.answered_at is None
        return original(pending, driver, started, window)

    monkeypatch.setattr(core_, "_turn", collect)
    message = _brief(tmp_path, "Continue\n", "message.md")
    assert core_.execute(lambda: core_.send(run.id, message)) == 0
    current = record.load(run.id, env)
    assert current.turn == 2 and current.owner_wait == ()
    assert core_.step(current)[0] != "owner"


def test_all_denial_reads_required_and_non_denial_needs_answer(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    core_ = _core(env)
    assert _start(core_, repo, _brief(tmp_path, "Task\n")) == 0
    run = record.load(_run_id(capsys.readouterr().out), env)
    folders = [tmp_path / "one", tmp_path / "two"]
    for folder in folders:
        folder.mkdir()
        run = core_._wait_owner(run, f"owner: allow {folder}", str(folder))
    choices.set_settings({"sandbox.read": [str(folders[0])]}, env=env)
    assert core_.step(run) == ("owner", None)
    run = core_._wait_owner(run, "owner: repair database")
    choices.set_settings({"sandbox.read": [str(p) for p in folders]}, env=env)
    assert core_.step(run) == ("owner", None)
    assert core_.answered(run.id) == 0
    assert core_.step(record.load(run.id, env))[0] != "owner"


@pytest.mark.parametrize("code", [exits.INTERRUPTED, exits.STALLED])
@pytest.mark.parametrize("allowed", [False, True])
def test_sent_turn_keeps_recovery_guidance_after_owner_wait(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    code: int,
    allowed: bool,
) -> None:
    """A new turn's wait and recovery are independent of the earlier denial."""
    from ember.cli import main

    folder = Path(env["HOME"]) / ".toolchain"
    folder.mkdir()
    monkeypatch.setattr(sandbox, "require", lambda *args, **kwargs: None)
    choices.set_settings({"sandbox.claude": "on"}, env=env)
    core_ = _core(env)
    brief = _brief(tmp_path, f"Task\n@reply Permission denied: '{folder}/python'\n")
    assert _start(core_, repo, brief) == 0
    run = record.load(_run_id(capsys.readouterr().out), env)
    assert run.owner_wait and run.owner_read == (str(folder),)
    if allowed:
        choices.set_settings({"sandbox.read": [str(folder)]}, env=env)
    observed: list[dict[str, Any]] = []

    def wait(pending: record.Run) -> None:
        """Inspect the sent turn before its wait ends or is interrupted."""
        if not observed:
            assert pending.turn == 2 and pending.turn_state == record.SENT
            assert main(["run", "--status", run.id, "--json"], env=env) == 0
            captured = capsys.readouterr()
            document = json.loads(captured.out)
            assert document["step"] == "running"
            assert document["next"] == f"next: emb run --status {run.id}"
            assert document["owner_wait"] == document["owner_read"] == []
            assert document["answered_at"] is None
            assert captured.err == document["next"] + "\n"
            observed.append(document)
        if code == exits.INTERRUPTED:
            raise KeyboardInterrupt

    followup = _core(env, _driver(env, on_wait=wait), stall=0.01)
    message = _brief(tmp_path, "Continue\n@hang\n", "correction.md")
    assert followup.execute(lambda: followup.send(run.id, message)) == code
    captured = capsys.readouterr()
    assert observed
    assert captured.err.splitlines()[-1] == exits.line(code, run=run.id)
    assert "owner:" not in captured.err
    for session, step in ((core.LIVE, "running"), (core.MISSING, "recover")):
        monkeypatch.setattr(
            Core, "_session_state", lambda self, run_id, state=session: state
        )
        assert main(["run", "--status", run.id, "--json"], env=env) == 0
        captured = capsys.readouterr()
        document = json.loads(captured.out)
        assert document["step"] == step
        option = "status" if step == "running" else "recover"
        assert document["next"] == f"next: emb run --{option} {run.id}"
        assert captured.err == document["next"] + "\n"


@pytest.mark.parametrize(
    ("failure", "code"),
    [(engines.DriverError("paste failed"), 71), (KeyboardInterrupt(), 74)],
)
@pytest.mark.parametrize("allowed", [False, True])
def test_failed_delivery_restores_only_unresolved_owner_wait(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    capsys: pytest.CaptureFixture[str],
    failure: BaseException,
    code: int,
    allowed: bool,
) -> None:
    """Error guidance notices read access granted without an intervening status."""
    driver = _SendFails("claude", script_path(env))
    core_ = _core(env, driver)
    assert _start(core_, repo, _brief(tmp_path, "Task\n")) == 0
    run = record.load(_run_id(capsys.readouterr().out), env)
    folder = tmp_path / "toolchain"
    folder.mkdir()
    run = core_._wait_owner(run, f"owner: allow {folder}", str(folder))
    if allowed:
        choices.set_settings({"sandbox.read": [str(folder)]}, env=env)
    driver.failure = failure
    message = _brief(tmp_path, "Continue\n", "correction.md")
    assert core_.execute(lambda: core_.send(run.id, message)) == code
    captured = capsys.readouterr()
    current = record.load(run.id, env)
    assert current.turn == run.turn and current.turn_state == run.turn_state
    assert current.owner_wait == (() if allowed else run.owner_wait)
    assert current.owner_read == (() if allowed else run.owner_read)
    assert current.answered_at == run.answered_at
    if allowed:
        assert captured.err.splitlines()[-1] == exits.line(code, "send", run=run.id)
    else:
        assert "next:" not in captured.err


@pytest.mark.parametrize("read_only", [False, True])
@pytest.mark.parametrize("command", [None, "", "printf 'ready\\n'"])
def test_preparation_before_admission_and_session(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    capsys: pytest.CaptureFixture[str],
    read_only: bool,
    command: str | None,
) -> None:
    if command is not None:
        choices.set_settings({"worktree.prepare": {str(repo): command}}, env=env)
    seen = []

    def admit(run: record.Run) -> None:
        assert Path(run.worktree).is_dir()
        assert not session_folder(tmp_path, run.id).exists()
        assert run.turn == 0
        if command:
            assert run.preparation is not None
            assert run.preparation["state"] == "ok"
            assert run.preparation["exit"] == 0
            assert run.preparation["pid"] > 1
            assert run.preparation["start"] > 0
            log = Path(run.preparation["log"])
            assert log.read_text() == "ready\n"
            assert stat.S_IMODE(log.stat().st_mode) == 0o600
        else:
            assert run.preparation is None
            assert "preparation" not in run.to_json()
            assert "start_argv" not in run.to_json()
            assert not (run.folder / "prepare.log").exists()
        seen.append(run.id)

    core_ = _core(env, admit=admit)
    options = StartOptions(
        "claude", str(repo), _brief(tmp_path, "Task"), read_only=read_only
    )
    assert core_.execute(lambda: core_.start(options)) == 0
    run_id = _run_id(capsys.readouterr().out)
    assert seen == [run_id]
    assert session_folder(tmp_path, run_id).exists()


@pytest.mark.parametrize("read_only", [False, True])
@pytest.mark.parametrize(
    ("command", "message"),
    [("echo broken >&2; exit 12", "exited 12"), (": > stray", "stray")],
)
def test_preparation_failure_keeps_tree_until_close(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    capsys: pytest.CaptureFixture[str],
    read_only: bool,
    command: str,
    message: str,
) -> None:
    choices.set_settings({"worktree.prepare": {str(repo): command}}, env=env)
    core_ = _core(env)
    assert _start(core_, repo, _brief(tmp_path, "Task"), read_only=read_only) == 79
    output = capsys.readouterr()
    run_id = _run_id(output.out)
    run = record.load(run_id, env)
    assert run.status == record.FAILED and run.turn == 0
    assert run.turn_state is None and run.last_exit == 79
    assert run.preparation is not None and run.preparation["state"] == "failed"
    assert message in output.err
    assert str(run.folder / "prepare.log") in output.err
    assert Path(run.worktree).exists()
    assert not session_folder(tmp_path, run_id).exists()
    next_line = core_.step(run, core.MISSING)[1]
    assert next_line is not None and next_line in output.err
    assert core_.execute(lambda: core_.close(run_id)) == 0
    assert not Path(run.worktree).exists()
    assert record.load(run_id, env).status == record.CLOSED
    if run.branch:
        assert git(repo, "rev-parse", run.branch, env=env) == run.base


def test_preparation_retry_saves_brief_and_options(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    choices.set_settings({"worktree.prepare": {str(repo): "exit 1"}}, env=env)
    choices.claim("prep-claim", env=env)
    brief = _brief(tmp_path, "Saved task\n", "brief with spaces.md")
    core_ = _core(env, quiet=True, stall=3)
    assert (
        _start(
            core_,
            repo,
            brief,
            model="sonnet",
            effort="high",
            read_only=True,
            read=(str(tmp_path),),
            trailers=("Reviewed-By: Lead",),
            claim="prep-claim",
            window=True,
        )
        == 79
    )
    output = capsys.readouterr()
    run = record.load(_run_id(output.out), env)
    Path(brief).unlink()
    argv = list(run.start_argv)
    assert argv[:2] == ["emb", "run"]
    assert argv[argv.index("--task-file") + 1] == str(run.folder / record.BRIEF_NAME)
    assert (run.folder / record.BRIEF_NAME).read_text() == "Saved task\n"
    for flag, value in (
        ("--engine", "claude"),
        ("--dir", str(repo)),
        ("--model", "sonnet"),
        ("--effort", "high"),
        ("--read", str(tmp_path)),
        ("--trailer", "Reviewed-By: Lead"),
        ("--claim", "prep-claim"),
        ("--stall", "3.0"),
    ):
        assert argv[argv.index(flag) + 1] == value
    assert all(flag in argv for flag in ("--window", "--read-only", "--quiet"))
    assert shlex.join(argv) in output.err


def test_preparation_lookup_error_refuses_before_artifacts(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from ember import prepare

    def unavailable(*args: object, **kwargs: object) -> None:
        raise prepare.PreparationError("cannot inspect /missing-checkout")

    monkeypatch.setattr(prepare, "lookup", unavailable)
    assert _start(_core(env), repo, _brief(tmp_path, "Task")) == 69
    output = capsys.readouterr()
    assert "cannot inspect /missing-checkout" in output.err
    assert "run " not in output.out
    assert not record.list_runs(env)[0]


def test_preparation_admission_refusal_starts_no_session(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    choices.set_settings({"worktree.prepare": {str(repo): "true"}}, env=env)
    core_ = _core(env)

    def refuse(run: record.Run) -> None:
        assert run.preparation is not None and run.preparation["state"] == "ok"
        raise RunError(79, "parent halted admission", run=run.id)

    options = StartOptions("claude", str(repo), _brief(tmp_path, "Task"))
    core_.admit = refuse
    assert core_.execute(lambda: core_.start(options)) == 79
    run = record.load(_run_id(capsys.readouterr().out), env)
    assert run.status == record.FAILED and run.turn == 0
    assert not session_folder(tmp_path, run.id).exists()


def test_send_and_recover_never_prepare(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from ember import prepare

    choices.set_settings({"worktree.prepare": {str(repo): "true"}}, env=env)
    core_ = _core(env)
    assert _start(core_, repo, _brief(tmp_path, "Task")) == 0
    run_id = _run_id(capsys.readouterr().out)

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("send or recover reached preparation")

    monkeypatch.setattr(prepare, "lookup", forbidden)
    monkeypatch.setattr(prepare, "run", forbidden)
    assert core_.execute(lambda: core_.send(run_id, _brief(tmp_path, "Again"))) == 0
    assert core_.execute(lambda: core_.recover(run_id)) == 0


@pytest.mark.parametrize("failure", ["timeout", "cancel", "shutdown", "exception"])
def test_preparation_interruption_records_failure_and_ends_group(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: str,
) -> None:
    import signal

    from ember import prepare

    choices.set_settings({"worktree.prepare": {str(repo): "/bin/sleep 60"}}, env=env)
    original = prepare.run
    stopped = threading.Event()
    groups = []

    def run_prepare(*args: Any, **kwargs: Any) -> prepare.Result:  # noqa: ANN401
        original_started = kwargs["started"]

        def started(pid: int, start: int) -> None:
            original_started(pid, start)
            groups.append(pid)
            saved = record.list_runs(env)[0][0]
            assert saved.preparation is not None
            assert saved.preparation["state"] == "running"
            assert (saved.preparation["pid"], saved.preparation["start"]) == (
                pid,
                start,
            )
            assert not session_folder(tmp_path, saved.id).exists()
            if failure == "cancel":
                stopped.set()
            elif failure == "shutdown":
                os.kill(os.getpid(), signal.SIGTERM)
            elif failure == "exception":
                raise OSError("cannot persist preparation identity")

        kwargs.update(started=started, timeout=0.05)
        return original(*args, **kwargs)

    monkeypatch.setattr(prepare, "run", run_prepare)
    core_ = _core(env)
    options = StartOptions("claude", str(repo), _brief(tmp_path, "Task"))
    core_.prepare_stop = stopped
    assert core_.execute(lambda: core_.start(options)) == 79
    run = record.load(_run_id(capsys.readouterr().out), env)
    assert run.preparation is not None
    expected = {
        "timeout": "timeout",
        "cancel": "interrupted",
        "shutdown": "interrupted",
        "exception": "failed",
    }[failure]
    assert run.preparation["state"] == expected
    assert groups and prepare.start_ticks(groups[0]) is None
    assert not session_folder(tmp_path, run.id).exists()
    assert Path(run.worktree).exists()


@pytest.mark.parametrize("force", [False, True])
def test_close_ends_recorded_preparation_group(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    capsys: pytest.CaptureFixture[str],
    force: bool,
) -> None:
    from ember import prepare

    choices.set_settings({"worktree.prepare": {str(repo): "exit 1"}}, env=env)
    core_ = _core(env)
    assert _start(core_, repo, _brief(tmp_path, "Task")) == 79
    run = record.load(_run_id(capsys.readouterr().out), env)
    with subprocess.Popen(["sleep", "60"], start_new_session=True) as process:
        try:
            ticks = prepare.start_ticks(process.pid)
            assert ticks is not None
            record.update(
                run,
                {
                    "preparation": {
                        "state": "running",
                        "command": "sleep 60",
                        "log": str(run.folder / "prepare.log"),
                        "pid": process.pid,
                        "start": ticks,
                    }
                },
            )
            assert core_.execute(lambda: core_.close(run.id, force=force)) == 0
            assert process.wait(timeout=5) == -9
            assert not Path(run.worktree).exists()
        finally:
            process.kill()


@pytest.mark.parametrize("kind", ["editing", "read-only", "gapcheck"])
@pytest.mark.parametrize("command", ["true", "exit 1"])
def test_start_holds_command_lock_from_record_creation_through_admission(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    command: str,
) -> None:
    choices.set_settings({"worktree.prepare": {str(repo): command}}, env=env)
    original = record.save
    observed = []
    admitted = []

    def locked(run: record.Run) -> None:
        with pytest.raises(RunError) as error, _core(env)._turn_lock(run.id):
            pytest.fail("start released its command lock")
        assert error.value.code == 75

    def save(run: record.Run) -> None:
        exists = (run.folder / record.RECORD_NAME).exists()
        if not exists or record.load(run.id, env).status == record.STARTING:
            locked(run)
        if not exists:
            observed.append(run.id)
        original(run)

    def admit(run: record.Run) -> None:
        locked(run)
        admitted.append(run.id)

    monkeypatch.setattr(record, "save", save)
    core_ = _core(env, admit=admit)
    code = _start(
        core_,
        repo,
        _brief(tmp_path, "Task"),
        read_only=kind != "editing",
        parent=record.parent("gapcheck") if kind == "gapcheck" else None,
    )
    assert code == (0 if command == "true" else 79)
    assert len(observed) == 1
    assert admitted == (observed if code == 0 else [])
    with _core(env)._turn_lock(observed[0]):
        pass


@pytest.mark.parametrize("kind", ["editing", "read-only", "gapcheck"])
@pytest.mark.parametrize("force", [False, True])
def test_close_fails_abandoned_start_and_ends_its_preparation(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    capsys: pytest.CaptureFixture[str],
    kind: str,
    force: bool,
) -> None:
    from ember import prepare

    choices.set_settings({"worktree.prepare": {str(repo): "exit 1"}}, env=env)
    core_ = _core(env)
    assert (
        _start(
            core_,
            repo,
            _brief(tmp_path, "Task"),
            read_only=kind != "editing",
            parent=record.parent("gapcheck") if kind == "gapcheck" else None,
        )
        == 79
    )
    run = record.load(_run_id(capsys.readouterr().out), env)
    with subprocess.Popen(["/bin/sleep", "60"], start_new_session=True) as process:
        try:
            ticks = prepare.start_ticks(process.pid)
            assert ticks is not None
            record.update(
                run,
                {
                    "status": record.STARTING,
                    "last_exit": None,
                    "preparation": {
                        "state": "running",
                        "command": "/bin/sleep 60",
                        "log": str(run.folder / "prepare.log"),
                        "pid": process.pid,
                        "start": ticks,
                    },
                },
            )
            assert core_.execute(lambda: core_.close(run.id, force=force)) == 0
            assert process.wait(timeout=5) == -9
            closed = record.load(run.id, env)
            assert closed.status == record.CLOSED and closed.last_exit == 79
            assert "abandoned" in (closed.last_reason or "")
            assert "preparation" in (closed.last_reason or "")
            assert not Path(run.worktree).exists()
        finally:
            process.kill()


@pytest.mark.parametrize("force", [False, True])
def test_close_leaves_a_locked_start_and_its_preparation_alone(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    capsys: pytest.CaptureFixture[str],
    force: bool,
) -> None:
    from ember import prepare

    choices.set_settings({"worktree.prepare": {str(repo): "exit 1"}}, env=env)
    core_ = _core(env)
    assert _start(core_, repo, _brief(tmp_path, "Task")) == 79
    run = record.load(_run_id(capsys.readouterr().out), env)
    with subprocess.Popen(["/bin/sleep", "60"], start_new_session=True) as process:
        try:
            ticks = prepare.start_ticks(process.pid)
            assert ticks is not None
            record.update(
                run,
                {
                    "status": record.STARTING,
                    "preparation": {
                        "state": "running",
                        "command": "/bin/sleep 60",
                        "log": str(run.folder / "prepare.log"),
                        "pid": process.pid,
                        "start": ticks,
                    },
                },
            )
            before = (run.folder / record.RECORD_NAME).read_bytes()
            with _core(env)._turn_lock(run.id):
                assert core_.execute(lambda: core_.close(run.id, force=force)) == 75
            assert process.poll() is None
            assert (run.folder / record.RECORD_NAME).read_bytes() == before
            assert Path(run.worktree).is_dir()
        finally:
            process.kill()


def test_codex_runs_create_distinct_temporary_folders_before_launch(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    choices.set_settings({"sandbox.codex": "on"}, env=env)
    monkeypatch.setattr(sandbox, "require", lambda engine, env=None: None)
    folders: list[Path] = []
    runs: list[record.Run] = []
    core_ = _core(env, _driver(env, "codex"))

    def inspect(spec: LaunchSpec, run: record.Run, common: Path) -> LaunchSpec:
        folder = paths.run_tmp_dir(run.id, env)
        assert folder.is_dir()
        assert stat.S_IMODE(folder.stat().st_mode) == 0o700
        folders.append(folder)
        runs.append(run)
        return spec

    # The stub engine runs directly; generated permission options are checked
    # by test_sandbox_permissions_allow_detached_review_builds.
    monkeypatch.setattr(core_, "_sandboxed", inspect)
    brief = _brief(tmp_path, "Task\n")
    for read_only in (False, True):
        assert (
            _start(
                core_, repo, brief, engine="codex", model="gpt-5", read_only=read_only
            )
            == 0
        ), capsys.readouterr().err
    assert len(set(folders)) == 2
    for run, folder in zip(runs, folders, strict=True):
        outside = tmp_path / f"outside-{run.id}"
        outside.mkdir()
        (outside / "keep").write_text("keep")
        (folder / "link").symlink_to(outside, target_is_directory=True)
        (folder / "temporary").write_text("remove")
        assert core_.close(run.id) == 0
        assert not folder.exists()
        # Closing also succeeds when the temporary folder is already gone.
        assert core_.close(run.id) == 0
        assert (outside / "keep").read_text() == "keep"
        assert Path(run.worktree).exists() is not run.read_only
        # A repeated close also clears a leftover folder.
        paths.ensure_run_tmp_dir(run.id, env)
        assert core_.close(run.id) == 0
        assert not folder.exists()


@pytest.mark.parametrize("read_only", [False, True])
def test_codex_off_creates_no_temporary_folder_and_closes_if_missing(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    read_only: bool,
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("unsandboxed Codex tried to create a temporary folder")

    monkeypatch.setattr(paths, "ensure_run_tmp_dir", forbidden)
    core_ = _core(env, _driver(env, "codex"))
    assert _start(
        core_, repo, _brief(tmp_path, "Task\n"), engine="codex", model="gpt-5",
        read_only=read_only,
    ) == 0, capsys.readouterr().err  # fmt: skip
    run = record.load(core_.last_run_id or "", env)
    assert not list(paths.RUN_TMP_ROOT.iterdir())
    assert "TMPDIR" not in (run.folder / core.LAUNCH_NAME).read_text()
    assert core_.close(run.id) == 0
    assert not list(paths.RUN_TMP_ROOT.iterdir())


@pytest.mark.parametrize("read_only", [False, True])
def test_codex_temporary_folder_creation_failure_is_69(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    read_only: bool,
) -> None:
    choices.set_settings({"sandbox.codex": "on"}, env=env)
    monkeypatch.setattr(sandbox, "require", lambda engine, env=None: None)
    parent = paths.RUN_TMP_ROOT
    parent.rmdir()
    parent.write_text("not a folder")
    driver = _driver(env, "codex")
    assert _start(
        _core(env, driver), repo, _brief(tmp_path, "Task\n"), engine="codex",
        model="gpt-5", read_only=read_only,
    ) == 69  # fmt: skip
    assert "cannot create the run's temporary folder" in capsys.readouterr().err
    assert not driver.commands
    assert not paths.worktrees_dir(env).exists()


@pytest.mark.parametrize("read_only", [False, True])
@pytest.mark.parametrize("kind", ["folder", "symlink", "dangling", "loop"])
def test_codex_existing_temporary_entry_refuses_start_without_setup(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    read_only: bool,
    kind: str,
) -> None:
    choices.set_settings({"sandbox.codex": "on"}, env=env)
    ident = "codex-20260101-000000-aabbcc"
    monkeypatch.setattr(record, "new_id", lambda engine: ident)
    folder = paths.run_tmp_dir(ident, env)
    target = tmp_path / "outside"
    target.mkdir()
    (target / "keep").write_text("keep")
    if kind == "folder":
        folder.mkdir(mode=0o755)
        (folder / "keep").write_text("keep")
    elif kind == "loop":
        folder.symlink_to(folder)
    else:
        folder.symlink_to(target if kind == "symlink" else target / "missing")
    driver = _driver(env, "codex")
    assert (
        _start(
            _core(env, driver),
            repo,
            _brief(tmp_path, "Task\n"),
            engine="codex",
            model="gpt-5",
            read_only=read_only,
        )
        == 69
    )
    assert str(folder) in capsys.readouterr().err
    assert not driver.commands
    assert not record.run_dir(ident, env).exists()
    assert not paths.worktrees_dir(env).exists()
    assert not paths.worker_cache_dir(env).exists()
    assert (target / "keep").read_text() == "keep"
    if kind == "folder":
        assert stat.S_IMODE(folder.stat().st_mode) == 0o755
        assert (folder / "keep").read_text() == "keep"
    else:
        assert folder.is_symlink()


@pytest.fixture
def scope_runner(env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> Path:
    """Enable scopes with a launcher that execs the pane command in place."""
    monkeypatch.setattr(core.scopes, "available", lambda env=None: True)
    binary = script_path(env).parent / "systemd-run"
    binary.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$@" >> "$SCOPE_LOG"\n'
        'if [ "$SCOPE_FAIL" = yes ]; then exit 1; fi\n'
        'while [ "$1" != -- ]; do shift; done\n'
        "shift\n"
        'exec "$@"\n'
    )
    binary.chmod(0o755)
    env["SCOPE_LOG"] = str(binary.parent / "scope.log")
    return Path(env["SCOPE_LOG"])


@pytest.mark.parametrize("force", [False, True])
@pytest.mark.parametrize("paste", [False, True])
def test_run_starts_in_scope_and_closes_it(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    scope_runner: Path,
    paste: bool,
    force: bool,
) -> None:
    ended = []
    monkeypatch.setattr(
        core.scopes, "end", lambda run_id, **kwargs: ended.append(run_id) or []
    )
    driver = _driver(env, paste=paste)
    core_ = _core(env, driver)
    assert _start(core_, repo, _brief(tmp_path, "Task\n")) == 0
    out, err = capsys.readouterr()
    run = record.load(_run_id(out), env)
    assert run.scoped
    assert "detached processes may outlive" not in err
    assert scope_runner.read_text().splitlines() == [
        "--user",
        "--scope",
        "--quiet",
        "--collect",
        "--unit",
        core.scopes.unit(run.id),
        "--",
        "/bin/sh",
        str(run.folder / core.LAUNCH_NAME),
    ]
    command = json.loads((session_folder(tmp_path, run.id) / "command").read_text())
    assert command["cwd"] == run.worktree
    assert command["env"]["STUB_ENGINE"] == "claude"
    assert core_.close(run.id, force=force) == 0
    assert ended == [run.id]


def test_scope_creation_failure_does_not_launch_worker_or_fall_back(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    scope_runner: Path,
) -> None:
    env["SCOPE_FAIL"] = "yes"
    ended = []

    def end(run_id: str, **kwargs: object) -> list[int]:
        ended.append(run_id)
        raise core.scopes.ScopeError("cleanup failed")

    monkeypatch.setattr(core.scopes, "end", end)
    core_ = _core(env)
    assert _start(core_, repo, _brief(tmp_path, "Task\n")) == exits.ENGINE_FAILED
    out, err = capsys.readouterr()
    run = record.load(_run_id(out), env)
    assert run.scoped and run.last_start_failure and run.status == record.FAILED
    assert "cannot start the scope" in err
    assert "cleanup failed" not in err
    assert "detached processes may outlive" not in err
    assert not (run.folder / "stub").exists()
    assert not session_folder(tmp_path, run.id).exists()
    assert scope_runner.read_text().splitlines().count("--scope") == 1
    assert ended == [run.id]


@pytest.mark.parametrize("force", [False, True])
def test_scope_close_failure_warns_and_still_ends_tree_and_removes_worktree(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    scope_runner: Path,
    force: bool,
) -> None:
    marker = tmp_path / "detached.pid"
    core_ = _core(env)
    assert (
        _start(
            core_, repo, _brief(tmp_path, f"Task\n@detach {marker}\n"), read_only=True
        )
        == 0
    )
    run = record.load(_run_id(capsys.readouterr().out), env)
    child = processes.tree(int(marker.read_text()))[0]
    temporary = paths.ensure_run_tmp_dir(run.id, env)
    (temporary / "worker-file").write_text("temporary")

    def end(run_id: str, **kwargs: object) -> list[int]:
        assert run_id == run.id
        raise core.scopes.ScopeError("permission denied")

    monkeypatch.setattr(core.scopes, "end", end)
    assert core_.close(run.id, force=force) == 0
    out, err = capsys.readouterr()
    assert out == f"closed {run.id}\n"
    assert err.startswith("WARNING:")
    assert f"systemctl --user kill {core.scopes.unit(run.id)}" in err
    assert not processes.running(child)
    assert record.load(run.id, env).status == record.CLOSED
    assert not Path(run.worktree).exists()
    assert not temporary.exists()


@pytest.mark.parametrize("legacy", [False, True])
def test_fallback_warns_once_at_start_and_closes_without_scope_commands(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    legacy: bool,
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("tree-only run tried to end a scope")

    monkeypatch.setattr(core.scopes, "end", forbidden)
    core_ = _core(env)
    assert _start(core_, repo, _brief(tmp_path, "Task\n")) == 0
    out, err = capsys.readouterr()
    assert [
        line for line in err.splitlines() if "detached processes may outlive" in line
    ] == [
        "WARNING: no usable systemd user session; "
        "detached processes may outlive the run"
    ]
    run = record.load(_run_id(out), env)
    assert not run.scoped
    if legacy:
        document = run.to_json()
        document.pop("scoped")
        (run.folder / record.RECORD_NAME).write_text(json.dumps(document))
    assert core_.close(run.id) == 0
    assert capsys.readouterr().err == ""


def test_scope_survivors_use_existing_close_report(
    tmp_path: Path,
    env: dict[str, str],
    repo: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    scope_runner: Path,
) -> None:
    monkeypatch.setattr(core.scopes, "end", lambda run_id, **kwargs: [123, 456])
    core_ = _core(env)
    assert _start(core_, repo, _brief(tmp_path, "Task\n")) == 0
    run_id = _run_id(capsys.readouterr().out)
    assert core_.close(run_id) == 0
    assert capsys.readouterr().err == (
        f"emb run: {run_id}: processes still running after SIGKILL: 123, 456\n"
    )
