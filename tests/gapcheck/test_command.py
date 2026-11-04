import io
import json
import os
import signal
import stat
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from ember import choices, paths
from ember.cli import build_parser
from ember.gapcheck import command, previous
from ember.runs import record
from ember.runs.core import Core, RunError, StartOptions
from runs.stub_engine import (  # pyrefly: ignore[missing-import]
    StubDriver,
    kill_sessions,
    make_env,
    make_repo,
    script_path,
)

RESULT_KEYS = {
    "status",
    "ask",
    "plan",
    "plan_steps",
    "failure_model",
    "previous",
    "reviewer",
    "run",
    "steps",
    "settled",
    "closed",
    "dispositions",
    "next_id",
    "previous_plan",
    "ranked",
    "counts",
    "uncovered_steps",
    "unknown_steps",
    "parse_error",
}
VALID = '{"steps": [{"step": 1, "gaps": []}, {"step": 2, "gaps": []}]}'


@pytest.fixture
def env(tmp_path: Path) -> Iterator[dict[str, str]]:
    environment = make_env(tmp_path)
    yield environment
    kill_sessions(tmp_path)


@pytest.fixture
def repo(tmp_path: Path, env: dict[str, str]) -> Path:
    return make_repo(tmp_path, env)


@pytest.fixture
def plan_file(tmp_path: Path) -> Path:
    path = tmp_path / "plan with spaces.md"
    path.write_text(
        "## Failure model\nA failure stops the work.\n1. First\n2. Second\n"
    )
    return path


@pytest.fixture
def reply_file(tmp_path: Path) -> Path:
    path = tmp_path / "answer.json"
    path.write_text(VALID)
    return path


@pytest.fixture
def driver(env: dict[str, str], reply_file: Path) -> StubDriver:
    return StubDriver("codex", script_path(env), reply_file=reply_file)


def invoke(
    env: Mapping[str, str],
    plan_file: Path,
    *options: str,
    driver: StubDriver | None = None,
    factory: Callable[..., Core] | None = None,
) -> tuple[int, str, str]:
    def make(environment: Mapping[str, str], **kwargs: Any) -> Core:  # noqa: ANN401
        assert driver is not None
        return Core(
            environment, load_driver=lambda name: driver, poll_seconds=0.05, **kwargs
        )

    args = build_parser().parse_args(["gapcheck", "--plan", str(plan_file), *options])
    out, err = io.StringIO(), io.StringIO()
    code = command.main(args, env, core_factory=factory or make, out=out, err=err)
    return code, out.getvalue(), err.getvalue()


def test_full_check_explicit_options_with_ask_rows(
    env: dict[str, str],
    repo: Path,
    plan_file: Path,
    driver: StubDriver,
) -> None:
    choices.set_settings({"single.reviewer": "ask", "single.writer": "ask"}, env=env)
    code, out, err = invoke(
        env,
        plan_file,
        "--dir",
        str(repo),
        "--engine",
        "codex",
        "--model",
        "unlisted-model",
        "--effort",
        "low",
        "--json",
        driver=driver,
    )
    assert code == 0, err
    data = json.loads(out)
    assert set(data) == RESULT_KEYS
    assert data["reviewer"] == {
        "engine": "codex",
        "model": "unlisted-model",
        "effort": "low",
        "source": "option",
    }
    assert data["run"]["closed"] is True
    run = record.load(data["run"]["id"], env)
    assert run.status == record.CLOSED
    assert run.read_only and run.branch is None and run.trailers == ()
    assert (run.folder / "brief.md").read_text().startswith("Gap check of the plan")
    assert not list(paths.runtime_dir(env).glob("gapcheck-*.md"))
    assert "WARNING:" in err  # The sandbox-off diagnostic also reaches exit 0.
    assert data["counts"] == {"high": 0, "medium": 0, "low": 0}


def test_reviewer_from_menu(
    env: dict[str, str],
    repo: Path,
    plan_file: Path,
    driver: StubDriver,
) -> None:
    choices.set_settings({"single.reviewer": "codex:gpt-5"}, env=env)
    code, out, err = invoke(env, plan_file, "--dir", str(repo), "--json", driver=driver)
    assert code == 0, err
    assert json.loads(out)["reviewer"]["source"] == "menu"


@pytest.mark.parametrize("state", ["ask", "not shortlisted", "engine off"])
def test_unusable_row_starts_nothing(
    env: dict[str, str],
    repo: Path,
    plan_file: Path,
    state: str,
) -> None:
    choices.set_settings({"single.reviewer": "codex:gpt-5"}, env=env)
    changes: dict[str, dict[str, object]] = {
        "ask": {"single.reviewer": "ask"},
        "not shortlisted": {"shortlist.codex": []},
        "engine off": {"engine.codex": "off"},
    }
    choices.set_settings(changes[state], env=env)
    code, out, err = invoke(env, plan_file, "--dir", str(repo))
    assert code == 64 and not out
    assert state in err and command.REVIEWER_LINE in err
    assert not paths.runs_dir(env).exists()


