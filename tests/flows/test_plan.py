import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from ember import choices
from ember.engines import Driver, DriverMissingError, status
from ember.flows import plan
from runs.stub_engine import (  # pyrefly: ignore[missing-import]
    StubDriver,
    engine_cache,
    git,
    make_env,
    make_repo,
    script_path,
)


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    return make_env(tmp_path)


@pytest.fixture
def raw(env: dict[str, str]) -> dict[str, Any]:
    return {
        "dir": env["HOME"],
        "tasks": [
            {
                "id": "a",
                "subject": "feat: a",
                "task": "Implement a",
                "files": ["src/a.py"],
            }
        ],
    }


def parsed(raw: dict[str, Any], tmp_path: Path) -> plan.Plan:
    return plan.from_json(raw, tmp_path / "plan.json")


def test_defaults_and_lookup(raw: dict[str, Any], tmp_path: Path) -> None:
    item = parsed(raw, tmp_path)
    assert item.base == "HEAD"
    assert (item.rounds, item.parallel, item.check_timeout) == (2, 4, 1800)
    assert item.writer is item.reviewer is None
    assert item.context == item.failure_model == ""
    assert item.trailers == item.check == item.read == ()
    assert item.writer_check is item.writer_check_for(item.task("a")) is None
    assert item.path.is_absolute()
    assert item.task("a") == plan.Task(
        "a", "feat: a", "Implement a", ("src/a.py",), (), ""
    )
    with pytest.raises(KeyError):
        item.task("missing")


def test_full_plan(raw: dict[str, Any], tmp_path: Path) -> None:
    raw.update(
        schema_version=1,
        base="main",
        trailers=["Co-Authored-By: X <x@y>"],
        writer={"engine": "codex", "model": "gpt-5", "effort": "high"},
        reviewer={"engine": "claude", "model": "opus"},
        rounds=0,
        parallel=8,
        check=["make", "check", ""],
        check_timeout=1,
        context="settled",
        failure_model="cases",
        read=["/data"],
    )
    raw["tasks"][0].update(subject="feat: implement a", review="check a", after=[])
    text = json.dumps(raw, indent=2) + "\n"
    item = plan.parse(text, tmp_path / "plan.json")
    assert item.text == text
    assert item.writer == plan.Worker("codex", "gpt-5", "high", "plan")
    assert item.reviewer == plan.Worker("claude", "opus", None, "plan")
    assert (item.rounds, item.parallel, item.check_timeout) == (0, 8, 1)
    assert item.check == ("make", "check", "")
    assert item.context == "settled" and item.failure_model == "cases"
    assert item.trailers == ("Co-Authored-By: X <x@y>",)
    assert item.read == ("/data",) and item.base == "main"
    assert item.task("a").subject == "feat: implement a"


@pytest.mark.parametrize("default", [[], [["pytest", "tests/a b.py", ""], ["ruff"]]])
def test_writer_check_resolution(
    raw: dict[str, Any], tmp_path: Path, default: list[list[str]]
) -> None:
    first = raw["tasks"][0]
    raw["writer_check"] = default
    raw["tasks"] += [
        {**first, "id": "b", "writer_check": [["pytest", "tests/test_b.py"]]},
        {**first, "id": "c", "writer_check": []},
        {**first, "id": "d", "writer_check": default},
    ]
    document = parsed(raw, tmp_path)
    expected = tuple(tuple(argv) for argv in default)
    assert document.check == ()
    assert document.writer_check == expected
    assert document.task("a").writer_check is None
    assert document.task("d").writer_check == expected
    assert [document.writer_check_for(task) for task in document.tasks] == [
        expected,
        (("pytest", "tests/test_b.py"),),
        (),
        expected,
    ]
    del raw["writer_check"]
    document = parsed(raw, tmp_path)
    assert document.writer_check is None
    assert document.writer_check_for(document.task("a")) is None
    assert document.writer_check_for(document.task("c")) == ()


