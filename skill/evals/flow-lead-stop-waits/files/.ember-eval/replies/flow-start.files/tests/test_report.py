"""Tests of tools/report.py."""

import contextlib
import io
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
import report  # noqa: E402

DATA = os.path.join(os.path.dirname(__file__), "..", "data", "orders.csv")


class ReportTests(unittest.TestCase):
    """The totals of the sample file."""

    def test_totals(self) -> None:
        """The sample file's totals."""
        self.assertEqual(report.totals(report.read_orders(DATA)), (21, 127.5))

    def test_empty(self) -> None:
        """No rows give zero totals."""
        self.assertEqual(report.totals([]), (0, 0))


class OptionTests(unittest.TestCase):
    """Options with a private orders file for each test."""

    def setUp(self) -> None:
        """Write sample orders in an independent folder."""
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = os.path.join(folder.name, "orders.csv")
        with open(self.path, "w", encoding="utf-8") as stream:
            stream.write(
                "date,item,quantity,price\n"
                "2026-01-01,Tea,2,1.25\n"
                "2026-01-02,tea,3,2.10\n"
                "2026-01-03,Coffee,1,4.00\n"
            )

    def call(self, *args: str) -> tuple[int, str, str]:
        """Capture the command's result and streams."""
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = report.main([self.path, *args])
        return code, stdout.getvalue(), stderr.getvalue()

    def bad_rows(self) -> None:
        """Append a non-integer quantity after valid rows."""
        with open(self.path, "a", encoding="utf-8") as stream:
            stream.write("2026-01-04,Tea,1.5,2.00\n")

    def test_read_orders_names_line(self) -> None:
        """The reader raises ValueError naming the bad row's line."""
        self.bad_rows()
        with self.assertRaisesRegex(ValueError, "line 5"):
            report.read_orders(self.path)

    def test_strict_error(self) -> None:
        """Strict mode reports the error without totals."""
        self.bad_rows()
        self.assertEqual(
            self.call("--strict"), (65, "", "line 5: quantity must be a whole number\n")
        )

    def test_non_strict_warns(self) -> None:
        """Without strict mode, valid rows count and bad rows warn."""
        self.bad_rows()
        self.assertEqual(
            self.call(),
            (
                0,
                "quantity: 6\nrevenue: 12.80\n",
                "warning: line 5: quantity must be a whole number\n",
            ),
        )

    def test_strict_valid(self) -> None:
        """Strict mode preserves valid totals."""
        self.assertEqual(
            self.call("--strict"), (0, "quantity: 6\nrevenue: 12.80\n", "")
        )

    def test_non_numeric_and_empty_quantities(self) -> None:
        """A word or an empty quantity is also a bad row."""
        with open(self.path, "a", encoding="utf-8") as stream:
            stream.write("2026-01-04,Tea,x,2.00\n2026-01-05,Tea,,2.00\n")
        code, out, err = self.call()
        self.assertEqual((code, out), (0, "quantity: 6\nrevenue: 12.80\n"))
        self.assertEqual(
            err.splitlines(),
            [
                "warning: line 5: quantity must be a whole number",
                "warning: line 6: quantity must be a whole number",
            ],
        )

    def test_strict_is_exact(self) -> None:
        """Only the exact option name turns on strict mode."""
        self.assertEqual(self.call("--str")[0], 64)
        self.assertEqual(self.call("--strict", "--strict")[0], 64)


if __name__ == "__main__":
    unittest.main()