@pytest.mark.parametrize("engine", [[], ["--engine", "claude"]])
def test_subagent_route(
    env: dict[str, str],
    repo: Path,
    plan_file: Path,
    engine: list[str],
) -> None:
    choices.set_settings({"single.reviewer_as": "subagent"}, env=env)
    code, out, err = invoke(env, plan_file, "--dir", str(repo), *engine)
    assert code == 64 and not out
    assert "--brief, then give the brief to a read-only Claude subagent" in err
    assert "--reply <file>" in err


@pytest.mark.parametrize(
    "options",
    [
        ["--model", "m"],
        ["--effort", "high"],
        ["--brief", "--engine", "codex"],
        ["--reply", "answer", "--dir", "."],
        ["--brief", "--reply", "answer"],
        ["--brief", "--json"],
        ["--brief", "--stall", "10"],
        ["--reply", "answer", "--window"],
        ["--brief", "--read", "."],
        ["--reply", "answer", "--claim", "claim-123"],
    ],
)
def test_usage_errors(
    env: dict[str, str],
    plan_file: Path,
    options: list[str],
) -> None:
    code, out, err = invoke(env, plan_file, *options)
    assert code == 64 and not out
    assert err.endswith("next: emb gapcheck --help\n")


@pytest.mark.parametrize(
    ("text", "message", "uncovered"),
    [
        ('{"steps": [{"step": 1, "gaps": []}]}', "misses step 2", [2]),
        ("Only prose", "the reply is not a JSON object", [1, 2]),
    ],
)
def test_bad_reply_prints_result_and_closes(
    env: dict[str, str],
    repo: Path,
    plan_file: Path,
    reply_file: Path,
    driver: StubDriver,
    text: str,
    message: str,
    uncovered: list[int],
) -> None:
    reply_file.write_text(text)
    code, out, err = invoke(
        env,
        plan_file,
        "--dir",
        str(repo),
        "--engine",
        "codex",
        "--model",
        "m",
        "--json",
        driver=driver,
    )
    assert code == 81
    data = json.loads(out)
    assert data["uncovered_steps"] == uncovered and data["run"]["closed"]
    assert message in err
    assert err.splitlines()[-1].startswith("next: emb gapcheck --plan ")


def test_close_refused_keeps_success(
    env: dict[str, str],
    repo: Path,
    plan_file: Path,
    driver: StubDriver,
) -> None:
    driver.foreign = "an owner message"
    code, out, err = invoke(
        env,
        plan_file,
        "--dir",
        str(repo),
        "--engine",
        "codex",
        "--model",
        "m",
        "--json",
        driver=driver,
    )
    assert code == 0
    data = json.loads(out)
    assert not data["run"]["closed"]
    assert "an owner message" in data["run"]["close_error"]
    assert "stays open:" in err and "close it with emb run --close" in err
    assert not any(line.startswith("next: emb gapcheck") for line in err.splitlines())


def test_brief_starts_nothing(
    env: dict[str, str],
    repo: Path,
    plan_file: Path,
) -> None:
    code, out, err = invoke(env, plan_file, "--brief", "--dir", str(repo))
    assert code == 0 and not err
    assert out.startswith("Gap check of the plan")
    assert "repository in your own working folder" in out
    assert str(repo) not in out
    assert not paths.runs_dir(env).exists()


def test_reply_only_and_text(
    env: dict[str, str],
    plan_file: Path,
    reply_file: Path,
) -> None:
    code, out, err = invoke(env, plan_file, "--reply", str(reply_file), "--json")
    assert code == 0 and not err
    data = json.loads(out)
    assert data["reviewer"] is None and data["run"] is None
    assert set(data) == RESULT_KEYS
    code, out, _ = invoke(env, plan_file, "--reply", str(reply_file))
    assert code == 0
    assert len([line for line in out.splitlines() if line.startswith("step ")]) == 2
    assert "counts: high 0, medium 0, low 0" in out


def test_single_step_without_failure_model(
    env: dict[str, str],
    plan_file: Path,
    reply_file: Path,
) -> None:
    plan_file.write_text("One step")
    reply_file.write_text('{"steps": [{"step": 1, "gaps": []}]}')
    code, out, err = invoke(env, plan_file, "--reply", str(reply_file), "--json")
    assert code == 0 and not json.loads(out)["failure_model"]
    assert err == (
        "WARNING: the plan has no failure model; "
        "every unsettled case is reported as a gap\n"
    )


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("missing plan", 66),
        ("empty plan", 64),
        ("missing dir", 66),
        ("not checkout", 65),
        ("missing reply", 66),
    ],
)
def test_input_errors(
    tmp_path: Path,
    env: dict[str, str],
    plan_file: Path,
    case: str,
    expected: int,
) -> None:
    options: list[str] = []
    if case == "missing plan":
        plan_file.unlink()
    elif case == "empty plan":
        plan_file.write_text(" \n")
    elif case == "missing dir":
        options = ["--dir", str(tmp_path / "absent")]
    elif case == "not checkout":
        options = ["--dir", str(tmp_path)]
    else:
        options = ["--reply", str(tmp_path / "absent")]
    code, out, err = invoke(env, plan_file, *options)
    assert code == expected and not out
    assert "next:" in err


