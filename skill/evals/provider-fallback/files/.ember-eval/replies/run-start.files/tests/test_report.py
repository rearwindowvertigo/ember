"""Tests of tools/report.py."""

import contextlib
import io
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
import report  # noqa: E402

DATA = os.path.join(os.path.dirname(__file__), "..", "data", "orders.csv")


def run(*argv: str) -> tuple[int, str, str]:
    """Run the tool's main and return its exit code, stdout and stderr."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = report.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class ReportTests(unittest.TestCase):
    """The totals of the sample file."""

    def test_totals(self) -> None:
        """The sample file's totals."""
        self.assertEqual(report.totals(report.read_orders(DATA)), (21, 127.5))

    def test_empty(self) -> None:
        """No rows give zero totals."""
        self.assertEqual(report.totals([]), (0, 0))

    def test_since_inside_the_data(self) -> None:
        """Orders before the day are left out."""
        self.assertEqual(
            run("--since", "2026-10-05", DATA),
            (0, "quantity: 12\nrevenue: 58.50\n", ""),
        )

    def test_since_counts_the_day_itself(self) -> None:
        """An order dated that day counts."""
        self.assertEqual(
            run("--since", "2026-10-07", DATA),
            (0, "quantity: 12\nrevenue: 58.50\n", ""),
        )

    def test_since_after_every_order(self) -> None:
        """A day after every order gives zero totals."""
        self.assertEqual(
            run(DATA, "--since", "2026-11-01"), (0, "quantity: 0\nrevenue: 0.00\n", "")
        )

    def test_malformed_days(self) -> None:
        """A malformed or impossible day is a usage error."""
        for text in ("2026-13-40", "2026-02-30", "20261007", "2026-1-7"):
            code, out, err = run("--since", text, DATA)
            self.assertEqual((code, out), (64, ""))
            self.assertIn(text, err)

    def test_since_without_a_day(self) -> None:
        """--since with no day is a usage error."""
        code, out, err = run(DATA, "--since")
        self.assertEqual((code, out), (64, ""))
        self.assertIn("--since", err)


if __name__ == "__main__":
    unittest.main()
