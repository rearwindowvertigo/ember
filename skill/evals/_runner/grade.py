"""Grade repeated runs of one eval case by expectation, blind to side.

Usage:
    grade.py batch <case dir> <out dir> [--seed <n>]
    grade.py apply <case dir> <out dir> [--graders <n>]

The out dir is one that run_repeat.sh filled. `batch` writes
`<out dir>/grading/<grader>/` for every file in the case's `graders/`:

- A `type: script` grader is decided at once from each run's `trace.jsonl`
  and saved as `script.json`. Its `check: order` passes when a Bash call
  matching the `first` pattern runs before the first Bash call matching the
  `then` pattern.
- A `type: llm` grader gets `task.md`, `expectation.md` and one folder per
  run under `runs/`, named `r01`, `r02` and so on in a shuffled order, with
  the run's `calls.log`, `owner.log` and a compact `trace.txt`. The run's
  path is replaced in those files, so neither the folder names nor the
  files say which side a run is on. The key is in
  `<out dir>/grading/keys/<grader>.json`, which no grader is given.

Each grader of a `type: llm` file reads one `<grader>` folder and writes
`verdict-<name>.json` there. `apply` needs `--graders` such files (default
2) for every `type: llm` grader, each covering every run. When they agree
on every run, it writes each run's `grading.json` in grader file order, as
tally.py reads it. When they disagree on any run, it lists the runs and
writes nothing: the expectation's text is read two ways and is corrected
before the runs are graded again.

Exit 0 on success, 64 on a usage error, 65 when verdicts are missing,
unreadable or in disagreement, 66 when the case has no graders or the out
dir has no runs.
"""

import argparse
import json
import random
import re
import shutil
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tally import runs, sides  # noqa: E402

RESULT_LIMIT = 1500

