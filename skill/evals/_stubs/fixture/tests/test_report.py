"""Tests of tools/report.py."""

import os
import sys
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


if __name__ == "__main__":
    unittest.main()