@pytest.mark.parametrize("scope", ["plan", "task"])
@pytest.mark.parametrize(
    "bad",
    [
        None,
        "make",
        ["make"],
        [[]],
        [[""]],
        [[" \n"]],
        [["make\0"]],
        [["make", 1]],
        [["make", "a\0\n"]],
    ],
)
def test_invalid_writer_check(
    raw: dict[str, Any], tmp_path: Path, scope: str, bad: object
) -> None:
    target, location = (
        (raw, "plan") if scope == "plan" else (raw["tasks"][0], "plan.tasks[0]")
    )
    target["writer_check"] = bad
    with pytest.raises(plan.PlanError) as caught:
        parsed(raw, tmp_path)
    assert caught.value.code == 64
    assert caught.value.problems[0].startswith(f"{location}.writer_check")


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("schema_version", 2),
        ("schema_version", True),
        ("schema_version", 1.0),
        ("dir", "relative"),
        ("dir", 1),
        ("base", " "),
        ("base", None),
        ("unknown", 1),
        ("rounds", True),
        ("rounds", -1),
        ("rounds", 4),
        ("rounds", 2.0),
        ("parallel", 0),
        ("parallel", 9),
        ("parallel", False),
        ("check_timeout", 0),
        ("check_timeout", True),
        ("check_timeout", 1.0),
        ("trailers", "x"),
        ("trailers", [""]),
        ("trailers", ["a\nb"]),
        ("trailers", ["a\rb"]),
        ("trailers", [2]),
        ("check", []),
        ("check", "make"),
        ("check", [""]),
        ("check", [" "]),
        ("check", ["make", 2]),
        ("check", ["make", "a\x00b"]),
        ("context", None),
        ("failure_model", []),
        ("read", ["relative"]),
        ("read", ["/a", "/a"]),
        ("read", "x"),
        ("read", [1]),
        ("tasks", None),
        ("tasks", [2]),
        ("writer", None),
        ("writer", {"engine": "codex"}),
        ("writer", {"model": "gpt-5"}),
        ("writer", {"engine": "other", "model": "gpt-5"}),
        ("writer", {"engine": "codex", "model": "gpt-5", "other": 1}),
        ("reviewer", {"engine": "codex", "model": "a b"}),
        ("reviewer", {"engine": "codex", "model": ""}),
        ("reviewer", {"engine": "codex", "model": "a@b"}),
        ("reviewer", {"engine": "codex", "model": "a", "effort": None}),
        ("reviewer", {"engine": "codex", "model": "a", "effort": "High"}),
    ],
)
def test_invalid_plan_field(
    raw: dict[str, Any], tmp_path: Path, key: str, value: object
) -> None:
    raw[key] = value
    with pytest.raises(plan.PlanError) as caught:
        parsed(raw, tmp_path)
    assert caught.value.code == 64
    assert key in str(caught.value)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("id", ""),
        ("id", "A"),
        ("id", "a" * 41),
        ("id", "a\n"),
        ("id", 1),
        ("task", ""),
        ("task", " \n"),
        ("task", 2),
        ("files", []),
        ("files", "x"),
        ("files", [2]),
        ("files", ["/a"]),
        ("files", ["a", "a"]),
        ("files", ["a/../b"]),
        ("files", ["../a"]),
        ("files", ["a//b"]),
        ("files", ["a/"]),
        ("files", [""]),
        ("files", ["a\x00b"]),
        ("files", ["a\n\x00b"]),
        ("files", ["./a"]),
        ("files", ["a/./b"]),
        ("files", ["."]),
        ("subject", ""),
        ("subject", "a\nb"),
        ("subject", "a\rb"),
        ("subject", "a" * 73),
        ("subject", 1),
        ("after", "a"),
        ("after", [1]),
        ("review", None),
        ("unknown", "x"),
    ],
)
def test_invalid_task_field(
    raw: dict[str, Any], tmp_path: Path, key: str, value: object
) -> None:
    raw["tasks"][0][key] = value
    with pytest.raises(plan.PlanError) as caught:
        parsed(raw, tmp_path)
    assert caught.value.code == 64
    assert key in str(caught.value)


@pytest.mark.parametrize("key", ["id", "subject", "task", "files"])
def test_missing_task_key(raw: dict[str, Any], tmp_path: Path, key: str) -> None:
    del raw["tasks"][0][key]
    with pytest.raises(plan.PlanError, match=key):
        parsed(raw, tmp_path)


@pytest.mark.parametrize("key", ["dir", "tasks"])
def test_missing_plan_key(raw: dict[str, Any], tmp_path: Path, key: str) -> None:
    del raw[key]
    with pytest.raises(plan.PlanError, match=key):
        parsed(raw, tmp_path)


def test_collects_three_problems(raw: dict[str, Any], tmp_path: Path) -> None:
    raw.update(rounds=4, parallel=0, context=1)
    with pytest.raises(plan.PlanError) as caught:
        parsed(raw, tmp_path)
    assert len(caught.value.problems) == 3


