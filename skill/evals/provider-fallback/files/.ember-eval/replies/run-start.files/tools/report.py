"""Print the totals of an orders file.

Usage: python3 tools/report.py [--since YYYY-MM-DD] data/orders.csv
"""

import csv
import datetime
import re
import sys

USAGE = "usage: report.py [--since YYYY-MM-DD] <orders.csv>"


def read_orders(path: str) -> list[dict[str, str]]:
    """Return the rows of an orders CSV file as dictionaries."""
    with open(path, newline="", encoding="utf-8") as stream:
        return list(csv.DictReader(stream))


def totals(rows: list[dict[str, str]]) -> tuple[int, float]:
    """Return the total quantity and the total revenue of the rows."""
    quantity = sum(int(row["quantity"]) for row in rows)
    revenue = sum(int(row["quantity"]) * float(row["price"]) for row in rows)
    return quantity, round(revenue, 2)


def since(rows: list[dict[str, str]], day: datetime.date) -> list[dict[str, str]]:
    """Return the rows dated on or after `day`."""
    return [row for row in rows if datetime.date.fromisoformat(row["date"]) >= day]


def parse_day(text: str) -> datetime.date | None:
    """Return the day `text` names as YYYY-MM-DD, or None."""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return None
    try:
        return datetime.date.fromisoformat(text)
    except ValueError:
        return None


def main(argv: list[str] | None = None) -> int:
    """Print the totals of the file named in `argv`; return the exit code."""
    argv = list(sys.argv[1:] if argv is None else argv)
    day = None
    if "--since" in argv:
        index = argv.index("--since")
        if index + 1 >= len(argv):
            print("report.py: --since needs a day", file=sys.stderr)
            return 64
        text = argv[index + 1]
        day = parse_day(text)
        if day is None:
            print(f"report.py: {text!r} is not a day (YYYY-MM-DD)", file=sys.stderr)
            return 64
        del argv[index : index + 2]
    if len(argv) != 1:
        print(USAGE, file=sys.stderr)
        return 64
    rows = read_orders(argv[0])
    if day is not None:
        rows = since(rows, day)
    quantity, revenue = totals(rows)
    print(f"quantity: {quantity}")
    print(f"revenue: {revenue:.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
