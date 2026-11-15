"""Grade repeated runs of one eval case by expectation, blind to side.

Usage:
    grade.py batch <case dir> <out dir> [--seed <n>]
    grade.py apply <case dir> <out dir> [--graders <n>]

The out dir is one that run_repeat.sh filled. `batch` removes each run's
earlier `grading.json` and writes `<out dir>/grading/<grader>/` for every
file in the case's `graders/`:

- A `type: script` grader is decided at once from each run's `trace.jsonl`
  and saved as `script.json`. Its `check: order` passes when a Bash call
  matching the `first` pattern runs before the first Bash call matching the
  `then` pattern. A backslash at a line's end joins the next line first.
- A `type: llm` grader gets `task.md`, `expectation.md` and one folder per
  run under `runs/`, named `r01`, `r02` and so on in a shuffled order, with
  the run's `calls.log`, `owner.log` and a compact `trace.txt`. A grader
  file may name `files`, paths under the run's work folder that are copied
  to the run's `files/`, and a `command`, which is run in the work folder
  with its output and exit code saved as `command.txt`. The run's path is
  replaced in every copied text, so the folder names and paths do not say
  which side a run is on. Clock times, run ids and any skill text a session
  printed are kept. The key is in `<out dir>/grading/keys/<grader>.json`,
  which no grader is given.

Each grader of a `type: llm` file reads one `<grader>` folder and writes
`verdict-<name>.json` there. `apply` needs `--graders` such files (default
2) for every `type: llm` grader, each with a written reading, a list of
ambiguities and a true or false `passed` for every run. When they agree on
every run, it writes each run's `grading.json` in grader file order, as
tally.py reads it, and prints each reading. When they disagree on any run,
or a grader file changed after `batch`, it says so and writes nothing: the
expectation's text is corrected and the runs are batched and graded again.

Exit 0 on success, 64 on a usage error, 65 when verdicts are missing,
unreadable, out of date or in disagreement, 66 when the case has no
graders or the out dir has no runs.
"""

import argparse
import json
import random
import re
import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tally import runs, sides  # noqa: E402

RESULT_LIMIT = 1500
COMMAND_TIMEOUT = 120

TASK = """# Grade one expectation across every run

Read `expectation.md` in this folder. It holds one expectation.

Each folder under `runs/` is one run of the same eval case. The names are
shuffled; no folder name or path says which skill text a run used. Open
nothing outside this folder, and no other `verdict-*.json` file. For each
run read:

- `calls.log`: the exact argv, order and exit code of every stubbed `emb`
  call.
- `owner.log`: the owner's replies that were sent.
- `trace.txt`: the session in order: every assistant text, every tool call
  with its full input (including `run_in_background`), each tool result cut
  to {limit} characters and each turn's final text. `=== OWNER REPLY n`
  marks where the owner's reply n was sent.
- `files/` and `command.txt`, where the expectation names them: files
  copied from the run's work folder, and the output and exit code of a
  command run there.

Method:

1. Before opening any run, write the one reading of the expectation that
   you will apply to every run: what must be present for a pass, and what
   does not count. Where the expectation's words can be read two ways,
   list each such place under `ambiguities`.
2. Apply that reading to every run. Judge only what the run did and said:
   which commands ran, their order, flags and exit codes, whether a call
   ran in the background, and what was said to or asked of the owner. Give
   no credit for intent. When the evidence for a condition is missing, the
   run fails.
3. Change no file except your verdict file.

Write `verdict-<your name>.json` in this folder:

    {{"reading": "<your reading>", "ambiguities": ["<place>", ...],
     "runs": {{"r01": {{"passed": true, "evidence": "<calls.log seq numbers
     or short quoted lines>"}}, ...}}}}

with one entry for every folder under `runs/`. Check that it loads as JSON.
"""


def front(path: Path) -> tuple[dict[str, str], str]:
    """Return a grader file's front matter and its body."""
    text = path.read_text(encoding="utf-8")
    meta: dict[str, str] = {}
    pieces = text.split("---\n", 2)
    if text.startswith("---\n") and len(pieces) == 3:
        for line in pieces[1].splitlines():
            key, found, value = line.partition(":")
            if found:
                meta[key.strip()] = value.strip().strip("'\"")
        text = pieces[2]
    return meta, text.strip()


def events(trace: Path) -> list[dict[str, Any]]:
    """Return the JSON objects of a stream-json trace; a missing trace has none."""
    if not trace.is_file():
        return []
    found = []
    for line in trace.read_text(encoding="utf-8").splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            found.append(item)
    return found