def test_saved_plan_keeps_resolved_subject(raw: dict[str, Any], tmp_path: Path) -> None:
    del raw["tasks"][0]["subject"]
    raw["tasks"][0]["task"] = "different objective"
    item = plan.from_json(raw, tmp_path / "plan.json", subjects={"a": "feat: resolved"})
    assert item.task("a").subject == "feat: resolved"


def test_empty_tasks_refused(raw: dict[str, Any], tmp_path: Path) -> None:
    raw["tasks"] = []
    with pytest.raises(plan.PlanError, match="tasks: expected at least 1") as caught:
        parsed(raw, tmp_path)
    assert caught.value.code == 64


@pytest.mark.parametrize(
    ("after", "message"), [(["a"], "itself"), (["missing"], "unknown id")]
)
def test_bad_dependency(
    raw: dict[str, Any], tmp_path: Path, after: list[str], message: str
) -> None:
    raw["tasks"][0]["after"] = after
    with pytest.raises(plan.PlanError, match=message):
        parsed(raw, tmp_path)


def test_duplicate_id(raw: dict[str, Any], tmp_path: Path) -> None:
    raw["tasks"].append(dict(raw["tasks"][0]))
    with pytest.raises(plan.PlanError, match="duplicate task id: a"):
        parsed(raw, tmp_path)


def test_named_cycle(raw: dict[str, Any], tmp_path: Path) -> None:
    raw["tasks"] = [
        {
            "id": name,
            "subject": "feat: task",
            "task": name,
            "files": [name],
            "after": [after],
        }
        for name, after in [("a", "b"), ("b", "c"), ("c", "a")]
    ]
    with pytest.raises(plan.PlanError, match="cycle: a -> b -> c -> a"):
        parsed(raw, tmp_path)


@pytest.mark.parametrize("text", ["", " \n", "[]", "null", "1", '"text"', "{"])
def test_bad_file_contents(tmp_path: Path, env: dict[str, str], text: str) -> None:
    path = tmp_path / "plan.json"
    path.write_text(text)
    with pytest.raises(plan.PlanError) as caught:
        plan.load(path)
    assert caught.value.code == 64


@pytest.mark.parametrize("kind", ["missing", "folder", "utf8"])
def test_unreadable(tmp_path: Path, env: dict[str, str], kind: str) -> None:
    path = tmp_path / "plan.json"
    if kind == "folder":
        path.mkdir()
    elif kind == "utf8":
        path.write_bytes(b"\xff")
    with pytest.raises(plan.PlanError) as caught:
        plan.load(path)
    assert caught.value.code == 66


@pytest.mark.parametrize(
    "text",
    [
        "codex:gpt-5",
        "codex:gpt-5@high",
        "opencode:openrouter/vendor/model:free@low",
        "claude:opus",
    ],
)
def test_worker_text(env: dict[str, str], text: str) -> None:
    worker = plan.parse_worker(text, "answer")
    assert worker.text() == text
    assert worker.to_json() == {
        "engine": worker.engine,
        "model": worker.model,
        "effort": worker.effort,
        "source": "answer",
    }


@pytest.mark.parametrize(
    "text",
    [
        "codex",
        "unknown:gpt",
        "codex:",
        "codex:gpt@High",
        "codex:gpt@",
        "codex:g pt",
        "codex:a@b@high",
        "codex:a@high\n",
    ],
)
def test_bad_worker(env: dict[str, str], text: str) -> None:
    with pytest.raises(plan.PlanError):
        plan.parse_worker(text, "option")


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [("a", "a", True), ("src", "src/a.py", True), ("src/a", "src/ab.py", False)],
)
def test_overlaps(env: dict[str, str], left: str, right: str, expected: bool) -> None:
    assert plan.overlaps(left, right) is expected
    assert plan.overlaps(right, left) is expected


def test_pick_wave(raw: dict[str, Any], tmp_path: Path) -> None:
    a = parsed(raw, tmp_path).task("a")
    b = replace(a, id="b", files=("src",))
    c = replace(a, id="c", files=("other",))
    assert plan.pick_wave([a, b, c]) == [a, c]
    assert plan.pick_wave([b, a, c]) == [b, c]
    assert plan.pick_wave([a, b, c], iter(["src"])) == [c]


