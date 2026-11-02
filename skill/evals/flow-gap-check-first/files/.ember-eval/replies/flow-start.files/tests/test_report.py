"""Tests of tools/report.py."""

import contextlib
import io
import json
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

    def test_item_exact(self) -> None:
        """Matching preserves case."""
        self.assertEqual(
            self.call("--item", "Tea"), (0, "quantity: 2\nrevenue: 2.50\n", "")
        )
        self.assertEqual(
            self.call("--item", "tea"), (0, "quantity: 3\nrevenue: 6.30\n", "")
        )

    def test_item_missing(self) -> None:
        """An unmatched item gives zero totals."""
        self.assertEqual(
            self.call("--item", "absent"), (0, "quantity: 0\nrevenue: 0.00\n", "")
        )

    def test_json(self) -> None:
        """JSON uses numeric totals and composes with item filtering."""
        code, text, error = self.call("--item", "tea", "--json")
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(json.loads(text), {"quantity": 3, "revenue": 6.3})
        self.assertEqual(
            json.loads(self.call("--json")[1]), {"quantity": 6, "revenue": 12.8}
        )
        self.assertEqual(
            json.loads(self.call("--item", "absent", "--json")[1]),
            {"quantity": 0, "revenue": 0},
        )

    def test_json_rounds(self) -> None:
        """JSON revenue rounds once to two decimal places."""
        with open(self.path, "w", encoding="utf-8") as stream:
            stream.write("date,item,quantity,price\n2026-01-01,Tea,3,1.234\n")
        self.assertEqual(
            json.loads(self.call("--json")[1]), {"quantity": 3, "revenue": 3.7}
        )

    def test_usage(self) -> None:
        """Missing values and unknown options are usage errors."""
        for args in [("--item",), ("--unknown",)]:
            code, text, error = self.call(*args)
            self.assertEqual((code, text), (64, ""))
            self.assertTrue(error)


if __name__ == "__main__":
    unittest.main()
