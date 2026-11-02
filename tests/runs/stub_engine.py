"""A stub tmux, a stub engine and its driver, for run tests.

The stub `tmux` keeps its sessions in `<tmp>/tmux-state/`. `new-session`
starts the pane's command as a detached process; keys and pastes are appended
to the session's `input.jsonl`, and `capture-pane` prints its `screen` file.

The stub engine runs in the pane. It reads messages from `input.jsonl` (text
up to an Enter key), acts on `@` lines in each message, writes the screen and
writes each turn's outcome to `turn-<n>.json` in its output folder:

- `@write <path> <text>`: write a file in the working folder.
- `@reply <text>`: the reply (default the `reply_file` text when supplied,
  otherwise `done: <first line>`).
- `@empty`: an empty reply.
- `@sleep <seconds>`: work for a while, changing the screen.
- `@hang`: stop changing the screen until `release` exists in the output
  folder.
- `@fail`, `@provider`, `@signedout <reason>`: a failed turn.
- `@exit`: end the engine without a reply.
- `@git <args>`: run git in the working folder.
"""

import contextlib
import json
import os
import shutil
import signal
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path

from ember import choices, engines
from ember.engines import Driver, IdleState, LaunchSpec, TurnOutcome, status
from ember.runs.record import Run

STUB_TMUX = r"""#!{python}
import json, os, shutil, signal, subprocess, sys
from pathlib import Path

STATE = Path(__file__).resolve().parent.parent / "tmux-state"
SESSIONS = STATE / "sessions"
BUFFERS = STATE / "buffers"
SESSIONS.mkdir(parents=True, exist_ok=True)
BUFFERS.mkdir(parents=True, exist_ok=True)
args = sys.argv[1:]
sock = None
if args[:1] == ["-S"]:
    sock, args = args[1], args[2:]
data = sys.stdin.read()
with open(STATE / "log.jsonl", "a") as log:
    log.write(json.dumps({{"args": args, "stdin": data}}) + "\n")


def fail(message):
    sys.stderr.write(message + "\n")
    sys.exit(1)


def session(value):
    name = value.lstrip("=").rstrip(":")
    folder = SESSIONS / name
    if not folder.is_dir():
        fail(f"can't find session: {{name}}")
    return folder


def alive(pid):
    try:
        text = Path(f"/proc/{{pid}}/stat").read_text()
    except OSError:
        return False
    return text.rsplit(")", 1)[1].split()[0] != "Z"


def event(folder, item):
    with open(folder / "input.jsonl", "a") as stream:
        stream.write(json.dumps(item) + "\n")


commands, current = [], []
for arg in args:
    if arg == ";":
        commands.append(current)
        current = []
    else:
        current.append(arg)
commands.append(current)
for command in commands:
    name, rest = command[0], command[1:]
    if name == "set-option":
        continue
    if name == "new-session":
        env, values, line, i = {{}}, {{}}, None, 0
        while i < len(rest):
            if rest[i] == "-d":
                i += 1
            elif rest[i] == "-e":
                key, _, value = rest[i + 1].partition("=")
                env[key] = value
                i += 2
            elif rest[i] in ("-s", "-x", "-y", "-c"):
                values[rest[i]] = rest[i + 1]
                i += 2
            else:
                line = rest[i]
                i += 1
        folder = SESSIONS / values["-s"]
        if folder.exists():
            fail(f"duplicate session: {{values['-s']}}")
        folder.mkdir()
        (folder / "input.jsonl").write_text("")
        (folder / "screen").write_text("")
        command = {{"line": line, "env": env, "cwd": values["-c"]}}
        (folder / "command").write_text(json.dumps(command))
        if sock:
            Path(sock).touch()
        env["STUB_PANE"] = str(folder)
        output = open(folder / "pane.log", "w")
        process = subprocess.Popen(
            ["/bin/sh", "-c", line], cwd=values["-c"], env=env,
            stdin=subprocess.DEVNULL, stdout=output, stderr=output,
            start_new_session=True,
        )
        (folder / "pid").write_text(str(process.pid))
    elif name == "has-session":
        session(rest[rest.index("-t") + 1])
    elif name == "send-keys":
        folder = session(rest[rest.index("-t") + 1])
        if "-l" in rest:
            event(folder, {{"text": rest[rest.index("--") + 1]}})
        else:
            for key in rest[rest.index("-t") + 2:]:
                event(folder, {{"key": key}})
    elif name == "load-buffer":
        (BUFFERS / rest[rest.index("-b") + 1]).write_text(data)
    elif name == "paste-buffer":
        folder = session(rest[rest.index("-t") + 1])
        buffer = BUFFERS / rest[rest.index("-b") + 1]
        event(folder, {{"text": buffer.read_text()}})
        buffer.unlink()
    elif name == "capture-pane":
        folder = session(rest[rest.index("-t") + 1])
        sys.stdout.write((folder / "screen").read_text())
    elif name == "display-message":
        folder = session(rest[rest.index("-t") + 1])
        pid = (folder / "pid").read_text()
        dead = "0" if alive(int(pid)) else "1"
        form = rest[-1]
        form = form.replace("#{{pane_dead}}", dead)
        sys.stdout.write(form.replace("#{{pane_pid}}", pid) + "\n")
    elif name == "kill-session":
        folder = session(rest[rest.index("-t") + 1])
        try:
            os.killpg(int((folder / "pid").read_text()), signal.SIGKILL)
        except OSError:
            pass
        shutil.rmtree(folder)
    elif name == "list-sessions":
        for folder in sorted(SESSIONS.iterdir()):
            sys.stdout.write(folder.name + "\n")
    else:
        fail(f"unknown command {{name}}")
"""

