"""Load flow plans, select workers and form waves of disjoint tasks."""

import json
import re
import subprocess
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from importlib import resources
from pathlib import Path, PurePosixPath
from typing import Any

from ember.engines import Driver, DriverMissingError, status
from ember.runs import exits

ROLES = ("writer", "reviewer")
ROW = {"writer": "flow.writer", "reviewer": "flow.reviewer"}
SCHEMA_NAME = "flow-plan.schema.json"
SUBJECT_LIMIT = 72
TASK_ID = r"[a-z0-9][a-z0-9-]{0,39}"
GIT_TIMEOUT = 10
_GIT_ENV = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_COMMON_DIR",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_NAMESPACE",
    "GIT_PREFIX",
    "EMBER_FLOW_RUNNER",
)


class PlanError(Exception):
    """A plan cannot be read (66) or has validation problems (64)."""

    def __init__(self, code: int, problems: Iterable[str]) -> None:
        """Keep every problem in reporting order."""
        self.code = code
        self.problems = tuple(problems)
        super().__init__("; ".join(self.problems))


class WorkerError(Exception):
    """A worker cannot run; code and line describe the next action."""

    def __init__(self, code: int, message: str, line: str) -> None:
        """Keep the exit code and action line with the diagnostic."""
        super().__init__(message)
        self.code = code
        self.line = line


@dataclass(frozen=True)
class Worker:
    """An engine, model and optional effort with its selection source."""

    engine: str
    model: str
    effort: str | None
    source: str

    def text(self) -> str:
        """Return the worker option syntax."""
        return f"{self.engine}:{self.model}" + (
            f"@{self.effort}" if self.effort is not None else ""
        )

    def to_json(self) -> dict[str, Any]:
        """Return the four stored worker fields."""
        return asdict(self)


@dataclass(frozen=True)
class Task:
    """One task's brief, file declarations and dependencies."""

    id: str
    subject: str
    task: str
    files: tuple[str, ...]
    after: tuple[str, ...]
    review: str


@dataclass(frozen=True)
class Plan:
    """A validated plan and its original source text."""

    path: Path
    text: str
    dir: str
    base: str
    trailers: tuple[str, ...]
    writer: Worker | None
    reviewer: Worker | None
    rounds: int
    parallel: int
    check: tuple[str, ...]
    check_timeout: int
    context: str
    failure_model: str
    read: tuple[str, ...]
    tasks: tuple[Task, ...]
    rounds_source: str = "plan"
    """Where `rounds` came from: `plan`, `setting`, or `default` before the
    owner's `flow.review_rounds` row is applied."""
    parallel_source: str = "plan"
    """Where `parallel` came from: `plan`, `setting`, or `default` before the
    owner's `flow.parallel` row is applied."""

    def task(self, task_id: str) -> Task:
        """Find a task by id, raising KeyError if it is absent."""
        for task in self.tasks:
            if task.id == task_id:
                return task
        raise KeyError(task_id)


def schema_text() -> str:
    """Read the packaged plan schema as UTF-8."""
    return resources.files("ember").joinpath("data", SCHEMA_NAME).read_text("utf-8")


