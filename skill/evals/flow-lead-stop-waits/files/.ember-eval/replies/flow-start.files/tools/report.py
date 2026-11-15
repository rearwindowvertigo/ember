"""Print the totals of an orders file.

Usage: python3 tools/report.py data/orders.csv
"""

import csv
import sys


def read_orders(path: str) -> list[dict[str, str]]:
    """Read orders, raising on quantities that are not whole numbers."""
    rows = []
    with open(path, newline="", encoding="utf-8") as stream:
        reader = csv.DictReader(stream)
        for row in reader:
            try:
                int(row["quantity"])
            except (ValueError, TypeError) as error:
                message = f"line {reader.line_num}: quantity must be a whole number"
                raise ValueError(message) from error
            rows.append(row)
    return rows


def totals(rows: list[dict[str, str]]) -> tuple[int, float]:
    """Return the total quantity and the total revenue of the rows."""
    quantity = sum(int(row["quantity"]) for row in rows)
    revenue = sum(int(row["quantity"]) * float(row["price"]) for row in rows)
    return quantity, round(revenue, 2)


def main(argv: list[str] | None = None) -> int:
    """Print the totals of the file named in `argv`; return the exit code."""
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print("usage: report.py <orders.csv>", file=sys.stderr)
        return 64
    quantity, revenue = totals(read_orders(argv[0]))
    print(f"quantity: {quantity}")
    print(f"revenue: {revenue:.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