@pytest.mark.parametrize("state", ["gone", "unknown"])
@pytest.mark.parametrize("explicit", [False, True])
def test_claim_checked_before_row(
    env: dict[str, str],
    repo: Path,
    plan_file: Path,
    state: str,
    explicit: bool,
) -> None:
    if state == "gone":
        choices.finish("claim-123", env=env)
    choices.set_settings({"single.reviewer": "ask"}, env=env)
    options = ["--engine", "codex", "--model", "m"] if explicit else []
    code, out, err = invoke(
        env,
        plan_file,
        "--dir",
        str(repo),
        "--claim",
        "claim-123",
        *options,
    )
    assert code == 64 and not out
    assert err.startswith(f"emb run: claim claim-123 is {state}\n")
    assert "next: emb choices --claim --id <id>" in err
    assert not paths.runs_dir(env).exists()


@pytest.mark.parametrize(
    ("action", "expected"),
    [
        ("@exit", 71),
        ("@signedout sign in", 69),
        ("@provider limit", 73),
        ("@empty", 80),
        ("@hang", 124),
    ],
)
def test_run_failures_are_forwarded(
    env: dict[str, str],
    repo: Path,
    plan_file: Path,
    driver: StubDriver,
    action: str,
    expected: int,
) -> None:
    script = script_path(env)
    script.write_text(
        script.read_text().replace(
            "lines = message.splitlines()",
            f"lines = [{action!r}]",
        )
    )
    code, out, err = invoke(
        env,
        plan_file,
        "--dir",
        str(repo),
        "--engine",
        "codex",
        "--model",
        "m",
        "--stall",
        "0.01",
        "--json",
        driver=driver,
    )
    assert code == expected, err
    data = json.loads(out)
    assert data["run"]["exit"] == expected and not data["run"]["closed"]
    assert "emb run:" in err
    lines = err.splitlines()
    assert lines[-1].startswith("next: ") and "emb gapcheck --plan" in lines[-1]
    if data["run"]["reply"] is None:
        assert "--reply" not in lines[-1]
    else:
        assert Path(data["run"]["reply"]).exists()
        assert "turn-1.md" in lines[-1]
    assert not list(paths.runtime_dir(env).glob("gapcheck-*.md"))


@pytest.mark.parametrize("interrupted", [False, True])
def test_start_failure_removes_private_brief(
    env: dict[str, str],
    repo: Path,
    plan_file: Path,
    interrupted: bool,
) -> None:
    class FailingCore(Core):
        def start(self, options: StartOptions) -> int:
            path = Path(options.task_file)
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
            if interrupted:
                raise KeyboardInterrupt
            raise RunError(69, "failed start", "runtime")

    code, out, err = invoke(
        env,
        plan_file,
        "--dir",
        str(repo),
        "--engine",
        "codex",
        "--model",
        "m",
        factory=FailingCore,
    )
    assert code == (74 if interrupted else 69) and not out
    assert "emb run:" in err
    assert not list(paths.runtime_dir(env).glob("gapcheck-*.md"))


