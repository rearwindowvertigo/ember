"""Generate canned replies with Ember's records and commands."""

import argparse
import json
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import asdict, dataclass, replace
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import Mock, patch

from ember import choices, engines, paths, sandbox, skill
from ember.cli import main as cli_main
from ember.engines import codex
from ember.flows import command as flows
from ember.flows import plan as flow_plan
from ember.gapcheck import command as gapcheck
from ember.gapcheck import plan as gap_plan
from ember.gapcheck import previous
from ember.panels import command as panels
from ember.panels import record as panel_records
from ember.panels.members import Member
from ember.panels.run import member_line
from ember.runs import record
from ember.runs.core import Core, RunError, StartOptions


def run_dry_run_replies(settings: dict[str, Any]) -> dict[str, str]:
    """Build the single-writer preview through the worker selector and formatter."""
    driver = Mock()
    driver.valid_effort.return_value = True
    core = Core({})
    with (
        patch.object(core, "_check_connected"),
        patch.object(core, "_driver", return_value=driver),
    ):
        _, worker = core._select(
            StartOptions("codex", ".", "brief.md", dry_run=True), settings
        )
    return {
        "_stubs/replies/run-dry-run.out": worker.line() + "\n",
        "_stubs/replies/run-dry-run.json": json.dumps(asdict(worker)) + "\n",
    }


def turn_results_state() -> dict[str, Any]:
    """Build the turn-results-counted state from isolated run records.

    Returns:
        The stub state containing the three status documents.

    Raises:
        RuntimeError: Status fails or reports a record error.
    """
    outcomes = (
        (
            {
                "exit": 73,
                "state": "failed",
                "reason": "Codex reported an error: You've hit your usage limit.",
                "provider_kind": "usage_limit",
                "attempts": [
                    {
                        "id": "turn-1-attempt-1",
                        "sent": True,
                        "sending": False,
                        "provider_kind": "usage_limit",
                        "result": "failed",
                        "seconds": 10.0,
                    }
                ],
                "provider_error": True,
                "signed_out": False,
                "incomplete": "provider error",
                "seconds": 10.0,
            },
            {
                "exit": 0,
                "state": "done",
                "reason": None,
                "provider_error": False,
                "signed_out": False,
                "incomplete": None,
                "provider_kind": None,
                "attempts": [
                    {
                        "id": "turn-2-attempt-1",
                        "sent": True,
                        "sending": False,
                        "provider_kind": "capacity",
                        "result": "failed",
                        "seconds": 2.0,
                    },
                    {
                        "id": "turn-2-attempt-2",
                        "sent": True,
                        "sending": False,
                        "provider_kind": None,
                        "result": "done",
                        "seconds": 13.0,
                    },
                ],
                "seconds": 45.0,
            },
        ),
        (
            {
                "state": "done",
                "reason": None,
                "provider_error": False,
                "signed_out": False,
                "incomplete": None,
            },
        ),
        (
            {
                "exit": None,
                "state": None,
                "reason": None,
                "provider_error": None,
                "signed_out": None,
                "incomplete": None,
                "seconds": 5.0,
            },
        ),
    )
    with TemporaryDirectory(dir=".") as temporary:
        root = Path(temporary).resolve()
        env = {
            "HOME": str(root / "home"),
            "XDG_STATE_HOME": str(root / "state"),
        }
        for index, turns in enumerate(outcomes, start=1):
            run_id = f"codex-20261103-12000{index}-00000{index}"
            unfinished = index == 3
            run = record.Run(
                id=run_id,
                engine="codex",
                model="gpt-6.1-sol",
                effort="high",
                read_only=True,
                repo="{{DIR}}",
                worktree=f"{{{{WORKTREES}}}}/{run_id}",
                branch=None,
                base="a" * 40,
                sandbox="codex",
                network=True,
                read=(),
                read_effective=(),
                trailers=(),
                claim="turn-count-01",
                status=record.RUNNING if unfinished else record.WAITING,
                turn=len(turns),
                turn_state=record.SENT if unfinished else record.COLLECTED,
                reply=None,
                expected_head=None,
                started_at=float(index),
                updated_at=float(index + 1),
                ended_at=None,
                last_exit=124 if unfinished else 0,
                last_reason="stalled" if unfinished else None,
                commits=(),
                subject="Inspect the report",
                main="{{DIR}}",
                common="{{DIR}}/.git",
                detached=True,
                folder=record.make_run_dir(run_id, env),
                env=env,
            )
            record.save(run)
            for turn, outcome in enumerate(turns, start=1):
                record.write_private(
                    record.outcome_path(run, turn), json.dumps(outcome) + "\n"
                )
        out, err = StringIO(), StringIO()
        core = Core(env, out=out, err=err)
        with patch.object(Core, "_session_state", return_value="live"):
            code = core.status(None, as_json=True)
        if code != 0 or err.getvalue():
            raise RuntimeError(f"status exited {code}: {err.getvalue()}")
        documents = json.loads(out.getvalue())
    return {"runs": {document["id"]: document for document in documents}}


