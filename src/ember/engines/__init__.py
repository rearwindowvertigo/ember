"""The worker engines: Codex, OpenCode and Claude Code.

`ember.engines.status` reads whether each engine is installed, signed in and
connected. `ember.engines.models` reads each engine's model list.

This module defines the driver interface that `ember.runs.core` uses to run
one engine in a tmux session. The core owns the run record, the worktree,
commits, exits and the tmux session; a driver owns the engine's command line,
its files, flags and event formats. The driver for engine `<name>` is the
`driver()` factory of the module `ember.engines.<name>`.
"""

import importlib
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING

from ember import tmux

if TYPE_CHECKING:
    from ember.runs.record import Run

RUNNING = "running"
DONE = "done"
FAILED = "failed"
TURN_STATES = (RUNNING, DONE, FAILED)
"""The states of `TurnOutcome`."""

PROVIDER_KINDS = ("capacity", "usage_limit", "outage")
"""The kinds of provider failure."""

IDLE = "idle"
BUSY = "busy"
UNKNOWN = "unknown"
IDLE_STATES = (IDLE, BUSY, UNKNOWN)
"""The states of `IdleState`."""


class DriverError(RuntimeError):
    """The engine cannot be started or read; the core exits 71 with the reason."""


class DriverMissingError(LookupError):
    """Ember has no driver for the engine."""


@dataclass(frozen=True)
class LaunchSpec:
    """How to start an engine's interactive session for one run."""

    argv: tuple[str, ...]
    """The command and its arguments, run in the session's single pane."""

    env: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    """Variables added to or replacing the environment of the pane."""

    cwd: Path = Path()
    """The pane's working folder: the worktree, or the checkout when read-only."""

    first_message: str | None = None
    """The text the core pastes once `Driver.ready` is true; `None` pastes
    nothing, because the brief went in on the command line."""

    binary: str | None = None
    """The engine's executable when `argv[0]` is not it, such as a shell
    script that starts it; the sandbox keeps this executable's folder.
    `None` means `argv[0]`."""


@dataclass(frozen=True)
class TurnOutcome:
    """One observation of the current turn."""

    state: str
    """`running`, `done` or `failed`."""

    reply: str | None = None
    """The final reply text of a `done` turn; `None` otherwise."""

    reason: str | None = None
    """Why a `failed` turn failed; `None` otherwise."""

    provider_error: bool = False
    """The failure is a provider limit or outage."""

    signed_out: bool = False
    """The failure is a sign-in failure."""

    provider_kind: str | None = None
    """`capacity`, `usage_limit` or `outage`; None for older outcomes."""

    def __post_init__(self) -> None:
        """Check the state and provider kind.

        Raises:
            ValueError: The state or provider kind is unknown.
        """
        if self.provider_kind is not None and self.provider_kind not in PROVIDER_KINDS:
            raise ValueError(f"unknown provider kind {self.provider_kind!r}")
        if self.state not in TURN_STATES:
            raise ValueError(f"unknown turn state {self.state!r}")


@dataclass(frozen=True)
class IdleState:
    """Whether a run's session can take a message now."""

    state: str
    """`idle`, `busy` or `unknown`."""

    reason: str = ""
    """What the driver saw, for messages."""

    def __post_init__(self) -> None:
        """Check the state.

        Raises:
            ValueError: The state is not one of `IDLE_STATES`.
        """
        if self.state not in IDLE_STATES:
            raise ValueError(f"unknown idle state {self.state!r}")