ENGINE_SCRIPT = r"""
import json, os, subprocess, sys, time
from pathlib import Path

out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)
pane = Path(os.environ["STUB_PANE"])
screen = pane / "screen"
born = time.time()


def show(text):
    temporary = pane / ".screen"
    temporary.write_text(text)
    os.replace(temporary, screen)


def outcome(turn, **values):
    temporary = out / f".turn-{turn}.json"
    temporary.write_text(json.dumps(values))
    os.replace(temporary, out / f"turn-{turn}.json")


def check_alive():
    if not pane.exists() or time.time() - born > 120:
        sys.exit(0)


def handle(message, turn):
    lines = message.splitlines()
    first = next((line for line in lines if line.strip()), "")
    reply_file = os.environ.get("STUB_REPLY_FILE")
    reply = Path(reply_file).read_text() if reply_file else f"done: {first}"
    scripts = os.environ.get("STUB_SCRIPTS")
    if scripts:
        for match, script in json.loads(Path(scripts).read_text()).items():
            if match in message:
                lines.extend(script.splitlines())
    for line in lines:
        word, _, rest = line.partition(" ")
        if word == "@write":
            name, _, text = rest.partition(" ")
            path = Path(name)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text + "\n")
        elif word == "@reply":
            reply = rest
        elif word == "@empty":
            reply = ""
        elif word == "@sleep":
            end, tick = time.time() + float(rest), 0
            while time.time() < end:
                tick += 1
                show(f"turn {turn} working {tick}\n")
                time.sleep(0.05)
        elif word == "@hang":
            show(f"turn {turn} hanging\n")
            while not (out / "release").exists():
                check_alive()
                time.sleep(0.05)
        elif word in ("@fail", "@provider", "@signedout"):
            show(f"turn {turn} failed\n")
            outcome(
                turn, state="failed", reason=rest,
                provider_error=word == "@provider", signed_out=word == "@signedout",
            )
            return
        elif word == "@exit":
            sys.exit(0)
        elif word == "@git":
            subprocess.run(["git", *rest.split()], check=False)
    show(f"turn {turn} done\n")
    outcome(turn, state="done", reply=reply)


show("stub engine starting\n")
time.sleep(float(os.environ.get("STUB_READY_DELAY", "0")))
show("stub engine ready\n")
turn = 0
if len(sys.argv) > 2:
    turn = 1
    handle(sys.argv[2], 1)
(out / "ready").write_text("")
offset, buffer = 0, ""
while True:
    check_alive()
    data = (pane / "input.jsonl").read_text()
    end = data.rfind("\n") + 1
    for line in data[offset:end].splitlines():
        item = json.loads(line)
        if "text" in item:
            buffer += item["text"]
        elif item["key"] == "Enter":
            turn += 1
            message, buffer = buffer, ""
            handle(message, turn)
        elif item["key"] == "C-c":
            with open(out / "interrupts", "a") as stream:
                stream.write(f"{turn}\n")
    offset = end
    time.sleep(0.02)
"""


