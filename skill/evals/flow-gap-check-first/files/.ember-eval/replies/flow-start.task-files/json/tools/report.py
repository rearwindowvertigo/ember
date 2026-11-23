"""Print the totals of an orders file.

Usage: python3 tools/report.py data/orders.csv
"""

import csv
import getopt
import json
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
    """Print totals, optionally selecting an item and emitting JSON."""
    argv = sys.argv[1:] if argv is None else argv
    try:
        flags, paths = getopt.gnu_getopt(argv, "", ["item=", "json"])
        if len(paths) != 1:
            raise ValueError("expected one orders CSV path")
    except (getopt.GetoptError, ValueError) as error:
        print(f"report.py: {error}", file=sys.stderr)
        return 64
    item = next((value for flag, value in reversed(flags) if flag == "--item"), None)
    rows = read_orders(paths[0])
    if item is not None:
        rows = [row for row in rows if row["item"] == item]
    quantity, revenue = totals(rows)
    if any(flag == "--json" for flag, _ in flags):
        print(json.dumps({"quantity": quantity, "revenue": revenue}))
    else:
        print(f"quantity: {quantity}")
        print(f"revenue: {revenue:.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
