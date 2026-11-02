"""Print the totals of an orders file.

Usage: python3 tools/report.py data/orders.csv
"""

import csv
import datetime
import getopt
import re
import sys


def read_orders(path: str) -> list[dict[str, str]]:
    """Return the rows of an orders CSV file as dictionaries."""
    with open(path, newline="", encoding="utf-8") as stream:
        return list(csv.DictReader(stream))


def totals(rows: list[dict[str, str]]) -> tuple[int, float]:
    """Return the total quantity and the total revenue of the rows."""
    quantity = sum(int(row["quantity"]) for row in rows)
    revenue = sum(int(row["quantity"]) * float(row["price"]) for row in rows)
    return quantity, round(revenue, 2)


def main(argv: list[str] | None = None) -> int:
    """Print totals for orders strictly before an optional cutoff day."""
    argv = sys.argv[1:] if argv is None else argv
    try:
        flags, paths = getopt.gnu_getopt(argv, "", ["before="])
        if len(paths) != 1:
            raise ValueError("expected one orders CSV path")
        value = next((value for flag, value in reversed(flags)), None)
        if value is not None and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError("--before needs YYYY-MM-DD")
        cutoff = datetime.date.fromisoformat(value) if value is not None else None
    except (getopt.GetoptError, ValueError) as error:
        print(f"report.py: {error}", file=sys.stderr)
        return 64
    rows = read_orders(paths[0])
    if cutoff is not None:
        rows = [
            row for row in rows if datetime.date.fromisoformat(row["date"]) < cutoff
        ]
    quantity, revenue = totals(rows)
    print(f"quantity: {quantity}")
    print(f"revenue: {revenue:.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