@dataclass
class _ProviderClock:
    """Advance capacity waits without sleeping in the fixture generator."""

    ticks: float = 100.0

    def time(self) -> float:
        """Return the fixture's epoch seconds."""
        return self.ticks

    def monotonic(self) -> float:
        """Return the fixture's elapsed seconds."""
        return self.ticks

    def sleep(self, seconds: float) -> None:
        """Advance the fixture clock by the requested wait."""
        self.ticks += seconds


def provider_replies() -> dict[str, str]:
    """Build exit-73 streams and status replies with the run formatters."""
    replies: dict[str, str] = {}
    template = next(iter(turn_results_state()["runs"].values()))
    capacity = "Selected model is at capacity. Please try a different model."
    for variant, message in (
        ("capacity", capacity),
        ("usage_limit", "You've hit your usage limit. Try again later."),
        ("outage", "Internal server error"),
        ("uncertain", capacity),
    ):
        with TemporaryDirectory() as temporary:
            env = {"XDG_STATE_HOME": temporary}
            run_id = template["id"]
            run = record.from_json(
                {
                    **template,
                    "read_only": False,
                    "detached": False,
                    "branch": f"ember/codex/{run_id}",
                    "turn": 1,
                    "turn_state": record.SENT,
                    "status": record.RUNNING,
                    "last_exit": None,
                    "last_reason": None,
                    "expected_head": template["base"],
                    "capacity_retries": 0 if variant == "capacity" else 2,
                },
                record.make_run_dir(run_id, env),
                env,
            )
            record.save(run)
            record.write_private(run.folder / "turn-1.prompt.md", "Inspect the report")
            out, err = StringIO(), StringIO()
            core = Core(env, out=out, err=err)
            driver = Mock(spec=engines.Driver)
            driver.send.side_effect = engines.DriverError("transport disconnected")
            clock = _ProviderClock()
            started = clock.monotonic() - 10.0
            with (
                patch("ember.runs.core.time", clock),
                patch.object(
                    Core, "_wait_turn", return_value=codex.error_outcome(message)
                ),
                core._collection(run, started) as started,
            ):
                outcome = core._capacity_turn(run, driver, started)
            seconds = clock.monotonic() - started
            core._save_outcome(run, outcome, seconds)
            core._write_outcome(run, {**core._saved_outcome(run), "exit": 73})
            run = replace(
                run,
                status=record.FAILED,
                last_exit=73,
                last_reason=outcome.reason,
                turn_state=record.SENT if variant == "uncertain" else record.COMMITTED,
            )
            record.save(run)
            core._say(f"run {run_id}")
            if variant != "uncertain":
                core._print_turn(run, 73, 0, None, None, seconds, None)

            def fail(
                reason: str = outcome.reason or "",
                variant: str = variant,
                run_id: str = run_id,
            ) -> int:
                """Return the provider failure through the command handler."""
                raise RunError(73, reason, variant, run=run_id)

            code = core.execute(fail)
            prefix = f"provider-{variant.replace('_', '-')}/files/.ember-eval/replies/"
            replies[prefix + "run-start.out"] = out.getvalue()
            replies[prefix + "run-start.err"] = err.getvalue()
            replies[prefix + "run-start.code"] = f"{code}\n"
            for as_json in (False, True):
                out.seek(0)
                out.truncate()
                err.seek(0)
                err.truncate()
                with patch.object(Core, "_session_state", return_value="live"):
                    core.status(run_id, as_json=as_json)
                replies[prefix + "run-status" + (".json" if as_json else ".out")] = (
                    out.getvalue()
                )
    return replies