TASK = """# Grade one expectation across every run

Read `expectation.md` in this folder. It holds one expectation.

Each folder under `runs/` is one run of the same eval case. The names are
shuffled; nothing says which skill text a run used. For each run read:

- `calls.log`: the exact argv, order and exit code of every stubbed `emb`
  call.
- `owner.log`: the owner's replies that were sent.
- `trace.txt`: the session in order: every assistant text, every tool call
  with its full input (including `run_in_background`), each tool result cut
  to {limit} characters, the owner's replies and each turn's final text.

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
    parts = text.split("---\n", 2)
    if text.startswith("---\n") and len(parts) == 3:
        for line in parts[1].splitlines():
            key, found, value = line.partition(":")
            if found:
                meta[key.strip()] = value.strip().strip("'\"")
        text = parts[2]
    return meta, text.strip()


def events(trace: Path) -> list[dict[str, Any]]:
    """Return the JSON objects of a stream-json trace, skipping other lines."""
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


def compact(trace: Path, limit: int = RESULT_LIMIT) -> str:
    """Return an ordered text view of a trace with tool results cut to the limit."""
    lines = []
    number = 0
    for item in events(trace):
        kind = item.get("type")
        if kind == "result":
            lines.append(
                f"=== TURN RESULT (final text of this turn):\n{item.get('result')}\n"
            )
        if kind not in ("assistant", "user"):
            continue
        for part in parts(item):
            number += 1
            what = part.get("type")
            if what == "text":
                lines.append(f"[{number}] {kind.upper()} TEXT:\n{part.get('text')}\n")
            elif what == "tool_use":
                shown = json.dumps(part.get("input"), ensure_ascii=False)
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
                body = str(body)
                if len(body) > limit:
                    body = body[:limit] + f"\n...[cut, {len(body)} chars]"
                lines.append(f"[{number}] TOOL RESULT:\n{body}\n")
    return "\n".join(lines)


def order(first: str, then: str, called: list[str]) -> tuple[bool, str]:
    """Return whether a `first` call precedes the first `then` call, with evidence."""
    before = next((i for i, text in enumerate(called, 1) if re.search(first, text)), 0)
    after = next((i for i, text in enumerate(called, 1) if re.search(then, text)), 0)
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


def scrub(text: str, run: Path, out: Path) -> str:
    """Replace the run's and the out dir's paths, which name the side."""
    for path, mark in ((run, "<RUN>"), (out, "<OUT>")):
        for spelling in {str(path), str(path.resolve())}:
            text = text.replace(spelling, mark)
    return text


def batch(case: Path, out: Path, seed: int) -> int:
    """Decide script graders and lay out blind run folders for the others."""
    root = out / "grading"
    found = all_runs(out)
    for grader in graders(case):
        meta, body = front(grader)
        folder = root / grader.stem
        if folder.exists():
            shutil.rmtree(folder)
        folder.mkdir(parents=True)
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
            (folder / "script.json").write_text(
                json.dumps(verdicts, indent=2) + "\n", encoding="utf-8"
            )
            print(f"{grader.stem}: script, {len(verdicts)} runs decided")
            continue
        shuffled = list(found)
        random.Random(f"{seed}:{grader.stem}").shuffle(shuffled)
        key = {}
        for number, (name, run) in enumerate(shuffled, 1):
            ident = f"r{number:02d}"
            key[ident] = name
            target = folder / "runs" / ident
            target.mkdir(parents=True)
            for log in ("calls.log", "owner.log"):
                source = run / log
                text = source.read_text(encoding="utf-8") if source.is_file() else ""
                (target / log).write_text(scrub(text, run, out), encoding="utf-8")
            (target / "trace.txt").write_text(
                scrub(compact(run / "trace.jsonl"), run, out), encoding="utf-8"
            )
        (folder / "expectation.md").write_text(body + "\n", encoding="utf-8")
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
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Return the agreed verdict per run of one llm grader, and the problems."""
    files = sorted(folder.glob("verdict-*.json"))
    problems = []
    if len(files) < need:
        problems.append(f"has {len(files)} of {need} verdict files")
    loaded = []
    for path in files:
        try:
            graded = json.loads(path.read_text(encoding="utf-8"))["runs"]
            for ident in key:
                if not isinstance(graded[ident]["passed"], bool):
                    raise TypeError(f"{ident} has no true or false passed")
        except (ValueError, KeyError, TypeError) as error:
            problems.append(f"{path.name} unreadable or incomplete: {error!r}")
            continue
        loaded.append((path.stem.removeprefix("verdict-"), graded))
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
    return agreed, problems


def apply(case: Path, out: Path, need: int) -> int:
    """Write each run's grading.json when every grader file has agreed verdicts."""
    root = out / "grading"
    found = all_runs(out)
    table: list[tuple[str, dict[str, dict[str, Any]]]] = []
    failed = False
    for grader in graders(case):
        meta, body = front(grader)
        folder = root / grader.stem
        problems: list[str] = []
        agreed: dict[str, dict[str, Any]] = {}
        try:
            if meta.get("type") == "script":
                agreed = json.loads((folder / "script.json").read_text("utf-8"))
            else:
                key = json.loads(
                    (root / "keys" / f"{grader.stem}.json").read_text("utf-8")
                )
                agreed, problems = verdicts_of(folder, key, need)
        except (OSError, ValueError) as error:
            problems = [f"not batched: {error}"]
        problems += [
            f"no verdict for {name}"
            for name, _ in found
            if name not in agreed and not problems
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
    args = parser.parse_args(argv)
    if not graders(args.case):
        print(f"grade.py: {args.case} has no graders/*.md", file=sys.stderr)
        return 66
    if not args.out.is_dir() or not sides(args.out):
        print(f"grade.py: {args.out} has no run-<i> folders", file=sys.stderr)
        return 66
    if args.graders < 1:
        print("grade.py: --graders is at least 1", file=sys.stderr)
        return 64
    if args.mode == "batch":
        return batch(args.case, args.out, args.seed)
    return apply(args.case, args.out, args.graders)


if __name__ == "__main__":
    sys.exit(main())
