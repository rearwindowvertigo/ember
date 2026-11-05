"""Count passed graders across repeated runs of one eval case.

Usage: tally.py <case dir> <out dir> [--json]

The out dir is one that run_repeat.sh filled: one folder per side (`skill`,
and `base` with `--base`), each holding `run-<i>` folders. A run counts once
its `grading.json` has one expectation per file in the case's `graders/`,
in file order, each with a true or false `passed`. Other runs are listed on
standard error as not graded and left out of the counts.

Exit 0 on success, 64 on a usage error, 66 when the case has no graders or
the out dir has no runs.
"""

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

RUN = re.compile(r"run-(\d+)")


def sides(out: Path) -> list[Path]:
    """Return the side folders that hold runs, `skill` first and `base` second."""
    found = [
        path
        for path in out.iterdir()
        if path.is_dir() and any(RUN.fullmatch(run.name) for run in path.iterdir())
    ]
    order = {"skill": 0, "base": 1}
    return sorted(found, key=lambda path: (order.get(path.name, 2), path.name))


def runs(side: Path) -> list[Path]:
    """Return a side's run folders in run order."""
    numbered = [
        (int(match.group(1)), path)
        for path in side.iterdir()
        if path.is_dir() and (match := RUN.fullmatch(path.name))
    ]
    return [path for _, path in sorted(numbered)]


def results(run: Path, count: int) -> list[bool] | str:
    """Return one passed value per grader, or why the run is not graded."""
    path = run / "grading.json"
    if not path.is_file():
        return "no grading.json"
    try:
        expectations = json.loads(path.read_text(encoding="utf-8"))["expectations"]
    except (ValueError, KeyError, TypeError) as error:
        return f"grading.json unreadable: {error}"
    if not isinstance(expectations, list) or len(expectations) != count:
        got = len(expectations) if isinstance(expectations, list) else "no list of"
        return f"grading.json has {got} expectations, the case has {count} graders"
    passed = [
        item.get("passed") if isinstance(item, dict) else None for item in expectations
    ]
    if not all(isinstance(value, bool) for value in passed):
        return "grading.json has an expectation without a true or false passed"
    return [bool(value) for value in passed]


def tally(case: Path, out: Path) -> dict[str, Any]:
    """Count passes per grader and for the whole case, for each side."""
    graders = sorted(path.stem for path in (case / "graders").glob("*.md"))
    table: dict[str, Any] = {"case": case.name, "graders": graders, "sides": {}}
    for side in sides(out):
        graded: list[list[bool]] = []
        skipped: dict[str, str] = {}
        for run in runs(side):
            result = results(run, len(graders))
            if isinstance(result, str):
                skipped[run.name] = result
            else:
                graded.append(result)
        table["sides"][side.name] = {
            "graded": len(graded),
            "passed": [
                sum(run[index] for run in graded) for index in range(len(graders))
            ],
            "all_passed": sum(all(run) for run in graded),
            "not_graded": skipped,
        }
    return table


def render(table: dict[str, Any]) -> str:
    """Return the counts as a plain table, one row per grader."""
    names = list(table["sides"])
    rows = [["grader", *names]]
    for index, grader in enumerate(table["graders"]):
        rows.append(
            [
                grader,
                *(
                    f"{side['passed'][index]}/{side['graded']}"
                    for side in table["sides"].values()
                ),
            ]
        )
    rows.append(
        [
            "all graders",
            *(
                f"{side['all_passed']}/{side['graded']}"
                for side in table["sides"].values()
            ),
        ]
    )
    widths = [max(len(row[column]) for row in rows) for column in range(len(rows[0]))]
    lines = [
        "  ".join(
            cell.ljust(width) for cell, width in zip(row, widths, strict=True)
        ).rstrip()
        for row in rows
    ]
    return "\n".join([table["case"], *lines])


def main(argv: list[str] | None = None) -> int:
    """Print the counts for one case's out dir."""
    parser = argparse.ArgumentParser(
        prog="tally.py", description=__doc__.splitlines()[0]
    )
    parser.add_argument("case", type=Path, help="the case folder")
    parser.add_argument("out", type=Path, help="the out dir run_repeat.sh filled")
    parser.add_argument("--json", action="store_true", help="print the counts as JSON")
    args = parser.parse_args(argv)
    if not any((args.case / "graders").glob("*.md")):
        print(f"tally.py: {args.case} has no graders/*.md", file=sys.stderr)
        return 66
    if not args.out.is_dir() or not sides(args.out):
        print(f"tally.py: {args.out} has no run-<i> folders", file=sys.stderr)
        return 66
    table = tally(args.case, args.out)
    for name, side in table["sides"].items():
        for run, reason in side["not_graded"].items():
            print(f"{name} {run} not graded: {reason}", file=sys.stderr)
    print(json.dumps(table, indent=2) if args.json else render(table))
    return 0


if __name__ == "__main__":
    sys.exit(main())