def denied_path_replies() -> dict[str, str]:
    """Build denial warnings and status using an isolated sandbox inventory."""
    replies: dict[str, str] = {}
    template = next(iter(turn_results_state()["runs"].values()))
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        home = root / "home"
        env = {
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "XDG_STATE_HOME": str(root / "state"),
            "XDG_DATA_HOME": str(home / ".local/share"),
            "XDG_RUNTIME_DIR": str(root / "runtime"),
        }
        worktree = paths.ensure_worktrees_dir(env) / template["id"]
        folders = [
            home / ".toolchain/versions/3.14",
            home / ".ssh",
            home / ".config/tool",
            paths.config_dir(env),
            paths.state_dir(env),
            paths.worktrees_dir(env) / "other-worker",
            root / "runtime",
            worktree,
        ]
        for folder in folders:
            folder.mkdir(parents=True, exist_ok=True)
        readable = home / "shared"
        readable.mkdir()
        choices.set_settings({"sandbox.read": [str(readable)]}, env=env)
        run = record.from_json(
            {
                **template,
                "model": "gpt-6.1-sol-mini",
                "effort": "medium",
                "turn": 1,
                "read_effective": [str(readable)],
                "last_exit": 0,
                "last_reason": None,
                "sandbox_paths": sandbox.hidden_snapshot(
                    worktree, worktree / ".git", [readable], env=env
                ),
            },
            record.make_run_dir(template["id"], env),
            env,
        )
        record.save(run)
        out, err = StringIO(), StringIO()
        core = Core(env, out=out, err=err)
        driver = Mock(spec=engines.Driver)
        driver.turn_output.return_value = ""
        driver.screen.return_value = ""
        with (
            patch.object(core, "_optional_driver", return_value=driver),
            patch.object(core, "_attempt_screen", return_value=""),
        ):
            # Earlier attempt's denial stays after the final attempt succeeds.
            core._save_outcome(
                run,
                engines.TurnOutcome(
                    engines.FAILED,
                    reason=f"{folders[0]}/python: Permission denied",
                    provider_error=True,
                    provider_kind="capacity",
                ),
                2.0,
            )
            reply = "Checks could not read:\n" + "\n".join(
                f"'{folder}/credentials': EACCES" for folder in folders[1:-1]
            )
            core._save_outcome(
                run, engines.TurnOutcome(engines.DONE, reply=reply), 45.0
            )
        core._write_outcome(run, {**core._saved_outcome(run), "exit": 0})
        core._say(f"run {run.id}")
        core._print_turn(run, 0, 0, None, reply, 45.0, None)
        prefix = "sandbox-denied-paths/files/.ember-eval/replies/"
        replies[prefix + "run-start.out"] = out.getvalue()
        replies[prefix + "run-start.err"] = err.getvalue()
        replies[prefix + "run-start.code"] = "0\n"
        for as_json in (False, True):
            out.seek(0)
            out.truncate()
            err.seek(0)
            err.truncate()
            with patch.object(Core, "_session_state", return_value="live"):
                code = core.status(run.id, as_json=as_json)
            if code != 0 or err.getvalue():
                raise RuntimeError(f"status exited {code}: {err.getvalue()}")
            replies[prefix + "run-status" + (".json" if as_json else ".out")] = (
                out.getvalue()
            )
        # All paths describe the fixture home, never the generator's home.
        return {
            name: text.replace(str(root), "/eval") for name, text in replies.items()
        }


