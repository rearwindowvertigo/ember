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

    def test_before_excludes_day(self) -> None:
        """An order on the cutoff day does not count."""
        self.assertEqual(
            self.call("--before", "2026-01-02"), (0, "quantity: 2\nrevenue: 2.50\n", "")
        )

    def test_bad_day(self) -> None:
        """Malformed and impossible dates are usage errors."""
        for day in ["bad", "2026-02-30", "20260101"]:
            code, text, error = self.call("--before", day)
            self.assertEqual((code, text), (64, ""))
            self.assertTrue(error)


if __name__ == "__main__":
    unittest.main()
