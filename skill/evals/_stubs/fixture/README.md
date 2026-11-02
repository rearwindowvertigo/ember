# order-tools

Small tools for a shop's order files.

`tools/report.py <orders.csv>` prints the total quantity and revenue. Each
row of the file has `date`, `item`, `quantity` and `price`, and every order
we recieve is one row.

Run the tests with `python3 -m unittest discover -s tests`.
