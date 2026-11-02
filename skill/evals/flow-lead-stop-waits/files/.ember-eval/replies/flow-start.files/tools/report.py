"""Print the totals of an orders file.

Usage: python3 tools/report.py [--strict] data/orders.csv
"""

import csv
import sys


def read_orders(path: str, *, strict: bool = True) -> list[dict[str, str]]:
    """Read orders, raising on bad quantities or skipping them with warnings."""
    rows = []
    with open(path, newline="", encoding="utf-8") as stream:
        reader = csv.DictReader(stream)
        for row in reader:
            try:
                int(row["quantity"])
            except (ValueError, TypeError) as error:
                message = f"line {reader.line_num}: quantity must be a whole number"
                if strict:
                    raise ValueError(message) from error
                print(f"warning: {message}", file=sys.stderr)
                continue
            rows.append(row)
    return rows


def totals(rows: list[dict[str, str]]) -> tuple[int, float]:
    """Return the total quantity and the total revenue of the rows."""
    quantity = sum(int(row["quantity"]) for row in rows)
    revenue = sum(int(row["quantity"]) * float(row["price"]) for row in rows)
    return quantity, round(revenue, 2)


def main(argv: list[str] | None = None) -> int:
    """Print valid totals or report a strict input error with exit 65."""
    argv = sys.argv[1:] if argv is None else argv
    strict = "--strict" in argv
    paths = [arg for arg in argv if arg != "--strict"]
    if argv.count("--strict") > 1 or len(paths) != 1 or paths[0].startswith("-"):
        print("usage: report.py [--strict] <orders.csv>", file=sys.stderr)
        return 64
    try:
        rows = read_orders(paths[0], strict=strict)
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 65
    quantity, revenue = totals(rows)
    print(f"quantity: {quantity}")
    print(f"revenue: {revenue:.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