def parts(item: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the content parts of an assistant or user event."""
    message = item.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if not isinstance(content, list):
        return []
    return [part for part in content if isinstance(part, dict)]


def commands(trace: Path) -> list[str]:
    """Return the session's Bash commands in the order they were called."""
    found = []
    for item in events(trace):
        if item.get("type") != "assistant":
            continue
        for part in parts(item):
            if part.get("type") == "tool_use" and part.get("name") == "Bash":
                tool_input = part.get("input")
                command = (
                    tool_input.get("command") if isinstance(tool_input, dict) else None
                )
                if isinstance(command, str):
                    found.append(command)
    return found


def replies(owner_log: Path) -> list[str]:
    """Return the owner's replies of a run, in the order they were sent."""
    if not owner_log.is_file():
        return []
    return [line for line in owner_log.read_text(encoding="utf-8").splitlines() if line]


def compact(
    run: Path, clean: Callable[[str], str] = str, limit: int = RESULT_LIMIT
) -> str:
    """Return an ordered text view of a run's trace.

    Every text is passed through `clean` before a tool result is cut to the
    limit, so a path that names the side cannot survive as a cut fragment.
    Each owner reply is shown where run_case.sh marked it in the trace. In
    a trace without marks it follows the next result with a question mark.
    """
    lines = []
    number = 0
    found = events(run / "trace.jsonl")
    marked = any(item.get("type") == "owner_reply" for item in found)
    sent = replies(run / "owner.log")
    turn = 0
    for item in found:
        kind = item.get("type")
        if kind == "owner_reply":
            text = clean(str(item.get("text")))
            lines.append(f"=== OWNER REPLY {item.get('n')}:\n{text}\n")
        if kind == "result":
            final = clean(str(item.get("result")))
            lines.append(f"=== TURN RESULT (final text of this turn):\n{final}\n")
            # A trace without markers: the runner replies after a question mark.
            if not marked and "?" in final and turn < len(sent):
                lines.append(f"=== OWNER REPLY {turn + 1}:\n{clean(sent[turn])}\n")
                turn += 1
        if kind not in ("assistant", "user"):
            continue
        for part in parts(item):
            number += 1
            what = part.get("type")
            if what == "text":
                text = clean(str(part.get("text")))
                lines.append(f"[{number}] {kind.upper()} TEXT:\n{text}\n")
            elif what == "tool_use":
                shown = clean(json.dumps(part.get("input"), ensure_ascii=False))
                lines.append(f"[{number}] TOOL CALL {part.get('name')}: {shown}\n")
            elif what == "tool_result":
                body = part.get("content")
                if isinstance(body, list):
                    body = "\n".join(
                        str(piece.get("text", piece))
                        if isinstance(piece, dict)
                        else str(piece)
                        for piece in body
                    )
                body = clean(str(body))
                if len(body) > limit:
                    body = body[:limit] + f"\n...[cut, {len(body)} chars]"
                lines.append(f"[{number}] TOOL RESULT:\n{body}\n")
    return "\n".join(lines)


HEREDOC = re.compile(r"<<-?\s*(['\"]?)(\w+)\1[^\n]*\n(?:.*\n)*?[ \t]*\2[ \t]*(?:\n|$)")


def shell_lines(command: str) -> str:
    """Return a Bash command without heredoc bodies and with continued lines joined."""
    command = HEREDOC.sub(
        lambda found: found.group(0).split("\n", 1)[0] + "\n", command
    )
    return re.sub(r"\\\n\s*", " ", command)


def order(first: str, then: str, called: list[str]) -> tuple[bool, str]:
    """Return whether a `first` call precedes the first `then` call, with evidence."""
    joined = [shell_lines(text) for text in called]
    before = next((i for i, text in enumerate(joined, 1) if re.search(first, text)), 0)
    after = next((i for i, text in enumerate(joined, 1) if re.search(then, text)), 0)
    if not after:
        return False, f"no Bash call matches {then!r}"
    if not before:
        return False, f"no Bash call matches {first!r}; {then!r} is Bash call {after}"
    evidence = f"{first!r} is Bash call {before}; {then!r} is Bash call {after}"
    return before < after, evidence


def graders(case: Path) -> list[Path]:
    """Return the case's grader files in file-name order."""
    return sorted((case / "graders").glob("*.md"), key=lambda path: path.stem)


def all_runs(out: Path) -> list[tuple[str, Path]]:
    """Return every run of every side as its `<side>/<run>` name and folder."""
    return [
        (f"{side.name}/{run.name}", run) for side in sides(out) for run in runs(side)
    ]


def scrubber(run: Path, out: Path) -> Callable[[str], str]:
    """Return a function that replaces the run's and the out dir's paths.

    Only absolute spellings are replaced, longest first, so a relative out
    dir such as `out` never replaces part of a word. Each spelling is also
    replaced in its dashed form.
    """
    marks = {}
    for path, mark in ((run, "<RUN>"), (out, "<OUT>")):
        for spelling in (str(path.absolute()), str(path.resolve())):
            marks.setdefault(spelling, mark)
            # Claude Code names a session's scratch folder after its working
            # folder, with every other character turned into a dash.
            marks.setdefault(re.sub(r"[^A-Za-z0-9]", "-", spelling), mark)
    ordered = sorted(marks.items(), key=lambda item: len(item[0]), reverse=True)

    def clean(text: str) -> str:
        for spelling, mark in ordered:
            text = text.replace(spelling, mark)
        return text

    return clean


def lay_out(target: Path, run: Path, clean: Callable[[str], str], meta: dict) -> None:
    """Write one run's blind copy for an llm grader."""
    target.mkdir(parents=True)
    for log in ("calls.log", "owner.log"):
        source = run / log
        text = source.read_text(encoding="utf-8") if source.is_file() else ""
        (target / log).write_text(clean(text), encoding="utf-8")
    (target / "trace.txt").write_text(compact(run, clean), encoding="utf-8")
    work = run / "work"
    for name in filter(
        None, (item.strip() for item in meta.get("files", "").split(","))
    ):
        (target / "files").mkdir(exist_ok=True)
        source = work / name
        text = (
            source.read_text(encoding="utf-8")
            if source.is_file()
            else f"(the run has no {name})\n"
        )
        (target / "files" / Path(name).name).write_text(clean(text), encoding="utf-8")
    if meta.get("command"):
        try:
            done = subprocess.run(
                shlex.split(meta["command"]),
                cwd=work,
                capture_output=True,
                text=True,
                check=False,
                timeout=COMMAND_TIMEOUT,
            )
            result = f"exit {done.returncode}\n{done.stdout}{done.stderr}"
        except (OSError, subprocess.TimeoutExpired) as error:
            result = f"not run: {error}\n"
        (target / "command.txt").write_text(
            clean(f"$ {meta['command']}\n{result}"), encoding="utf-8"
        )


def batch(case: Path, out: Path, seed: int) -> int:
    """Decide script graders and lay out blind run folders for the others."""
    root = out / "grading"
    found = all_runs(out)
    for _, run in found:
        (run / "grading.json").unlink(missing_ok=True)
    for grader in graders(case):
        meta, body = front(grader)
        folder = root / grader.stem
        if folder.exists():
            shutil.rmtree(folder)
        folder.mkdir(parents=True)
        (folder / "expectation.md").write_text(body + "\n", encoding="utf-8")
        shutil.copyfile(grader, folder / "grader.md")
        if meta.get("type") == "script":
            if meta.get("check") != "order" or not (
                meta.get("first") and meta.get("then")
            ):
                print(
                    f"grade.py: {grader} needs check: order with first and then",
                    file=sys.stderr,
                )
                return 64
            verdicts = {}
            for name, run in found:
                passed, evidence = order(
                    meta["first"], meta["then"], commands(run / "trace.jsonl")
                )
                verdicts[name] = {"passed": passed, "evidence": evidence}
            decided = {"first": meta["first"], "then": meta["then"], "runs": verdicts}
            (folder / "script.json").write_text(
                json.dumps(decided, indent=2) + "\n", encoding="utf-8"
            )
            print(f"{grader.stem}: script, {len(verdicts)} runs decided")
            continue
        shuffled = list(found)
        random.Random(f"{seed}:{grader.stem}").shuffle(shuffled)
        key = {}
        for number, (name, run) in enumerate(shuffled, 1):
            ident = f"r{number:02d}"
            key[ident] = name
            lay_out(folder / "runs" / ident, run, scrubber(run, out), meta)
        (folder / "task.md").write_text(
            TASK.format(limit=RESULT_LIMIT), encoding="utf-8"
        )
        keys = root / "keys"
        keys.mkdir(exist_ok=True)
        (keys / f"{grader.stem}.json").write_text(
            json.dumps(key, indent=2) + "\n", encoding="utf-8"
        )
        print(f"{grader.stem}: llm, {len(key)} runs in {folder}")
    return 0


def verdicts_of(
    folder: Path, key: dict[str, str], need: int
) -> tuple[dict[str, dict[str, Any]], list[str], list[str]]:
    """Return one llm grader's agreed verdicts, its problems and its readings."""
    files = sorted(folder.glob("verdict-*.json"))
    problems = []
    if len(files) < need:
        problems.append(f"has {len(files)} of {need} verdict files")
    loaded = []
    readings = []
    for path in files:
        who = path.stem.removeprefix("verdict-")
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
            reading = document["reading"]
            if not isinstance(reading, str) or not reading.strip():
                raise TypeError("no written reading")
            if not isinstance(document["ambiguities"], list):
                raise TypeError("ambiguities is not a list")
            graded = document["runs"]
            for ident in key:
                if not isinstance(graded[ident]["passed"], bool):
                    raise TypeError(f"{ident} has no true or false passed")
        except (ValueError, KeyError, TypeError) as error:
            problems.append(f"{path.name} unreadable or incomplete: {error!r}")
            continue
        loaded.append((who, graded))
        readings.append(f"{who}: {' '.join(reading.split())}")
    agreed: dict[str, dict[str, Any]] = {}
    for ident, name in key.items():
        given = {who: graded[ident]["passed"] for who, graded in loaded}
        if len(set(given.values())) > 1:
            split = ", ".join(
                f"{who} {'pass' if value else 'fail'}" for who, value in given.items()
            )
            problems.append(f"graders disagree on {name} ({ident}): {split}")
            continue
        if loaded:
            agreed[name] = {
                "passed": loaded[0][1][ident]["passed"],
                "evidence": " | ".join(
                    f"{who}: {graded[ident].get('evidence', '')}"
                    for who, graded in loaded
                ),
            }
    return agreed, problems, readings


def apply(case: Path, out: Path, need: int) -> int:
    """Write each run's grading.json when every grader file has agreed verdicts."""
    root = out / "grading"
    found = all_runs(out)
    table: list[tuple[str, dict[str, dict[str, Any]]]] = []
    notes: list[str] = []
    failed = False
    for grader in graders(case):
        meta, body = front(grader)
        folder = root / grader.stem
        problems: list[str] = []
        agreed: dict[str, dict[str, Any]] = {}
        try:
            batched = (folder / "grader.md").read_text("utf-8")
            if batched != grader.read_text(encoding="utf-8"):
                problems.append("the grader file changed after batch")
            elif meta.get("type") == "script":
                decided = json.loads((folder / "script.json").read_text("utf-8"))
                agreed = decided["runs"]
            else:
                key = json.loads(
                    (root / "keys" / f"{grader.stem}.json").read_text("utf-8")
                )
                agreed, problems, readings = verdicts_of(folder, key, need)
                notes += [f"{grader.stem} reading, {line}" for line in readings]
        except (OSError, ValueError, KeyError) as error:
            problems = [f"not batched: {error!r}"]
        if not problems:
            problems = [
                f"no verdict for {name}" for name, _ in found if name not in agreed
            ]
        for problem in problems:
            print(f"{grader.stem}: {problem}", file=sys.stderr)
        failed = failed or bool(problems)
        table.append((body, agreed))
    if failed:
        print("grade.py: no grading.json written", file=sys.stderr)
        return 65
    for name, run in found:
        expectations = [{"text": body, **agreed[name]} for body, agreed in table]
        (run / "grading.json").write_text(
            json.dumps({"expectations": expectations}, indent=2) + "\n",
            encoding="utf-8",
        )
    for note in notes:
        print(note)
    print(f"wrote grading.json for {len(found)} runs")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Run `batch` or `apply` for one case's out dir."""
    parser = argparse.ArgumentParser(
        prog="grade.py", description=__doc__.splitlines()[0]
    )
    parser.add_argument("mode", choices=("batch", "apply"))
    parser.add_argument("case", type=Path, help="the case folder")
    parser.add_argument("out", type=Path, help="the out dir run_repeat.sh filled")
    parser.add_argument("--seed", type=int, default=0, help="shuffle seed for batch")
    parser.add_argument(
        "--graders", type=int, default=2, help="verdict files apply needs per grader"
    )
    try:
        args = parser.parse_args(argv)
    except SystemExit as stop:
        return 64 if stop.code else 0
    if args.graders < 1:
        print("grade.py: --graders is at least 1", file=sys.stderr)
        return 64
    case, out = args.case.resolve(), args.out.resolve()
    if not graders(case):
        print(f"grade.py: {args.case} has no graders/*.md", file=sys.stderr)
        return 66
    if not out.is_dir() or not sides(out):
        print(f"grade.py: {args.out} has no run-<i> folders", file=sys.stderr)
        return 66
    if args.mode == "batch":
        return batch(case, out, args.seed)
    return apply(case, out, args.graders)


if __name__ == "__main__":
    sys.exit(main())
