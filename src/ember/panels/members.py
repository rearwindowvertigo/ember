"""Parse panel members, resolve saved rows and check their engine gates."""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from ember.engines import Driver
from ember.flows.plan import (
    PlanError,
    Worker,
    WorkerError,
    check_worker,
    parse_worker,
)

MAX_MEMBERS = 8
NO_MEMBER_LINE = (
    "owner: name the members with --member <engine>:<model>[@<effort>], "
    "or set Panel on the Team screen"
)
HELP_LINE = "next: emb panel --help"


class MemberError(Exception):
    """A member set cannot run; code is the exit, line the next or owner line."""

    def __init__(self, code: int, message: str, line: str) -> None:
        """Keep the exit, action and any saved member exclusion details."""
        super().__init__(message)
        self.code = code
        self.line = line
        self.left_out: tuple[dict[str, str], ...] = ()


@dataclass(frozen=True)
class Member:
    """An engine, model and effort with its option or settings layer."""

    engine: str
    model: str
    effort: str | None
    source: str

    def text(self) -> str:
        """Return the member option syntax, omitting a null effort."""
        return f"{self.engine}:{self.model}" + (
            f"@{self.effort}" if self.effort is not None else ""
        )

    def entry(self, n: int) -> dict[str, Any]:
        """Return a pending panel record entry with independent empty lists."""
        return {
            "n": n,
            "engine": self.engine,
            "model": self.model,
            "effort": self.effort,
            "source": self.source,
            "status": "pending",
            "run": None,
            "exit": None,
            "reason": None,
            "reply": None,
            "bytes": 0,
            "changed": [],
            "seconds": None,
            "closed": False,
            "close_error": None,
            "attempts": [],
        }


@dataclass(frozen=True)
class Selection:
    """The members in run order and the saved entries left out with reasons."""

    members: tuple[Member, ...]
    left_out: tuple[dict[str, str], ...]


def parse_options(values: Sequence[str]) -> list[Member]:
    """Parse up to eight explicit members and refuse repeated engine/model pairs."""
    if len(values) > MAX_MEMBERS:
        raise MemberError(
            64, f"a panel has at most 8 members; got {len(values)}", HELP_LINE
        )
    members: list[Member] = []
    seen: set[tuple[str, str]] = set()
    for value in values:
        try:
            worker = parse_worker(value, "option")
        except PlanError as error:
            raise MemberError(64, f"--member {value}: {error}", HELP_LINE) from error
        pair = (worker.engine, worker.model)
        if pair in seen:
            raise MemberError(
                64, f"member {worker.engine}:{worker.model} is named twice", HELP_LINE
            )
        seen.add(pair)
        members.append(Member(worker.engine, worker.model, worker.effort, "option"))
    return members


def from_settings(
    settings: Mapping[str, Any],
) -> tuple[list[Member], list[dict[str, str]]]:
    """Read saved panel rows, retaining every unusable row's reason."""
    menu = settings.get("menu")
    if settings.get("source_error") or menu is None:
        reason = "; ".join(settings.get("errors", [])) or "not valid"
        raise MemberError(64, f"the settings cannot be read: {reason}", NO_MEMBER_LINE)
    source = settings["answers"]["panel.members"]["layer"]
    members: list[Member] = []
    left_out: list[dict[str, str]] = []
    for row in menu["panel.members"]["members"]:
        if row["state"] == "set":
            members.append(Member(row["engine"], row["model"], row["effort"], source))
        else:
            left_out.append({"value": row["value"], "reason": row["reason"]})
    return members, left_out


def check(
    member: Member,
    settings: Mapping[str, Any],
    env: Mapping[str, str],
    load_driver: Callable[[str], Driver],
) -> None:
    """Check the engine switch, connection, driver and explicit effort."""
    try:
        check_worker(
            Worker(member.engine, member.model, member.effort, member.source),
            settings,
            env,
            load_driver,
        )
    except WorkerError as error:
        line = HELP_LINE if error.line == "next: emb flow --help" else error.line
        raise MemberError(error.code, str(error), line) from error


def resolve_effort(member: Member, load_driver: Callable[[str], Driver]) -> Member:
    """Resolve the driver's default and validate a non-null effort."""
    driver = load_driver(member.engine)
    effort = member.effort
    if effort is None:
        effort = driver.default_effort(member.model)
    if effort is not None and not driver.valid_effort(effort):
        raise MemberError(
            64, f"{member.engine} does not take the effort {effort}", HELP_LINE
        )
    return replace(member, effort=effort)


def select(
    options: Sequence[str],
    settings: Mapping[str, Any],
    env: Mapping[str, str],
    load_driver: Callable[[str], Driver],
) -> Selection:
    """Choose explicit or saved members, preserving exclusions on errors."""
    candidates, left_out = (
        (parse_options(options), []) if options else from_settings(settings)
    )
    members: list[Member] = []
    for member in candidates:
        try:
            check(member, settings, env, load_driver)
            members.append(resolve_effort(member, load_driver))
        except MemberError as error:
            if options:
                raise
            left_out.append(
                {"value": f"{member.engine}:{member.model}", "reason": str(error)}
            )
    if not members:
        message = "no panel member to run"
        if left_out:
            message += ": every saved member was left out"
        error = MemberError(64, message, NO_MEMBER_LINE)
        error.left_out = tuple(left_out)
        raise error
    return Selection(tuple(members), tuple(left_out))