def test_environment_and_concurrent_briefs(
    env: dict[str, str],
    repo: Path,
    plan_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    barrier = threading.Barrier(2)
    seen: list[Path] = []
    original_run = command.subprocess.run

    def git_call(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        assert not set(command.GIT_LOCATION).intersection(kwargs["env"])
        return original_run(*args, **kwargs)

    monkeypatch.setattr(command.subprocess, "run", git_call)
    dirty_env = {**env, **dict.fromkeys(command.GIT_LOCATION, "/invalid")}

    class CheckingCore(Core):
        def start(self, options: StartOptions) -> int:
            assert not set(command.GIT_LOCATION).intersection(self.env)
            seen.append(Path(options.task_file))
            barrier.wait(timeout=10)
            assert len(set(seen)) == 2 and all(path.exists() for path in seen)
            barrier.wait(timeout=10)
            raise RunError(69, "stop after checking")

    def check() -> tuple[int, str, str]:
        return invoke(
            dirty_env,
            plan_file,
            "--dir",
            str(repo),
            "--engine",
            "codex",
            "--model",
            "m",
            factory=CheckingCore,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: check(), range(2)))
    assert all(code == 69 for code, _, _ in results)
    assert not any(path.exists() for path in seen)


def test_recorded_reply_cannot_be_decoded(
    env: dict[str, str],
    repo: Path,
    plan_file: Path,
    driver: StubDriver,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_load = record.load

    def load(run_id: str, environment: Mapping[str, str] | None = None) -> record.Run:
        run = original_load(run_id, environment)
        if run.reply:
            Path(run.reply).write_bytes(b"\xff")
        return run

    monkeypatch.setattr(command.record, "load", load)
    code, out, err = invoke(
        env,
        plan_file,
        "--dir",
        str(repo),
        "--engine",
        "codex",
        "--model",
        "m",
        "--json",
        driver=driver,
    )
    assert code == 66 and json.loads(out)["run"]["exit"] == 0
    assert "cannot read the reply" in err and "next: ls -l" in err


def test_success_without_reply_file_is_81(
    env: dict[str, str],
    repo: Path,
    plan_file: Path,
    driver: StubDriver,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_load = record.load

    def load(run_id: str, environment: Mapping[str, str] | None = None) -> record.Run:
        run = original_load(run_id, environment)
        if run.reply:
            Path(run.reply).unlink(missing_ok=True)
        return run

    monkeypatch.setattr(command.record, "load", load)
    code, out, err = invoke(
        env,
        plan_file,
        "--dir",
        str(repo),
        "--engine",
        "codex",
        "--model",
        "m",
        "--json",
        driver=driver,
    )
    data = json.loads(out)
    assert code == 81 and data["run"]["reply"] is None
    assert data["parse_error"] == "the run produced no reply"
    assert data["uncovered_steps"] == [1, 2]
    assert "--reply" not in err.splitlines()[-1]


def test_interrupt_during_brief_write_removes_file(
    env: dict[str, str],
    repo: Path,
    plan_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def interrupt(*args: object) -> str:
        raise KeyboardInterrupt

    monkeypatch.setattr(command.brief, "text", interrupt)
    code, out, err = invoke(
        env,
        plan_file,
        "--dir",
        str(repo),
        "--engine",
        "codex",
        "--model",
        "m",
    )
    assert code == 74 and not out and "interrupted" in err
    assert not list(paths.runtime_dir(env).glob("gapcheck-*.md"))


def test_runtime_failure_is_69(
    env: dict[str, str],
    repo: Path,
    plan_file: Path,
) -> None:
    Path(env["XDG_RUNTIME_DIR"]).write_text("not a directory")
    code, out, err = invoke(
        env,
        plan_file,
        "--dir",
        str(repo),
        "--engine",
        "codex",
        "--model",
        "m",
    )
    assert code == 69 and not out
    assert "owner: set XDG_RUNTIME_DIR to a private folder of this user" in err


def test_claim_is_passed_through_and_not_finished(
    env: dict[str, str],
    repo: Path,
    plan_file: Path,
    driver: StubDriver,
) -> None:
    choices.set_once({"single.reviewer": "codex:gpt-5"}, env=env)
    choices.claim("claim-123", env=env)
    before = choices.read("claim-123", env=env)
    code, out, err = invoke(
        env,
        plan_file,
        "--dir",
        str(repo),
        "--claim",
        "claim-123",
        "--json",
        driver=driver,
    )
    assert code == 0, err
    run = record.load(json.loads(out)["run"]["id"], env)
    assert run.claim == "claim-123"
    assert choices.read("claim-123", env=env) == before


def test_invalid_reply_and_refused_close_have_one_gapcheck_action(
    env: dict[str, str],
    repo: Path,
    plan_file: Path,
    reply_file: Path,
    driver: StubDriver,
) -> None:
    driver.foreign = "an owner message"
    reply_file.write_text("no JSON")
    code, out, err = invoke(
        env,
        plan_file,
        "--dir",
        str(repo),
        "--engine",
        "codex",
        "--model",
        "m",
        "--json",
        driver=driver,
    )
    assert code == 81 and not json.loads(out)["run"]["closed"]
    actions = [
        line for line in err.splitlines() if line.startswith("next: emb gapcheck")
    ]
    assert len(actions) == 1 and "--reply" in actions[0]
    assert "WARNING: the run" in err


def test_interrupt_while_waiting_keeps_the_run(
    env: dict[str, str],
    repo: Path,
    plan_file: Path,
    driver: StubDriver,
) -> None:
    class WaitInterrupted(Core):
        def _wait_turn(self, run: Any, driver: Any) -> Any:  # noqa: ANN401
            raise KeyboardInterrupt

    def make(environment: Mapping[str, str], **kwargs: Any) -> Core:  # noqa: ANN401
        return WaitInterrupted(
            environment, load_driver=lambda name: driver, poll_seconds=0.05, **kwargs
        )

    code, out, err = invoke(
        env,
        plan_file,
        "--dir",
        str(repo),
        "--engine",
        "codex",
        "--model",
        "m",
        "--json",
        factory=make,
    )
    assert code == 74, err
    data = json.loads(out)
    assert data["run"]["exit"] == 74 and not data["run"]["closed"]
    assert "keeps running" in err
    assert err.splitlines()[-1].startswith("next: ")
    assert not list(paths.runtime_dir(env).glob("gapcheck-*.md"))
    assert record.load(data["run"]["id"], env).status != record.CLOSED


def test_gap_check_reviewer_row_wins_over_the_single_reviewer(
    env: dict[str, str],
    repo: Path,
    plan_file: Path,
    driver: StubDriver,
) -> None:
    choices.set_settings(
        {
            "shortlist.codex": ["gpt-5@high", "gpt-5-mini@low"],
            "single.reviewer": "codex:gpt-5",
            "gapcheck.reviewer": "codex:gpt-5-mini",
        },
        env=env,
    )
    code, out, err = invoke(env, plan_file, "--dir", str(repo), "--json", driver=driver)
    assert code == 0, err
    reviewer = json.loads(out)["reviewer"]
    assert (reviewer["model"], reviewer["effort"]) == ("gpt-5-mini", "low")


@pytest.mark.parametrize(
    ("when", "status"),
    [("off", "skipped"), ("3", "skipped"), ("ask", "ask")],
)
def test_settings_skip_or_ask_without_a_run(
    env: dict[str, str],
    repo: Path,
    plan_file: Path,
    when: str,
    status: str,
) -> None:
    choices.set_settings(
        {"single.reviewer": "codex:gpt-5", "gapcheck.when": when}, env=env
    )

    def no_run(*_args: object, **_kwargs: object) -> Core:
        raise AssertionError("no run may start")

    code, out, err = invoke(
        env, plan_file, "--dir", str(repo), "--json", factory=no_run
    )
    assert code == 0, err
    data = json.loads(out)
    assert data["status"] == status
    assert data["reason"]
    assert ("owner: ask whether to run a gap check" in err) == (status == "ask")


def test_confirmed_runs_a_check_the_settings_skip(
    env: dict[str, str],
    repo: Path,
    plan_file: Path,
    driver: StubDriver,
) -> None:
    choices.set_settings(
        {"single.reviewer": "codex:gpt-5", "gapcheck.when": "ask"}, env=env
    )
    code, out, err = invoke(
        env, plan_file, "--dir", str(repo), "--confirmed", "--json", driver=driver
    )
    assert code == 0, err
    assert json.loads(out)["status"] == "run"


def test_plan_at_the_least_count_is_checked(
    env: dict[str, str],
    repo: Path,
    plan_file: Path,
    driver: StubDriver,
) -> None:
    choices.set_settings(
        {"single.reviewer": "codex:gpt-5", "gapcheck.when": "2"}, env=env
    )
    code, out, err = invoke(env, plan_file, "--dir", str(repo), "--json", driver=driver)
    assert code == 0, err
    assert json.loads(out)["status"] == "run"


def test_brief_of_a_skipped_plan_prints_no_brief(
    env: dict[str, str], repo: Path, plan_file: Path
) -> None:
    choices.set_settings({"gapcheck.when": "off"}, env=env)
    code, out, err = invoke(env, plan_file, "--dir", str(repo), "--brief")
    assert code == 0, err
    assert out == ""
    assert err.startswith("gap check skipped: the gap check is off")


def test_unreadable_settings_ask_about_every_gap() -> None:
    gap = {"harm": "low"}
    result: dict[str, Any] = {"steps": [{"gaps": [gap]}], "ranked": [gap]}
    defaults = {"gapcheck.ask": {"value": "high", "layer": "default"}}
    command._mark_asks(result, {"source_error": True, "answers": defaults})
    assert result["ask"] == "all"
    assert gap["ask_owner"] is True


def test_confirmed_subagent_route_keeps_confirmed(
    env: dict[str, str], repo: Path, plan_file: Path
) -> None:
    choices.set_settings(
        {"single.reviewer_as": "subagent", "gapcheck.when": "ask"}, env=env
    )
    code, _out, err = invoke(env, plan_file, "--dir", str(repo), "--confirmed")
    assert code == 64
    assert "--brief --confirmed, then give the brief" in err
    code, out, err = invoke(
        env, plan_file, "--dir", str(repo), "--brief", "--confirmed"
    )
    assert code == 0, err
    assert out.startswith("Gap check of the plan")


def dispositions(text: str, **statuses: str) -> str:
    made = json.loads(text)
    made["dispositions"] = [
        {"id": identity, "status": status, "reason": "Plan reviewed"}
        for identity, status in statuses.items()
    ]
    return json.dumps(made)


def gap_reply(*gaps: dict[str, Any]) -> str:
    return json.dumps(
        {"steps": [{"step": 1, "gaps": list(gaps)}, {"step": 2, "gaps": []}]}
    )


def reply_gap(name: str, **extra: str) -> dict[str, str]:
    return {"gap": name, "harm": "high", "why": "w", "default": "d", **extra}


def save_answered(path: Path, data: dict[str, Any], answers: Mapping[str, str]) -> Path:
    """Save a result with answers on its gaps, as the lead records them."""
    for step in data["steps"]:
        for item in step["gaps"]:
            if item["id"] in answers:
                item["answer"] = answers[item["id"]]
    path.write_text(json.dumps(data))
    return path


def check_reply(
    env: dict[str, str],
    plan_file: Path,
    reply_file: Path,
    text: str,
    *options: str,
) -> tuple[int, dict[str, Any], str]:
    reply_file.write_text(text)
    code, out, err = invoke(
        env, plan_file, "--reply", str(reply_file), "--json", *options
    )
    return code, json.loads(out) if out else {}, err


def test_first_result_holds_ids_and_null_answers(
    env: dict[str, str], plan_file: Path, reply_file: Path
) -> None:
    code, data, err = check_reply(
        env, plan_file, reply_file, gap_reply(reply_gap("a"), reply_gap("b"))
    )
    assert code == 0, err
    assert data["previous"] is None and data["settled"] == []
    gaps = data["steps"][0]["gaps"]
    assert [(item["id"], item["earlier"], item["answer"]) for item in gaps] == [
        ("g1", None, None),
        ("g2", None, None),
    ]
    assert data["ranked"] == [
        {"id": "g1", "step": 1, "harm": "high"},
        {"id": "g2", "step": 1, "harm": "high"},
    ]


def test_repeat_check_on_an_unchanged_answered_plan_settles_every_gap(
    env: dict[str, str], tmp_path: Path, plan_file: Path, reply_file: Path
) -> None:
    _, first, _ = check_reply(
        env, plan_file, reply_file, gap_reply(reply_gap("a"), reply_gap("b"))
    )
    saved = save_answered(
        tmp_path / "check-1.json", first, {"g1": "Keep 30 days", "g2": "Stop"}
    )
    code, data, err = check_reply(
        env,
        plan_file,
        reply_file,
        dispositions(VALID, g1="settled", g2="settled"),
        "--previous",
        str(saved),
    )
    assert code == 0, err
    assert data["previous"] == str(saved)
    assert data["ranked"] == []
    assert data["counts"] == {"high": 0, "medium": 0, "low": 0}
    assert [(item["id"], item["answer"]) for item in data["settled"]] == [
        ("g1", "Keep 30 days"),
        ("g2", "Stop"),
    ]
    assert data["settled"][0]["title"] == "1. First"
    # The settled answers reach a third check through the second result.
    second = tmp_path / "check-2.json"
    second.write_text(json.dumps(data))
    code, third, _ = check_reply(
        env,
        plan_file,
        reply_file,
        dispositions(VALID, g1="settled", g2="settled"),
        "--previous",
        str(second),
    )
    assert code == 0
    assert [item["id"] for item in third["settled"]] == ["g1", "g2"]


def test_repeat_check_reports_an_invalidated_gap_with_its_earlier_id(
    env: dict[str, str], tmp_path: Path, plan_file: Path, reply_file: Path
) -> None:
    _, first, _ = check_reply(
        env, plan_file, reply_file, gap_reply(reply_gap("a"), reply_gap("b"))
    )
    saved = save_answered(
        tmp_path / "check-1.json", first, {"g1": "Keep 30 days", "g2": "Stop"}
    )
    reopened = reply_gap("The log is now deleted at step 1", earlier="g1")
    text = dispositions(
        gap_reply(reopened, reply_gap("new")), g1="reopened", g2="settled"
    )
    code, data, err = check_reply(
        env, plan_file, reply_file, text, "--previous", str(saved)
    )
    assert code == 0, err
    gaps = data["steps"][0]["gaps"]
    assert [(item["id"], item["earlier"], item["answer"]) for item in gaps] == [
        ("g3", "g1", None),
        ("g4", None, None),
    ]
    assert [item["id"] for item in data["settled"]] == ["g2"]
    code, out, _ = invoke(
        env, plan_file, "--reply", str(reply_file), "--previous", str(saved)
    )
    assert code == 0
    assert "  g3 high: The log is now deleted at step 1" in out
    assert "    earlier: g1, answered: Keep 30 days" in out
    assert f"settled from {saved}:" in out
    assert "  g2 high, earlier step 1: b" in out
    assert "    answer: Stop" in out


def test_previous_without_answers_counts_its_gaps_as_unanswered(
    env: dict[str, str], tmp_path: Path, plan_file: Path, reply_file: Path
) -> None:
    saved = tmp_path / "old.json"
    _, old, _ = check_reply(env, plan_file, reply_file, gap_reply(reply_gap("a")))
    old_gap = old["steps"][0]["gaps"][0]
    del old_gap["id"], old_gap["answer"], old_gap["earlier"]
    old["ranked"] = [{"step": 1, **old_gap}]
    for key in ("next_id", "previous_plan", "settled", "closed", "dispositions"):
        del old[key]
    saved.write_text(json.dumps(old))
    text = dispositions(gap_reply(reply_gap("a", earlier="g1")), g1="reopened")
    code, data, err = check_reply(
        env, plan_file, reply_file, text, "--previous", str(saved)
    )
    assert code == 0, err
    assert data["settled"] == []
    assert data["steps"][0]["gaps"][0]["id"] == "g2"
    code, out, _ = invoke(
        env, plan_file, "--reply", str(reply_file), "--previous", str(saved)
    )
    assert code == 0
    assert "    earlier: g1, unanswered" in out
    assert f"settled from {saved}:\n  none\n" in out


def test_unknown_or_unexpected_earlier_id_is_81(
    env: dict[str, str], tmp_path: Path, plan_file: Path, reply_file: Path
) -> None:
    saved = tmp_path / "check-1.json"
    _, empty, _ = check_reply(env, plan_file, reply_file, VALID)
    saved.write_text(json.dumps(empty))
    text = gap_reply(reply_gap("a", earlier="g5"))
    code, data, err = check_reply(
        env, plan_file, reply_file, text, "--previous", str(saved)
    )
    assert code == 81
    message = (
        "the reply names earlier gap g5, which the previous result does not have "
        "as an active or settled gap"
    )
    assert message in err
    assert data["parse_error"] == message
    code, _, err = check_reply(env, plan_file, reply_file, text)
    assert code == 81
    assert "but the check has no previous result" in err


def test_invalid_or_unreadable_previous_starts_nothing(
    env: dict[str, str],
    tmp_path: Path,
    plan_file: Path,
    reply_file: Path,
    repo: Path,
) -> None:
    saved = tmp_path / "check-1.json"
    _, malformed, _ = check_reply(env, plan_file, reply_file, gap_reply(reply_gap("a")))
    malformed["ranked"][0]["answer"] = "x"
    saved.write_text(json.dumps(malformed))
    for options in (
        ("--reply", str(reply_file)),
        ("--brief", "--dir", str(repo)),
        ("--dir", str(repo), "--engine", "codex", "--model", "m"),
    ):
        code, out, err = invoke(env, plan_file, *options, "--previous", str(saved))
        assert code == command.INVALID_PREVIOUS == 65
        assert out == ""
        assert "unexpected key 'answer'" in err
        assert err.splitlines()[-1].startswith("next: correct ")
    assert not paths.runs_dir(env).exists()
    missing = str(tmp_path / "missing.json")
    code, _, err = invoke(
        env, plan_file, "--reply", str(reply_file), "--previous", missing
    )
    assert code == 66
    assert err.splitlines()[-1].startswith("next: ls -l ")


def test_brief_and_run_pass_the_earlier_gaps(
    env: dict[str, str],
    tmp_path: Path,
    repo: Path,
    plan_file: Path,
    driver: StubDriver,
) -> None:
    _, first, _ = check_reply(
        env, plan_file, Path(driver.reply_file), gap_reply(reply_gap("Retention"))
    )
    saved = save_answered(tmp_path / "check-1.json", first, {"g1": "Keep"})
    first["steps"][0]["step"] = 2
    first["steps"][0]["title"] = "Second"
    first["steps"][1]["step"] = 1
    first["ranked"][0]["step"] = 2
    saved.write_text(json.dumps(first))
    Path(driver.reply_file).write_text(dispositions(VALID, g1="settled"))
    listed = "- g1, earlier step 2 (Second): Retention Answer: Keep"
    code, out, err = invoke(
        env, plan_file, "--brief", "--dir", str(repo), "--previous", str(saved)
    )
    assert code == 0, err
    assert listed in out.splitlines()
    code, out, err = invoke(
        env,
        plan_file,
        "--dir",
        str(repo),
        "--engine",
        "codex",
        "--model",
        "m",
        "--json",
        "--previous",
        str(saved),
        driver=driver,
    )
    assert code == 0, err
    data = json.loads(out)
    assert set(data) == RESULT_KEYS
    assert data["settled"][0]["id"] == "g1"
    run = record.load(data["run"]["id"], env)
    assert listed in (run.folder / "brief.md").read_text().splitlines()


def answer_command(env: Mapping[str, str], *options: str) -> tuple[int, str, str]:
    args = build_parser().parse_args(["gapcheck", *options])
    out, err = io.StringIO(), io.StringIO()
    code = command.main(args, env, out=out, err=err)
    return code, out.getvalue(), err.getvalue()


def test_answer_command_records_and_validates_without_a_plan(
    env: dict[str, str], tmp_path: Path, plan_file: Path, reply_file: Path
) -> None:
    _, first, _ = check_reply(env, plan_file, reply_file, gap_reply(reply_gap("a")))
    saved = tmp_path / "result.json"
    saved.write_text(json.dumps(first))
    plan_file.unlink()
    code, out, err = answer_command(
        env,
        "--answer",
        str(saved),
        "--id",
        "g1",
        "--text",
        "Keep 30 days\nThen delete",
        "--json",
    )
    assert code == 0, err
    made = json.loads(out)
    assert made == json.loads(saved.read_text())
    assert made["steps"][0]["gaps"][0]["answer"] == "Keep 30 days\nThen delete"
    assert made["ranked"] == first["ranked"]
    previous.load(str(saved))
    assert not list(tmp_path.glob(".result.json.*"))


@pytest.mark.parametrize(
    "options",
    [
        [],
        ["--id", "g1"],
        ["--text", "a"],
        ["--id", "g1", "--text", ""],
        ["--id", "g1", "--text", "   "],
        ["--id", "unknown", "--text", "a"],
        ["--id", "g1", "--text", "a", "--brief"],
        ["--id", "g1", "--text", "a", "--plan", "missing"],
        ["--id", "g1", "--text", "a", "--previous", "missing"],
        ["--id", "g1", "--text", "a", "--reply", "missing"],
    ],
)
def test_answer_errors_do_not_change_the_file(
    env: dict[str, str],
    tmp_path: Path,
    plan_file: Path,
    reply_file: Path,
    options: list[str],
) -> None:
    _, first, _ = check_reply(env, plan_file, reply_file, gap_reply(reply_gap("a")))
    saved = tmp_path / "result.json"
    saved.write_text(json.dumps(first))
    before = saved.read_bytes()
    code, out, err = answer_command(env, "--answer", str(saved), *options)
    assert code == 64, err
    assert out == "" and "next:" in err
    assert saved.read_bytes() == before


def test_answer_input_and_write_errors(
    env: dict[str, str],
    tmp_path: Path,
    plan_file: Path,
    reply_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    saved = tmp_path / "result.json"
    options = ["--answer", str(saved), "--id", "g1", "--text", "a"]
    code, _, _ = answer_command(env, *options)
    assert code == 66
    saved.write_text("{}")
    code, _, _ = answer_command(env, *options)
    assert code == 65
    _, first, _ = check_reply(env, plan_file, reply_file, gap_reply(reply_gap("a")))
    saved.write_text(json.dumps(first))
    before = saved.read_bytes()

    def refuse(*args: object) -> None:
        raise OSError("write refused")

    monkeypatch.setattr(previous.os, "replace", refuse)
    code, out, err = answer_command(env, *options)
    assert code == 74 and not out
    assert "write refused" in err and "next:" in err
    assert saved.read_bytes() == before
    assert not list(tmp_path.glob(".result.json.*"))


def test_repeat_chain_carries_closed_history_and_never_reuses_an_id(
    env: dict[str, str], tmp_path: Path, plan_file: Path, reply_file: Path
) -> None:
    _, first, _ = check_reply(
        env, plan_file, reply_file, gap_reply(reply_gap("a"), reply_gap("b"))
    )
    saved = save_answered(tmp_path / "first.json", first, {"g1": "Keep"})
    code, second, err = check_reply(
        env,
        plan_file,
        reply_file,
        dispositions(VALID, g1="settled", g2="closed"),
        "--previous",
        str(saved),
    )
    assert code == 0, err
    assert second["next_id"] == 3
    assert second["closed"][0]["id"] == "g2"
    assert second["closed"][0]["reason"] == "Plan reviewed"
    saved.write_text(json.dumps(second))
    # A settled gap still needs a disposition. A closed one does not.
    code, third, err = check_reply(
        env,
        plan_file,
        reply_file,
        dispositions(
            gap_reply(reply_gap("Closed concern matters again")), g1="settled"
        ),
        "--previous",
        str(saved),
    )
    assert code == 0, err
    assert third["steps"][0]["gaps"][0]["id"] == "g3"
    assert third["steps"][0]["gaps"][0]["earlier"] is None
    assert third["next_id"] == 4 and third["closed"] == second["closed"]
    previous.validate(third)
    saved.write_text(json.dumps(third))
    code, _, err = check_reply(
        env,
        plan_file,
        reply_file,
        dispositions(VALID, g3="closed"),
        "--previous",
        str(saved),
    )
    assert code == 81 and "misses earlier gap g1" in err
    code, _, err = check_reply(
        env,
        plan_file,
        reply_file,
        dispositions(VALID, g1="settled", g2="closed", g3="closed"),
        "--previous",
        str(saved),
    )
    assert code == 81 and "unknown earlier id g2" in err


@pytest.mark.parametrize("mode", ["reply", "brief", "run"])
def test_different_plan_path_warns_and_records_both_paths(
    env: dict[str, str],
    tmp_path: Path,
    plan_file: Path,
    reply_file: Path,
    repo: Path,
    driver: StubDriver,
    mode: str,
) -> None:
    _, first, _ = check_reply(env, plan_file, reply_file, VALID)
    saved = tmp_path / "first.json"
    saved.write_text(json.dumps(first))
    changed = tmp_path / "changed.md"
    changed.write_text(plan_file.read_text())
    options = {
        "reply": ["--reply", str(reply_file), "--json"],
        "brief": ["--brief", "--dir", str(repo)],
        "run": ["--dir", str(repo), "--engine", "codex", "--model", "m", "--json"],
    }
    code, out, err = invoke(
        env, changed, *options[mode], "--previous", str(saved), driver=driver
    )
    assert code == 0, err
    assert "WARNING: the previous result's plan" in err
    assert str(changed) in err and str(plan_file) in err
    if mode != "brief":
        made = json.loads(out)
        assert made["plan"] == str(changed) and made["previous_plan"] == str(plan_file)
        previous.validate(made)


@pytest.mark.parametrize("options", [[], ["--id", "g1"], ["--text", "a"]])
def test_check_requires_plan_and_answer_options_require_answer(
    env: dict[str, str], options: list[str]
) -> None:
    code, out, err = answer_command(env, *options)
    assert code == 64 and not out and "next:" in err


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGHUP])
def test_stop_signal_while_waiting_keeps_the_run_with_its_parent(
    env: dict[str, str],
    repo: Path,
    plan_file: Path,
    driver: StubDriver,
    signum: int,
) -> None:
    class WaitSignalled(Core):
        def _wait_turn(self, run: Any, driver: Any) -> Any:  # noqa: ANN401
            os.kill(os.getpid(), signum)
            time.sleep(5)
            raise AssertionError("the signal did not interrupt the wait")

    def make(environment: Mapping[str, str], **kwargs: Any) -> Core:  # noqa: ANN401
        return WaitSignalled(
            environment, load_driver=lambda name: driver, poll_seconds=0.05, **kwargs
        )

    before = signal.getsignal(signum)
    code, out, err = invoke(
        env,
        plan_file,
        "--dir",
        str(repo),
        "--engine",
        "codex",
        "--model",
        "m",
        "--json",
        factory=make,
    )
    assert code == 74, err
    assert signal.getsignal(signum) is before
    data = json.loads(out)
    assert "keeps running" in err
    assert err.splitlines()[-1].startswith("next: ")
    run = record.load(data["run"]["id"], env)
    assert run.status != record.CLOSED
    assert run.parent is not None and run.parent["command"] == "gapcheck"
    assert run.parent["pid"] == os.getpid() and not record.orphaned(run)