def panel_replies() -> dict[str, str]:
    """Build completion streams and three running-panel status snapshots."""
    replies: dict[str, str] = {}
    panel_id = "panel-20261102-120000-aaaaa0"
    entries = [
        Member(engine, model, None, "option").entry(n)
        for n, (engine, model) in enumerate(
            (
                ("codex", "gpt-6-astra"),
                ("claude", "opus"),
                ("opencode", "deepseek/deepseek-v4.1-flash-1017"),
            ),
            1,
        )
    ]
    began = 1793620800.0
    for n, member in enumerate(entries, 1):
        member.update(
            run=f"{member['engine']}-20261102-120000-aaaaa{n}",
            status="answered" if n < 3 else "failed",
            seconds=30.0 + n,
            exit=0 if n < 3 else 80,
            bytes=(246, 282, 0)[n - 1],
            closed=True,
            reason=None if n < 3 else "the turn ended with no reply",
            attempts=[{"kind": "start", "at": began, "exit": 0 if n < 3 else 80}],
        )
    panel = panel_records.new_panel(
        panel_id,
        dir="{{DIR}}",
        question_file="{{DIR}}/question.md",
        read=(),
        claim="panel-progress-01",
        stall_minutes=10,
        timeout_minutes=30,
        members=entries,
        left_out=(),
        base="a" * 40,
        now=began,
    )
    with TemporaryDirectory(dir=".") as temporary:
        root = Path(temporary).resolve()
        env = {
            "HOME": str(root / "home"),
            "XDG_STATE_HOME": str(root / "state"),
            "XDG_RUNTIME_DIR": str(root / "runtime"),
        }
        folder = panel_records.folder(panel_id, env)

        def render(text: str, *, completion: bool = False) -> str:
            text = text.replace(str(folder / "summary.md"), "{{SUMMARY}}")
            text = text.replace(str(folder), "{{EVAL}}/runs/{{PANEL}}")
            return text.replace(panel_id, "{{PANEL}}") if completion else text

        panel.update(status="incomplete", exit=83, ended_at=began + 33)
        prefix = "panel-named-members-compared/files/.ember-eval/replies/panel-start"
        for as_json in (False, True):
            out, err = StringIO(), StringIO()
            if not as_json:
                print(f"panel {panel_id}", file=out)
            for member in panel["members"]:
                print(
                    f"WARNING: member {member['n']} "
                    f"({member['engine']}:{member['model']}): read-only run "
                    f"{member['run']} does not see uncommitted review-notes.md",
                    file=err,
                )
            for member in panel["members"]:
                print(member_line(member), file=err)
            panels._result(panel, env, as_json, out, err)
            replies[prefix + (".json" if as_json else ".out")] = render(
                out.getvalue(), completion=True
            )
            replies[prefix + ".err"] = render(err.getvalue(), completion=True)

        panel.update(status="running", exit=None, ended_at=None)
        for member in panel["members"][1:]:
            member.update(
                status="running",
                seconds=None,
                exit=None,
                closed=False,
                reason=None,
                bytes=0,
            )
            member["attempts"][0]["exit"] = None
        panel["members"][2]["attempts"][0]["at"] = began + 60
        active = panel["members"][1]
        run = record.Run(
            id=active["run"],
            engine=active["engine"],
            model=active["model"],
            effort=None,
            read_only=True,
            repo="{{DIR}}",
            worktree="{{WORKTREES}}/" + active["run"],
            branch=None,
            base="a" * 40,
            sandbox="bubblewrap",
            network=True,
            read=(),
            read_effective=(),
            trailers=(),
            claim=None,
            status=record.RUNNING,
            turn=1,
            turn_state=record.SENT,
            reply=None,
            expected_head=None,
            started_at=began,
            updated_at=began,
            ended_at=None,
            last_exit=None,
            last_reason=None,
            commits=(),
            subject="Inspect rounding",
            last_screen_change_at=began + 40,
            folder=record.make_run_dir(active["run"], env),
            env=env,
        )
        record.save(run)
        older = replace(
            run,
            id=panel["members"][2]["run"],
            engine="opencode",
            model=panel["members"][2]["model"],
            folder=record.make_run_dir(panel["members"][2]["run"], env),
        )
        document = older.to_json()
        del document["last_screen_change_at"]
        record.write_private(older.folder / "run.json", json.dumps(document))
        panel_records.create(panel, "Inspect rounding", "Inspect rounding", env)
        for call, seconds in enumerate((60, 120, 180), 1):
            if call == 3:
                record.update(run, {"last_screen_change_at": began + 170})
            for as_json in (False, True):
                out, err = StringIO(), StringIO()
                with (
                    patch.object(panels.time, "time", return_value=began + seconds),
                    patch.object(panel_records, "live", return_value=True),
                ):
                    panels._status(
                        "" if call == 1 else panel_id, as_json, env, out, err
                    )
                if err.getvalue():
                    raise RuntimeError(err.getvalue())
                suffix = ".json" if as_json else ".out"
                replies[
                    "panel-progress-reported/files/.ember-eval/replies/"
                    f"panel-status.{call}{suffix}"
                ] = render(out.getvalue())
    return replies