def _validate(
    value: object, schema: Mapping[str, Any], root: Mapping[str, Any], path: str
) -> list[str]:
    """Apply the JSON Schema keywords used by the packaged plan schema."""
    if "$ref" in schema:
        return _validate(
            value, root["$defs"][schema["$ref"].split("/")[-1]], root, path
        )
    kind = schema.get("type")
    types = {"object": dict, "array": list, "string": str, "integer": int}
    if kind in types and type(value) is not types[kind]:
        return [f"{path}: expected {kind}"]
    problems: list[str] = []
    if "const" in schema and value != schema["const"]:
        problems.append(f"{path}: expected {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        problems.append(f"{path}: expected one of {', '.join(schema['enum'])}")
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        problems.extend(
            f"{path}: missing required key {key!r}"
            for key in schema.get("required", [])
            if key not in value
        )
        if schema.get("additionalProperties") is False:
            problems.extend(
                f"{path}: unexpected key {key!r}"
                for key in sorted(value.keys() - properties.keys())
            )
        for key, item in properties.items():
            if key in value:
                problems.extend(_validate(value[key], item, root, f"{path}.{key}"))
    elif isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            problems.append(f"{path}: expected at least {schema['minItems']} entries")
        if schema.get("uniqueItems"):
            seen: list[Any] = []
            for index, item in enumerate(value):
                if item in seen:
                    problems.append(f"{path}[{index}]: duplicate entry")
                seen.append(item)
        prefix = schema.get("prefixItems", [])
        for index, item in enumerate(value):
            child = prefix[index] if index < len(prefix) else schema.get("items", {})
            problems.extend(_validate(item, child, root, f"{path}[{index}]"))
    elif isinstance(value, str):
        if "pattern" in schema and re.search(schema["pattern"], value) is None:
            problems.append(
                f"{path}: expected {schema.get('description', schema['pattern'])}"
            )
        if len(value) > schema.get("maxLength", len(value)):
            problems.append(f"{path}: at most {schema['maxLength']} characters")
    elif type(value) is int:
        if value < schema.get("minimum", value):
            problems.append(f"{path}: expected at least {schema['minimum']}")
        if value > schema.get("maximum", value):
            problems.append(f"{path}: expected at most {schema['maximum']}")
    return problems


def _dependencies(tasks: list[Any]) -> list[str]:
    """Report duplicate ids, invalid dependency references and named cycles."""
    graph: dict[str, list[str]] = {}
    problems: list[str] = []
    for task in tasks:
        if not isinstance(task, dict) or not isinstance(task.get("id"), str):
            continue
        name = task["id"]
        if name in graph:
            problems.append(f"duplicate task id: {name}")
        after = task.get("after", [])
        graph[name] = (
            [item for item in after if isinstance(item, str)]
            if isinstance(after, list)
            else []
        )
    for name, after in graph.items():
        for dependency in after:
            if dependency == name:
                problems.append(f"{name}: after names itself")
            elif dependency not in graph:
                problems.append(f"{name}: after names unknown id: {dependency}")
    # Iterative depth-first traversal also handles plans above the recursion limit.
    done: set[str] = set()
    for start in graph:
        if start in done:
            continue
        route: list[str] = [start]
        active = {start: 0}
        stack = [iter(graph[start])]
        while stack:
            dependency = next(stack[-1], None)
            if dependency is None:
                done.add(route.pop())
                active.pop(next(reversed(active)))
                stack.pop()
            elif dependency in active:
                if dependency != route[-1]:
                    cycle = route[active[dependency] :] + [dependency]
                    problems.append("cycle: " + " -> ".join(cycle))
            elif dependency in graph and dependency not in done:
                active[dependency] = len(route)
                route.append(dependency)
                stack.append(iter(graph[dependency]))
    return problems


def parse(text: str, path: Path) -> Plan:
    """Validate JSON text, collecting all independent problems before raising."""
    try:
        value = json.loads(text)
    except ValueError as error:
        raise PlanError(64, [f"plan is not valid JSON: {error}"]) from error
    schema = json.loads(schema_text())
    problems = _validate(value, schema, schema, "plan")
    if isinstance(value, dict) and isinstance(value.get("tasks"), list):
        problems.extend(_dependencies(value["tasks"]))
    if problems:
        raise PlanError(64, problems)
    workers = {
        role: Worker(
            engine=value[role]["engine"],
            model=value[role]["model"],
            effort=value[role].get("effort"),
            source="plan",
        )
        if role in value
        else None
        for role in ROLES
    }
    tasks = tuple(
        Task(
            id=item["id"],
            subject=item["subject"],
            task=item["task"],
            files=tuple(item["files"]),
            after=tuple(item.get("after", [])),
            review=item.get("review", ""),
        )
        for item in value["tasks"]
    )
    return Plan(
        path=path.absolute(),
        text=text,
        dir=value["dir"],
        base=value.get("base", "HEAD"),
        trailers=tuple(value.get("trailers", [])),
        writer=workers["writer"],
        reviewer=workers["reviewer"],
        rounds=value.get("rounds", 2),
        rounds_source="plan" if "rounds" in value else "default",
        parallel=value.get("parallel", 4),
        parallel_source="plan" if "parallel" in value else "default",
        check=tuple(value.get("check", [])),
        check_timeout=value.get("check_timeout", 1800),
        context=value.get("context", ""),
        failure_model=value.get("failure_model", ""),
        read=tuple(value.get("read", [])),
        tasks=tasks,
    )


def from_json(
    value: Mapping[str, Any], path: Path, *, subjects: Mapping[str, str] | None = None
) -> Plan:
    """Validate a plan, retaining resolved subjects when loading a saved flow."""
    document = dict(value)
    if subjects is not None and isinstance(document.get("tasks"), list):
        document["tasks"] = [
            {**item, "subject": subjects[item["id"]]}
            if (
                isinstance(item, dict)
                and isinstance(item.get("id"), str)
                and item["id"] in subjects
            )
            else item
            for item in document["tasks"]
        ]
    return parse(json.dumps(document), path)


def load(path: str | Path) -> Plan:
    """Read a UTF-8 plan, reporting unreadable files with code 66."""
    source = Path(path).absolute()
    try:
        text = source.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise PlanError(66, [f"cannot read {source}: {error}"]) from error
    return parse(text, source)


def parse_worker(text: str, source: str) -> Worker:
    """Parse an engine and model, with an optional lowercase effort suffix."""
    engine, colon, model = text.partition(":")
    effort = None
    if "@" in model:
        model, _, effort = model.rpartition("@")
    value = {"engine": engine, "model": model}
    if effort is not None:
        value["effort"] = effort
    schema = json.loads(schema_text())
    problems = _validate(value, schema["$defs"]["worker"], schema, "worker")
    if not colon or problems:
        raise PlanError(
            64, problems or ["worker: expected <engine>:<model>[@<effort>]"]
        )
    return Worker(engine, model, effort, source)


def overlaps(left: str, right: str) -> bool:
    """Test equality or a complete POSIX folder prefix in either direction."""
    a, b = PurePosixPath(left).parts, PurePosixPath(right).parts
    return a[: len(b)] == b or b[: len(a)] == a


def pick_wave(candidates: Sequence[Task], busy_files: Iterable[str] = ()) -> list[Task]:
    """Greedily pick tasks whose files overlap neither busy nor picked files."""
    occupied = list(busy_files)
    picked: list[Task] = []
    for task in candidates:
        if not any(overlaps(a, b) for a in task.files for b in occupied):
            picked.append(task)
            occupied.extend(task.files)
    return picked


def waves(plan: Plan) -> list[list[Task]]:
    """Form dry-run waves, assuming each preceding wave merges completely."""
    pending = list(plan.tasks)
    merged: set[str] = set()
    result: list[list[Task]] = []
    while pending:
        wave = pick_wave([task for task in pending if set(task.after) <= merged])
        if not wave:
            raise PlanError(64, ["no task can run: unresolved dependencies"])
        result.append(wave)
        merged.update(task.id for task in wave)
        pending = [task for task in pending if task.id not in merged]
    return result


def warnings(plan: Plan, top: str, env: Mapping[str, str]) -> list[str]:
    """Describe missing checks, missing tests and folders declared as files."""
    result: list[str] = []
    if not plan.failure_model.strip():
        result.append("no failure_model; every unsettled case is a gap")
    if not plan.check:
        result.append("no check; waves are merged without a check")
    clean_env = {key: value for key, value in env.items() if key not in _GIT_ENV}
    for task in plan.tasks:
        for file in task.files:
            try:
                output = subprocess.run(
                    ["git", "-C", top, "cat-file", "-t", f"{plan.base}:{file}"],
                    env=clean_env,
                    capture_output=True,
                    text=True,
                    timeout=GIT_TIMEOUT,
                    check=False,
                )
            except OSError, subprocess.TimeoutExpired:
                continue
            if output.returncode == 0 and output.stdout.strip() == "tree":
                result.append(
                    f"{task.id}: {file} is a folder; list the files the task changes"
                )
        if not any(_test_file(file) for file in task.files):
            result.append(f"{task.id}: no test file in its files")
    return result


def _test_file(file: str) -> bool:
    """Recognize test folders and conventional test filenames."""
    path = PurePosixPath(file)
    return bool(
        {"test", "tests"}.intersection(path.parts[:-1])
        or path.name.startswith("test_")
        or re.search(r"_test\.", path.name)
    )


def resolve(
    role: str, option: str | None, plan: Plan, settings: Mapping[str, Any]
) -> Worker:
    """Resolve a worker from the option, plan, then a usable menu row."""
    if role not in ROLES:
        raise ValueError(f"unknown role: {role}")
    if option is not None:
        return parse_worker(option, "option")
    worker = getattr(plan, role)
    if worker is not None:
        return worker
    line = (
        f"owner: name the flow's {role} with --{role}, "
        f"or set Flow {role} on the Team screen"
    )
    menu = settings.get("menu")
    if settings.get("source_error") or menu is None:
        reason = "; ".join(settings.get("errors", [])) or "not valid"
        raise WorkerError(64, f"the settings cannot be read: {reason}", line)
    row = menu[ROW[role]]
    # A flow row set to `same` reports the single-task row it follows.
    name = f"single.{role}" if row.get("same") else ROW[role]
    if row["state"] != "set":
        raise WorkerError(64, f"{name} is {row['state']}: {row['reason']}", line)
    if row["engine"] == "claude" and row.get("run_as") == "subagent":
        raise WorkerError(
            64,
            f"{name} runs claude:{row['model']} as a subagent; a flow runs CLI workers",
            f"owner: name the flow's {role} with --{role} claude:{row['model']}, "
            f"or set Flow {role} to a CLI worker on the Team screen",
        )
    return Worker(row["engine"], row["model"], row["effort"], "menu")


def check_worker(
    worker: Worker,
    settings: Mapping[str, Any],
    env: Mapping[str, str],
    load_driver: Callable[[str], Driver],
) -> None:
    """Check the engine switch, connection, driver and explicit effort."""
    engine = worker.engine
    if settings["answers"][f"engine.{engine}"]["value"] != "on":
        raise WorkerError(
            64,
            f"{engine} is switched off",
            exits.line(64, "engine_off", engine=engine) or "",
        )
    held = status.cached(env)
    if held is None:
        held = status.read(env)
    item = held[engine]
    if not item.connected:
        fix = next(
            (req.fix for req in item.requirements if not req.met and req.fix), None
        )
        raise WorkerError(
            69,
            f"{engine} is not connected: {item.reason or item.state}",
            exits.line(69, "fix" if fix else None, engine=engine, fix=fix) or "",
        )
    try:
        driver = load_driver(engine)
    except DriverMissingError as error:
        raise WorkerError(
            69, str(error), exits.line(69, "driver", engine=engine) or ""
        ) from error
    if worker.effort is not None and not driver.valid_effort(worker.effort):
        raise WorkerError(
            64,
            f"{engine} does not take the effort {worker.effort}",
            "next: emb flow --help",
        )