@pytest.mark.parametrize(
    ("dependencies", "files", "expected"),
    [
        ([[], ["a"], ["b"]], ["a", "b", "c"], [["a"], ["b"], ["c"]]),
        (
            [[], ["a"], ["a"], ["b", "c"]],
            ["a", "b", "c", "d"],
            [["a"], ["b", "c"], ["d"]],
        ),
        ([[], [], []], ["same", "same", "other"], [["a", "c"], ["b"]]),
    ],
)
def test_waves(
    raw: dict[str, Any],
    tmp_path: Path,
    dependencies: list[list[str]],
    files: list[str],
    expected: list[list[str]],
) -> None:
    raw["tasks"] = [
        {
            "id": chr(97 + i),
            "subject": "feat: work",
            "task": "work",
            "files": [file],
            "after": after,
        }
        for i, (file, after) in enumerate(zip(files, dependencies, strict=True))
    ]
    raw["parallel"] = 1
    assert [
        [task.id for task in wave] for wave in plan.waves(parsed(raw, tmp_path))
    ] == expected


def test_warnings_at_base(
    raw: dict[str, Any], tmp_path: Path, env: dict[str, str]
) -> None:
    repo = make_repo(tmp_path, env)
    (repo / "src").mkdir()
    (repo / "src/a.py").write_text("a")
    git(repo, "add", ".", env=env)
    git(repo, "commit", "-qm", "test: add source", env=env)
    raw["tasks"][0]["files"] = ["src"]
    item = parsed(raw, tmp_path)
    hostile = {**env, "GIT_DIR": str(tmp_path / "absent")}
    assert plan.warnings(item, str(repo), hostile) == [
        "no failure_model; every unsettled case is a gap",
        "no check; waves are merged without a check",
        "a: src is a folder; list the files the task changes",
        "a: no test file in its files",
    ]
    raw.update(check=["true"], failure_model="cases")
    raw["tasks"][0]["files"] = ["tests/test_a.py"]
    assert plan.warnings(parsed(raw, tmp_path), str(repo), env) == []


@pytest.mark.parametrize("has_check", [True, False])
def test_writer_check_overlap_warnings(
    raw: dict[str, Any], tmp_path: Path, env: dict[str, str], has_check: bool
) -> None:
    repo = make_repo(tmp_path, env)
    check = ["make", "check"]
    raw.update(failure_model="cases", writer_check=[check])
    if has_check:
        raw["check"] = check
    first = raw["tasks"][0]
    first.update(id="inherited", files=["tests/test_a.py"])
    raw["tasks"] += [
        {**first, "id": "different", "writer_check": [["make", "test"]]},
        {**first, "id": "empty", "writer_check": []},
        {**first, "id": "explicit", "writer_check": [["ruff"], check, check]},
    ]
    assert plan.warnings(parsed(raw, tmp_path), str(repo), env) == (
        [
            "inherited: writer_check runs the plan's check",
            "explicit: writer_check runs the plan's check",
        ]
        if has_check
        else ["no check; waves are merged without a check"]
    )


@pytest.mark.parametrize(
    "file",
    [
        "test/a.py",
        "tests/a.py",
        "test_a.py",
        "a_test.go",
        "a.test.ts",
        "a.test.tsx",
        "a.spec.js",
        "src/__tests__/a.ts",
        "web/app/queue.test.ts",
        "Queue.test.tsx",
        "q.spec.ts",
        "__tests__/a.ts",
    ],
)
def test_test_file_warning(
    raw: dict[str, Any], tmp_path: Path, env: dict[str, str], file: str
) -> None:
    raw["tasks"][0]["files"] = [file]
    assert "a: no test file in its files" not in plan.warnings(
        parsed(raw, tmp_path), str(tmp_path), env
    )


@pytest.mark.parametrize(
    "file",
    [
        "src/a.ts",
        "a.testimony.ts",
        "a.special.js",
        "a.test",
        "a.spec",
        "src/my__tests__/a.ts",
        "src/__tests__helpers/a.ts",
        "src/__tests__",
        "src/a.test.ts/helper.ts",
        "src/a.spec.js/helper.js",
    ],
)
def test_non_test_file_warning(
    raw: dict[str, Any], tmp_path: Path, env: dict[str, str], file: str
) -> None:
    raw["tasks"][0]["files"] = [file]
    assert "a: no test file in its files" in plan.warnings(
        parsed(raw, tmp_path), str(tmp_path), env
    )