def choices_replies() -> dict[str, str]:
    """Build choices replies and skill warnings through the claim command."""
    replies: dict[str, str] = {}
    cases = {
        "_stubs": "current",
        "refuses-owner-setting": "current",
        "provider-capacity": "current",
        "claim-lost-then-gone": "current",
        "flow-gap-check-first": "current",
        "owner-away-named-workers": "current",
        "claim-stale-skill": "stale",
        "claim-unknown-skill": "unknown",
    }
    loaded = "1.2.3"
    for name, outcome in cases.items():
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            env = {
                "HOME": str(root / "home"),
                "XDG_CONFIG_HOME": str(root / "config"),
                "XDG_STATE_HOME": str(root / "state"),
                "XDG_CACHE_HOME": str(root / "cache"),
            }
            if outcome != "unknown":
                installed = root / "installed-skill"
                installed.mkdir()
                version = loaded if outcome == "current" else "999.0.0"
                (installed / "SKILL.md").write_text(
                    f"---\nname: ember\n---\nSkill version: {version}\n"
                )
                link = skill.link_path(env)
                link.parent.mkdir(parents=True)
                link.symlink_to(installed)
            if name != "owner-away-named-workers":
                choices.set_settings(
                    {
                        "shortlist.codex": [
                            "gpt-6.1-sol@high",
                            "gpt-6.1-sol-mini@medium",
                        ]
                    },
                    env=env,
                )
            if name not in ("owner-away-named-workers", "refuses-owner-setting"):
                choices.set_settings(
                    {
                        "single.writer": "codex:gpt-6.1-sol",
                        "single.reviewer": "codex:gpt-6.1-sol-mini",
                    },
                    env=env,
                )
            if name == "provider-capacity":
                choices.set_settings({"provider.capacity_retries": "0"}, env=env)
            if name == "flow-gap-check-first":
                choices.set_settings(
                    {"multi.runs_as": "flow", "gapcheck.ask": "all"}, env=env
                )
            if name == "claim-lost-then-gone":
                choices.finish("eval-claim", env=env)
            if outcome in ("stale", "unknown"):
                choices.set_once({"single.writer": "codex:gpt-6.1-sol-mini"}, env=env)
            out, err = StringIO(), StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = cli_main(
                    ["choices", "--claim", "--id", "eval-claim", "--skill", loaded],
                    env=env,
                )
            if code != 0:
                raise RuntimeError(f"claim exited {code}: {err.getvalue()}")
            claimed = json.loads(out.getvalue())
            documents = {
                "choices-claim.out": claimed,
                "choices-read-id.out": choices.read("eval-claim", env=env),
            }
            if name in ("_stubs", "refuses-owner-setting", "provider-capacity"):
                documents["choices-read.out"] = choices.read(env=env)
            if name in ("_stubs", "flow-gap-check-first", "owner-away-named-workers"):
                documents["menu.json"] = claimed["menu"]
            for filename, document in documents.items():
                if filename != "menu.json" and document["claim"] is not None:
                    document["claim"]["id"] = "{{ID}}"
                replies[
                    f"{name}/files/.ember-eval/replies/{filename}"
                    if name != "_stubs"
                    else f"_stubs/replies/{filename}"
                ] = (
                    json.dumps(document, indent=2).replace(loaded, "{{SKILL_VERSION}}")
                    + "\n"
                )
            if err.getvalue():
                prefix = f"{name}/files/.ember-eval/replies/choices-claim"
                replies[prefix + ".err"] = err.getvalue().replace(
                    loaded, "{{SKILL_VERSION}}"
                )
                replies[prefix + ".code"] = f"{code}\n"
    return replies