class StubDriver(Driver):
    """Runs the stub engine; each turn's outcome is a file it writes."""

    def __init__(
        self,
        name: str,
        script: Path,
        *,
        paste: bool = True,
        on_wait: Callable[[Run], None] | None = None,
        idle: str | None = None,
        foreign: str | None = None,
        reply_file: Path | None = None,
        scripts: Path | None = None,
    ) -> None:
        self.name = name
        self.script = script
        self.paste = paste
        self.on_wait = on_wait
        self.idle = idle
        self.foreign = foreign
        self.reply_file = reply_file
        self.scripts = scripts
        self.foreign_checks = 0
        self.commands: list[tuple[Run, str]] = []
        self.closed: list[str] = []

    def out(self, run: Run) -> Path:
        return run.folder / "stub"

    def command(self, run: Run, first_message: str) -> LaunchSpec:
        self.commands.append((run, first_message))
        argv: tuple[str, ...] = (sys.executable, "-I", str(self.script))
        argv += (str(self.out(run)),)
        if not self.paste:
            argv += (first_message,)
        environment = {"STUB_ENGINE": self.name}
        if self.scripts is not None:
            environment["STUB_SCRIPTS"] = str(self.scripts)
        if self.reply_file is not None:
            environment["STUB_REPLY_FILE"] = str(self.reply_file)
        return LaunchSpec(
            argv=argv,
            env=environment,
            cwd=Path(run.worktree),
            first_message=first_message if self.paste else None,
        )

    def ready(self, run: Run) -> bool:
        return (self.out(run) / "ready").exists()

    def wait(self, run: Run) -> TurnOutcome:
        if self.on_wait is not None:
            self.on_wait(run)
        path = self.out(run) / f"turn-{run.turn}.json"
        try:
            data = json.loads(path.read_text())
        except FileNotFoundError:
            return TurnOutcome(engines.RUNNING)
        return TurnOutcome(
            data["state"],
            reply=data.get("reply"),
            reason=data.get("reason"),
            provider_error=data.get("provider_error", False),
            signed_out=data.get("signed_out", False),
        )

    def idle_state(self, run: Run) -> IdleState:
        if self.idle is not None:
            return IdleState(self.idle, "set by the test")
        if (self.out(run) / f"turn-{run.turn}.json").exists():
            return IdleState(engines.IDLE, "the turn's outcome is written")
        return IdleState(engines.BUSY, "the turn is running")

    def foreign_input(self, run: Run) -> str | None:
        self.foreign_checks += 1
        return self.foreign

    def accept_input(self, run: Run) -> None:
        self.foreign = None

    def close(self, run: Run) -> None:
        self.closed.append(run.id)

    def valid_effort(self, effort: str) -> bool:
        return effort in ("low", "medium", "high")

    def default_effort(self, model: str) -> str | None:
        return "medium"


def git(cwd: Path, *args: str, env: Mapping[str, str]) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        env=dict(env),
        check=True,
    )
    return result.stdout.strip()


def engine_cache(env: Mapping[str, str], connected: Mapping[str, bool]) -> None:
    payload = {
        "engines": {
            engine: status.EngineStatus(
                engine=engine,
                state=status.CONNECTED
                if connected.get(engine, True)
                else status.NOT_SIGNED_IN,
                reason=None if connected.get(engine, True) else "signed out",
                installed=True,
                binary=f"/usr/bin/{engine}",
                version="1",
                signed_in=connected.get(engine, True),
                connected=connected.get(engine, True),
                requirements=()
                if connected.get(engine, True)
                else (
                    status.Requirement(
                        label="signed in", met=False, fix=f"{engine} login"
                    ),
                ),
                checked_at=1.0,
            ).to_json()
            for engine in status.ENGINES
        }
    }
    status.save_cache_file(status.cache_path(env), payload, env)


def make_env(tmp_path: Path) -> dict[str, str]:
    """Return an environment with the stub tmux first on PATH."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    tmux = bin_dir / "tmux"
    tmux.write_text(STUB_TMUX.format(python=sys.executable))
    tmux.chmod(0o755)
    (bin_dir / "stub_engine_script.py").write_text(ENGINE_SCRIPT)
    home = tmp_path / "home"
    home.mkdir()
    # Only these tools are reachable, so no real tmux, bwrap or engine runs.
    tools = tmp_path / "tools"
    tools.mkdir()
    for name in ("git", "true", "false"):
        found = shutil.which(name)
        assert found is not None
        (tools / name).symlink_to(found)
    gitconfig = tmp_path / "gitconfig"
    gitconfig.write_text("")
    env = {
        "HOME": str(home),
        "PATH": f"{bin_dir}{os.pathsep}{tools}",
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "XDG_RUNTIME_DIR": str(tmp_path / "run"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": str(gitconfig),
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
        "LANG": "C.UTF-8",
    }
    engine_cache(env, {})
    choices.set_settings(
        {
            "shortlist.claude": ["opus@high"],
            "shortlist.codex": ["gpt-5@high"],
            "single.writer": "claude:opus",
            "single.reviewer": "claude:opus",
            "sandbox.codex": "off",
            "sandbox.opencode": "off",
            "sandbox.claude": "off",
        },
        env=env,
    )
    return env


def script_path(env: Mapping[str, str]) -> Path:
    return Path(env["PATH"].split(os.pathsep)[0]) / "stub_engine_script.py"


def make_repo(tmp_path: Path, env: Mapping[str, str]) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main", env=env)
    (repo / "README.md").write_text("hello\n")
    git(repo, "add", "-A", env=env)
    git(repo, "commit", "-q", "-m", "first", env=env)
    return repo


def kill_sessions(tmp_path: Path) -> None:
    """End every stub session's process."""
    sessions = tmp_path / "tmux-state" / "sessions"
    if not sessions.is_dir():
        return
    for folder in sessions.iterdir():
        with contextlib.suppress(OSError, ValueError):
            os.killpg(int((folder / "pid").read_text()), signal.SIGKILL)


def session_folder(tmp_path: Path, run_id: str) -> Path:
    return tmp_path / "tmux-state" / "sessions" / run_id


def tmux_log(tmp_path: Path) -> list[list[str]]:
    path = tmp_path / "tmux-state" / "log.jsonl"
    if not path.exists():
        return []
    return [json.loads(line)["args"] for line in path.read_text().splitlines()]