@pytest.mark.parametrize(
    ("files", "warns"),
    [
        (["README.md"], False),
        (["skill/references/flows.md", "skill/references/gap-check.md"], False),
        (["docs/reference.md"], False),
        (["docs/guide.rst", "docs/assets/diagram.svg"], False),
        (["README.md", "docs/guide.rst"], False),
        (["src/a.py"], True),
        (["README.md", "src/a.py"], True),
        (["src/a.py", "docs/reference.md"], True),
        (["docs/guide.rst", "src/a.py"], True),
        (["src/docs/guide.rst"], True),
        (["docs-extra/guide.rst"], True),
        (["docs.py"], True),
        (["README.mdx"], True),
    ],
)
def test_documentation_only_test_file_warning(
    raw: dict[str, Any],
    tmp_path: Path,
    env: dict[str, str],
    files: list[str],
    warns: bool,
) -> None:
    raw.update(check=["true"], failure_model="cases")
    raw["tasks"][0]["files"] = files
    assert plan.warnings(parsed(raw, tmp_path), str(tmp_path), env) == (
        ["a: no test file in its files"] if warns else []
    )


def test_documentation_folder_still_warns(
    raw: dict[str, Any], tmp_path: Path, env: dict[str, str]
) -> None:
    repo = make_repo(tmp_path, env)
    (repo / "docs").mkdir()
    (repo / "docs/guide.rst").write_text("Guide\n")
    git(repo, "add", ".", env=env)
    git(repo, "commit", "-qm", "docs: add guide", env=env)
    raw.update(check=["true"], failure_model="cases")
    raw["tasks"][0]["files"] = ["docs"]
    assert plan.warnings(parsed(raw, tmp_path), str(repo), env) == [
        "a: docs is a folder; list the files the task changes"
    ]


def test_resolution_order(
    raw: dict[str, Any], tmp_path: Path, env: dict[str, str]
) -> None:
    settings = choices.read(env=env)
    assert (
        plan.resolve("writer", None, parsed(raw, tmp_path), settings).source == "menu"
    )
    raw["writer"] = {"engine": "codex", "model": "gpt-5", "effort": "low"}
    item = parsed(raw, tmp_path)
    assert item.writer is not None
    assert (
        plan.resolve("writer", None, item, settings).to_json() == item.writer.to_json()
    )
    assert (
        plan.resolve("writer", "claude:other@high", item, settings).source == "option"
    )


@pytest.mark.parametrize("role", plan.ROLES)
@pytest.mark.parametrize("source", ["option", "plan", "menu", "same"])
def test_resolved_worker_announce_metadata(
    raw: dict[str, Any], tmp_path: Path, env: dict[str, str], role: str, source: str
) -> None:
    """Resolve announce labels and sources without adding stored worker fields."""
    option = None
    if source == "option":
        option = "claude:opus@high"
    elif source == "plan":
        raw[role] = {"engine": "claude", "model": "opus", "effort": "high"}
    else:
        choices.set_settings(
            {f"flow.{role}": "same" if source == "same" else "claude:opus"},
            env=env,
        )
    worker = plan.resolve(role, option, parsed(raw, tmp_path), choices.read(env=env))
    if source in {"option", "plan"}:
        assert (worker.screen, worker.row) == (source, role)
        assert worker.model_source == worker.effort_source == source
    else:
        expected_row = (
            f"Single tasks {role.title()}" if source == "same" else f"Flow {role}"
        )
        expected_source = f"single.{role}" if source == "same" else f"flow.{role}"
        assert (worker.screen, worker.row) == ("Team", expected_row)
        assert worker.model_source == worker.effort_source == expected_source
    assert worker.to_json() == {
        "engine": "claude",
        "model": "opus",
        "effort": "high",
        "source": "menu" if source == "same" else source,
    }


@pytest.mark.parametrize(
    "state", ["ask", "not shortlisted", "engine off", "subagent", "source error"]
)
def test_menu_refusal(
    raw: dict[str, Any], tmp_path: Path, env: dict[str, str], state: str
) -> None:
    if state == "ask":
        choices.set_settings({"single.writer": "ask"}, env=env)
    elif state == "not shortlisted":
        choices.set_settings({"shortlist.claude": []}, env=env)
    elif state == "engine off":
        choices.set_settings({"engine.claude": "off"}, env=env)
    elif state == "subagent":
        choices.set_settings({"single.writer_as": "subagent"}, env=env)
    settings = choices.read(env=env)
    if state == "source error":
        settings.update(source_error=True, menu=None, errors=["bad settings"])
    with pytest.raises(plan.WorkerError) as caught:
        plan.resolve("writer", None, parsed(raw, tmp_path), settings)
    assert caught.value.code == 64
    assert caught.value.line.startswith("owner:")
    assert "--writer" in caught.value.line