def gapcheck_replies() -> dict[str, str]:
    """Build flow dry runs and gap results from the cases' reviewer replies."""
    replies: dict[str, str] = {}
    root = Path(__file__).resolve().parent.parent
    for name, ids in (
        ("flow-gap-check-first", ("item", "json")),
        ("owner-away-named-workers", ("before", "decimals")),
        ("flow-lead-stop-waits", ("parse", "strict")),
    ):
        prefix = f"{name}/files/.ember-eval/replies/"
        steps = tuple(
            gap_plan.Step(n, f"task {identity}", None, None)
            for n, identity in enumerate(ids, 1)
        )
        document = gap_plan.Plan(Path("{{PLAN}}"), "", "tasks", steps, True)
        made = gapcheck._result(document, None)
        made.update(
            gapcheck._read_reply(
                (root / (prefix + "gapcheck.reply")).read_text(), document, None
            )
        )
        named = name == "owner-away-named-workers"
        model, effort = (
            ("gpt-6-astra", "high") if named else ("gpt-6.1-sol-mini", "medium")
        )
        made["reviewer"] = dict(
            engine="codex",
            model=model,
            effort=effort,
            source="option" if named else "menu",
        )
        made["run"] = dict(
            id="codex-20261102-120000-aaaaaa",
            reply="{{EVAL}}/runs/codex-20261102-120000-aaaaaa/turn-1.md",
            exit=0,
            closed=True,
            close_error=None,
        )
        settings = {
            "answers": {
                "gapcheck.ask": {"value": "high" if named else "all"},
                "gapcheck.when": {"value": "always"},
                "flow.whole_review": {"value": False},
                "flow.stall": {"value": 10},
            }
        }
        gapcheck._mark_asks(made, settings)
        previous.validate(made)
        for as_json in (False, True):
            out = StringIO()
            gapcheck._print_result(made, as_json, out)
            replies[prefix + "gapcheck" + (".json" if as_json else ".out")] = (
                out.getvalue()
            )
        # Build the task plan through the same loader used by the CLI.
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "plan.json"
            path.write_text(
                json.dumps(
                    {
                        "dir": "/eval/repo",
                        "failure_model": "Empty selections are handled.",
                        "check": [
                            "python3",
                            "-m",
                            "unittest",
                            "discover",
                            "-s",
                            "tests",
                        ],
                        "tasks": [
                            dict(
                                id=identity,
                                subject="feat: update report",
                                task="Update report",
                                files=["tools/report.py", "tests/test_report.py"],
                                after=[] if n == 0 else [ids[n - 1]],
                                review="Review report",
                            )
                            for n, identity in enumerate(ids)
                        ],
                    }
                )
            )
            flow = flow_plan.load(str(path))
        flow = replace(
            flow,
            path=Path("{{PLAN}}"),
            rounds_source="setting",
            parallel_source="setting",
        )
        writer = flow_plan.Worker(
            "codex",
            model if named else "gpt-6.1-sol",
            effort if named else "high",
            "option" if named else "menu",
        )
        reviewer = flow_plan.Worker(
            "codex", model, effort, "option" if named else "menu"
        )
        for as_json in (False, True):
            out, err = StringIO(), StringIO()
            args = argparse.Namespace(
                previous=None, claim=None, json=as_json, stall=None
            )
            with patch.object(flow_plan, "warnings", return_value=[]):
                flows._dry_run(
                    args,
                    flow,
                    Path("{{DIR}}"),
                    "{{HEAD}}",
                    writer,
                    reviewer,
                    settings,
                    {},
                    out,
                    err,
                )
            replies[prefix + "flow-dry-run" + (".json" if as_json else ".out")] = (
                out.getvalue()
            )
    return replies


def generated_replies() -> dict[str, str]:
    """Return every generated reply and state file by its eval-relative path."""
    choices = choices_replies()
    return {
        "turn-results-counted/files/.ember-eval/state.json": (
            json.dumps(turn_results_state(), indent=2) + "\n"
        ),
        **provider_replies(),
        **denied_path_replies(),
        **panel_replies(),
        **gapcheck_replies(),
        **choices,
        **run_dry_run_replies(json.loads(choices["_stubs/replies/choices-read.out"])),
    }


def main() -> None:
    """Write generated fixtures or list their eval-relative paths."""
    parser = argparse.ArgumentParser(description=__doc__)
    destination = parser.add_mutually_exclusive_group(required=True)
    destination.add_argument("--output-dir", type=Path)
    destination.add_argument("--list-files", action="store_true")
    args = parser.parse_args()
    replies = generated_replies()
    if args.list_files:
        print("\n".join(replies))
        return
    root = args.output_dir.resolve()
    evals = Path(__file__).resolve().parent.parent
    if root.is_relative_to(evals):
        parser.error("--output-dir must be outside skill/evals")
    for name, text in replies.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


if __name__ == "__main__":
    main()
