import dataclasses
import sys
import types
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest

from ember import engines
from ember.engines import Driver, IdleState, LaunchSpec, TurnOutcome
from ember.runs.record import Run


class Minimal(Driver):
    name = "codex"

    def command(self, run: Run, first_message: str) -> LaunchSpec:
        return LaunchSpec(argv=("codex",), cwd=Path(run.worktree))

    def ready(self, run: Run) -> bool:
        return True

    def wait(self, run: Run) -> TurnOutcome:
        return TurnOutcome(engines.RUNNING)

    def idle_state(self, run: Run) -> IdleState:
        return IdleState(engines.IDLE)

    def valid_effort(self, effort: str) -> bool:
        return True

    def default_effort(self, model: str) -> str | None:
        return None


def _run(env: dict[str, str]) -> Run:
    return cast(Run, types.SimpleNamespace(id="codex-1-abcdef", env=env, worktree="/w"))


def test_value_types_are_frozen_with_defaults() -> None:
    outcome = TurnOutcome(engines.DONE, reply="ok")
    assert (outcome.reason, outcome.provider_error, outcome.signed_out) == (
        None,
        False,
        False,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        outcome.state = engines.FAILED  # type: ignore[misc]
    spec = LaunchSpec(argv=("a",))
    assert dict(spec.env) == {}
    assert spec.first_message is None
    assert IdleState(engines.BUSY).reason == ""


def test_states_are_checked() -> None:
    with pytest.raises(ValueError, match="turn state"):
        TurnOutcome("finished")
    with pytest.raises(ValueError, match="idle state"):
        IdleState("asleep")


def test_default_methods_use_the_run_session(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    def record(name: str) -> Callable[..., str]:
        def call(*args: object, **kwargs: object) -> str:
            calls.append((name, args, kwargs))
            return "screen text"

        return call

    for name in ("send_text", "interrupt", "capture"):
        monkeypatch.setattr(engines.tmux, name, record(name))
    env = {"PATH": "/x"}
    driver = Minimal()
    run = _run(env)
    driver.send(run, "hello")
    driver.interrupt(run)
    assert driver.screen(run) == "screen text"
    driver.close(run)
    assert calls == [
        ("send_text", ("codex-1-abcdef", "hello"), {"env": env}),
        ("interrupt", ("codex-1-abcdef",), {"env": env}),
        ("capture", ("codex-1-abcdef",), {"env": env}),
    ]


def test_driver_needs_its_abstract_methods() -> None:
    with pytest.raises(TypeError):
        Driver()  # type: ignore[abstract]


def test_load_driver_from_engine_module(monkeypatch: pytest.MonkeyPatch) -> None:
    received: list[object] = []

    def factory(env: object) -> Minimal:
        received.append(env)
        return Minimal()

    module = types.ModuleType("ember.engines.codex")
    module.driver = factory  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "ember.engines.codex", module)
    env = {"HOME": "/home/test"}
    assert isinstance(engines.load_driver("codex", env), Minimal)
    assert received == [env]


def test_load_driver_without_module(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "ember.engines.opencode", None)
    with pytest.raises(engines.DriverMissingError, match="no driver for opencode"):
        engines.load_driver("opencode")


def test_load_driver_that_is_not_a_driver(monkeypatch: pytest.MonkeyPatch) -> None:
    module = types.ModuleType("ember.engines.claude")
    module.driver = lambda env: object()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "ember.engines.claude", module)
    with pytest.raises(engines.DriverMissingError, match="is not a Driver"):
        engines.load_driver("claude")


def test_load_driver_unknown_engine() -> None:
    with pytest.raises(engines.DriverMissingError, match="unknown engine"):
        engines.load_driver("status")