def test_explicit_worker_with_ask_rows(
    raw: dict[str, Any], tmp_path: Path, env: dict[str, str]
) -> None:
    choices.set_settings({"single.writer": "ask", "single.reviewer": "ask"}, env=env)
    settings = choices.read(env=env)
    worker = plan.resolve(
        "writer", "codex:unlisted@high", parsed(raw, tmp_path), settings
    )
    plan.check_worker(
        worker, settings, env, lambda name: StubDriver(name, script_path(env))
    )


@pytest.mark.parametrize(
    ("failure", "code", "line"),
    [
        ("off", 64, "switch codex on"),
        ("connection", 69, "codex login"),
        ("driver", 69, "no driver"),
        ("effort", 64, "next:"),
    ],
)
def test_check_worker_failure(
    env: dict[str, str], failure: str, code: int, line: str
) -> None:
    if failure == "off":
        choices.set_settings({"engine.codex": "off"}, env=env)
    elif failure == "connection":
        engine_cache(env, {"codex": False})
    worker = plan.parse_worker(
        "codex:gpt-5@" + ("max" if failure == "effort" else "high"), "option"
    )

    def driver(name: str) -> Driver:
        if failure == "driver":
            raise DriverMissingError("missing")
        return StubDriver(name, script_path(env))

    with pytest.raises(plan.WorkerError) as caught:
        plan.check_worker(worker, choices.read(env=env), env, driver)
    assert caught.value.code == code
    assert line in caught.value.line


def test_status_read_fallback(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    cached = status.cached(env)
    assert cached is not None
    calls: list[object] = []

    def read(environment: object) -> dict[str, status.EngineStatus]:
        calls.append(environment)
        return cached

    monkeypatch.setattr(status, "cached", lambda _: None)
    monkeypatch.setattr(status, "read", read)
    plan.check_worker(
        plan.Worker("codex", "gpt-5", None, "plan"),
        choices.read(env=env),
        env,
        lambda name: StubDriver(name, script_path(env)),
    )
    assert calls == [env]


def test_cited_sources_warn_until_a_read_file_supplies_them(
    raw: dict[str, Any], tmp_path: Path, env: dict[str, str]
) -> None:
    repo = make_repo(tmp_path, env)
    raw.update(check=["true"], failure_model="cases", context="Decided in #12.")
    raw["tasks"][0].update(task="Implement #93", files=["tests/test_a.py"])
    assert plan.warnings(parsed(raw, tmp_path), str(repo), env) == [
        "the context cites #12, which no read path supplies",
        "task a cites #93, which no read path supplies",
    ]
    sources = tmp_path / "sources"
    sources.mkdir()
    (sources / "issue-93.md").write_text("title: a\n")
    (sources / "decision.md").write_text("From #12\n")
    raw["read"] = [str(sources)]
    assert plan.warnings(parsed(raw, tmp_path), str(repo), env) == []


def test_standing_read_paths_supply_citations(
    raw: dict[str, Any], tmp_path: Path, env: dict[str, str]
) -> None:
    repo = make_repo(tmp_path, env)
    raw.update(check=["true"], failure_model="cases")
    raw["tasks"][0].update(task="Implement #93", files=["tests/test_a.py"])
    issue = tmp_path / "issue-93.md"
    issue.write_text("x\n")
    item = parsed(raw, tmp_path)
    assert plan.warnings(item, str(repo), env) == [
        "task a cites #93, which no read path supplies"
    ]
    assert plan.warnings(item, str(repo), env, [str(issue)]) == []


@pytest.mark.parametrize(
    "check",
    [[], [" "], ["\t\n"], ["a\0"], ["a", "\0"], "true", None, {}, [1], ["a", False]],
)
def test_check_option_uses_plan_validation(
    raw: dict[str, Any], tmp_path: Path, check: object
) -> None:
    raw["check"] = check
    with pytest.raises(plan.PlanError) as original:
        parsed(raw, tmp_path)
    with pytest.raises(plan.PlanError) as option:
        plan.parse_check(json.dumps(check))
    assert original.value.code == option.value.code == 64
    assert option.value.problems == tuple(
        p.removeprefix("plan.") for p in original.value.problems
    )


def test_check_option_allows_empty_later_argument() -> None:
    assert plan.parse_check('["command", ""]') == ["command", ""]