class Driver(ABC):
    """Runs one engine in a run's tmux session.

    Every method takes the run as it is recorded, with its folder and
    environment. A method that cannot reach the engine raises `DriverError`;
    the default methods raise `TmuxError` when the session is gone.
    """

    name: str = ""
    """The engine: `codex`, `opencode` or `claude`."""

    @abstractmethod
    def command(self, run: Run, first_message: str) -> LaunchSpec:
        """Return how to start the engine's interactive session for a run.

        The model, effort, read-only mode, sandbox provider, network and read
        paths come from `run`. The core wraps the command in the run's
        sandbox afterwards and relies on these placements:

        - Codex: `argv[0]` is the `codex` executable itself, not a wrapper or
          a shell. The core inserts the sandbox's `-c` options right after
          `argv[0]` and finds the bundled `bwrap` from that executable.
        - OpenCode: `ember.sandbox.opencode_wrap` wraps the whole `argv` in
          `bwrap`; `argv[0]` is the `opencode` executable, whose folder is
          kept in the sandbox when it is in the home folder.
        - Claude: the command runs as returned, with no wrapping. The
          program it starts wraps each turn's `claude` in
          `ember.sandbox.claude_wrap` when the run's sandbox is
          `bubblewrap`.

        Args:
            run: The run, with status `starting`.
            first_message: The brief under the line that names Claude as its
                author, ready to paste.

        Returns:
            The command, its extra variables, its working folder and the
            first message to paste, or `None` when the command carries it.
        """

    @abstractmethod
    def ready(self, run: Run) -> bool:
        """Return whether the session can take its first message.

        Args:
            run: The run.

        Returns:
            `True` once the engine waits for input.
        """

    def send(self, run: Run, text: str) -> None:
        """Deliver text as the next message.

        Args:
            run: The run.
            text: The message.
        """
        tmux.send_text(run.id, text, env=run.env)

    @abstractmethod
    def wait(self, run: Run) -> TurnOutcome:
        """Observe the current turn once, without blocking past a short poll.

        The current turn is `run.turn`.

        Args:
            run: The run.

        Returns:
            `running`; `done` with the final reply; or `failed` with a reason,
            `provider_error` and `provider_kind` for a provider limit or outage,
            and `signed_out`
            for a sign-in failure.
        """

    @abstractmethod
    def idle_state(self, run: Run) -> IdleState:
        """Return whether the session can take a message now.

        Args:
            run: The run.

        Returns:
            `idle`, `busy` or `unknown`, with a reason.
        """

    def foreign_input(self, run: Run) -> str | None:
        """Return whether the session holds messages Ember did not send.

        The owner can type into a session through a terminal window. `--send`
        and `--close` without `--force` refuse a run for which this returns a
        description.

        Args:
            run: The run.

        Returns:
            `None` once the session's history was read and every message in
            it was sent by Ember; otherwise a short description of the
            messages Ember did not send, or of why the history could not be
            checked. The default, for an engine that cannot be typed into,
            is `None`.
        """
        return None

    def accept_input(self, run: Run) -> None:
        """Accept the messages `foreign_input` reports.

        `--send --force` calls this, so the same messages do not refuse later
        commands. The default does nothing.

        Args:
            run: The run.
        """
        return None

    def interrupt(self, run: Run) -> None:
        """Interrupt the current turn.

        Args:
            run: The run.
        """
        tmux.interrupt(run.id, env=run.env)

    def turn_output(self, run: Run) -> str:
        """Return this attempt's tool output for denied-access detection.

        Drivers without a transcript return an empty string; the core also
        checks the reply and new pane lines.
        """
        return ""

    def screen(self, run: Run) -> str:
        """Return the text of the session's pane.

        Args:
            run: The run.

        Returns:
            The visible pane.
        """
        return tmux.capture(run.id, env=run.env)

    def close(self, run: Run) -> None:  # noqa: B027
        """Clean up the engine's own state before the core kills the session.

        The core also calls it when a start fails after the driver was
        chosen, whatever was or was not started, so it must be safe to call
        more than once and on a run whose engine never started. The default
        does nothing.

        Args:
            run: The run.
        """

    @abstractmethod
    def valid_effort(self, effort: str) -> bool:
        """Return whether the engine takes an effort.

        Args:
            effort: The effort, such as `high`.

        Returns:
            `True` if the engine accepts it.
        """

    @abstractmethod
    def default_effort(self, model: str) -> str | None:
        """Return the effort a run of a model uses when none is chosen.

        Args:
            model: The model.

        Returns:
            The effort, or `None` to leave it to the engine.
        """


def load_driver(name: str, env: Mapping[str, str] | None = None) -> Driver:
    """Return the driver for an engine.

    Args:
        name: `codex`, `opencode` or `claude`.
        env: The run's environment, passed to the driver; `None` reads
            `os.environ`.

    Returns:
        A new driver from `ember.engines.<name>.driver(env)`.

    Raises:
        DriverMissingError: `name` is not an engine, or Ember has no driver
            module for it.
    """
    if name not in ("codex", "opencode", "claude"):
        raise DriverMissingError(f"unknown engine {name!r}")
    try:
        module = importlib.import_module(f"ember.engines.{name}")
    except ModuleNotFoundError as error:
        if error.name != f"ember.engines.{name}":
            raise
        raise DriverMissingError(f"Ember has no driver for {name}") from error
    driver = module.driver(env)
    if not isinstance(driver, Driver):
        raise DriverMissingError(f"ember.engines.{name}.driver(env) is not a Driver")
    return driver
