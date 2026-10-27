#!/usr/bin/env python
"""Independent read-only SQL cross-check for the DataAgent project database.

Why English output labels?  On Windows PowerShell, piping Chinese text through
`python -c "..."` can be mangled by the console code page and show as "????" —
that is a **display/encoding problem, not a calculation error**.  This script
therefore prints ASCII-only labels, so it works the same in cmd / PowerShell /
WSL / CI.

Read-only guarantee:
  - the DuckDB connection is opened read-only (`read_only=True`);
  - the script only issues SELECT statements;
  - the full requested date window is used (no silent truncation); if the
    database does not cover the window, the script says so explicitly instead
    of adjusting the dates.

Usage (Windows PowerShell):
  .\\.venv\\Scripts\\python.exe scripts\\verify_month_aggregate.py `
      --db data\\processed\\dataagent_demo.duckdb `
      --start 2014-08-01 --end 2014-08-31 --metric purchase

Usage (any shell):
  python scripts/verify_month_aggregate.py --db <path> --start 2014-08-01 --end 2014-08-31

Output (ASCII labels only):
  metric            : purchase
  date_from         : 2014-08-01
  date_to           : 2014-08-31
  total_amount      : 7921353364
  distinct_dates    : 31
  covered_by_db     : yes
  sql               : SELECT SUM(total_purchase_amt) ...
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

METRICS = {
    "purchase": ("total_purchase_amt", "purchase"),
    "redeem": ("total_redeem_amt", "redeem"),
    "net": ("total_purchase_amt - total_redeem_amt", "net"),
}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Read-only SQL cross-check (ASCII output labels)")
    ap.add_argument("--db", required=True, type=Path,
                    help="path to the DuckDB database (read-only)")
    ap.add_argument("--start", required=True, help="start date, YYYY-MM-DD")
    ap.add_argument("--end", required=True, help="end date, YYYY-MM-DD")
    ap.add_argument("--metric", default="purchase", choices=sorted(METRICS),
                    help="which amount to aggregate")
    ap.add_argument("--table", default="user_balance_table",
                    help="table name (default: user_balance_table)")
    args = ap.parse_args(argv)

    import duckdb

    if not args.db.is_file():
        print(f"error: database file not found: {args.db}")
        return 1

    expr, name = METRICS[args.metric]
    sql = (f"SELECT COALESCE(SUM({expr}), 0) AS total_amount, "
           f"COUNT(DISTINCT report_date) AS distinct_dates "
           f"FROM {args.table} WHERE report_date BETWEEN ? AND ?")

    con = duckdb.connect(str(args.db), read_only=True)
    try:
        row = con.execute(sql, [args.start, args.end]).fetchone()
        total, n_dates = row[0], row[1]
        # 数据覆盖范围（只读查询）
        cov = con.execute(
            f"SELECT MIN(report_date), MAX(report_date) FROM {args.table}"
        ).fetchone()
    finally:
        con.close()

    covered = bool(cov and cov[0] and cov[1]
                    and str(cov[0]) <= args.start and str(cov[1]) >= args.end)

    print(f"metric            : {name}")
    print(f"date_from         : {args.start}")
    print(f"date_to           : {args.end}")
    print(f"total_amount      : {total}")
    print(f"distinct_dates    : {n_dates}")
    print(f"covered_by_db     : {'yes' if covered else 'no (window exceeds db coverage)'}")
    print(f"db_date_min       : {cov[0] if cov else 'n/a'}")
    print(f"db_date_max       : {cov[1] if cov else 'n/a'}")
    print(f"sql               : {sql}")
    print("note              : read-only query; full requested window used "
          "(no truncation applied)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
