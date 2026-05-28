#!/usr/bin/env python3
"""
calc_dividends.py
Reads data/transactions.csv and outputs total dividends per (symbol, portfolio).
Run from the project root: python scripts/calc_dividends.py
"""
import csv, sys
from collections import defaultdict
from pathlib import Path

csv_path = Path(__file__).parent.parent / "data" / "transactions.csv"
if not csv_path.exists():
    print(f"File not found: {csv_path}", file=sys.stderr)
    sys.exit(1)

totals = defaultdict(float)

with open(csv_path, newline="", encoding="utf-8") as fh:
    reader = csv.DictReader(fh)
    for row in reader:
        txtype = (row.get("Type") or "").strip()
        if txtype != "Dividend":
            continue
        sym  = (row.get("Symbol") or "").strip()
        port = (row.get("Portfolio") or "").strip()
        try:
            amt = float(row.get("Shares Owned") or 0)
        except ValueError:
            continue
        if amt > 0:
            totals[(sym, port)] += amt

print("\nDividends per position (native currency):")
print(f"{'Symbol':<12} {'Portfolio':<12} {'Total Dividends':>16}")
print("-" * 42)
for (sym, port), total in sorted(totals.items()):
    print(f"{sym:<12} {port:<12} {total:>16.2f}")

print("\nPython dict for HOLDINGS_STATIC:")
for (sym, port), total in sorted(totals.items()):
    print(f"  # {sym} / {port}: {total:.2f}")
