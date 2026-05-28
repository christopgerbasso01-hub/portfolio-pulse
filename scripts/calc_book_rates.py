#!/usr/bin/env python3
"""
calc_book_rates.py
------------------
Reads transactions.csv, computes the weighted-average USD/CAD book rate
for every currently-held USD-denominated position.

Rules:
  • If a Buy row has a non-zero Purchase Exchange Rate  → use it directly.
  • If a Buy row has rate = 0 or blank               → fetch that day's
    close from USDCAD=X via yfinance.

Output: a Python dict ready to paste into market.py as book_rate fields.
"""

import csv
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

try:
    import yfinance as yf
    HAS_YF = True
except ImportError:
    print("yfinance not installed — run: pip install yfinance", file=sys.stderr)
    HAS_YF = False

# -----------------------------------------------------------------------
# Currently-held USD positions (ticker, account)
# These must exactly match what is in market.py HOLDINGS.
# -----------------------------------------------------------------------
HELD_USD = [
    ("FNGU",  "TFSA"),
    ("NVDA",  "TFSA"),
    ("SPXL",  "TFSA"),
    ("TSLA",  "TFSA"),
    ("UDOW",  "TFSA"),
    ("AVGO",  "TFSA"),
    ("COST",  "TFSA"),
    ("NFLX",  "TFSA"),
    ("MSFT",  "TFSA"),
    ("AAPL",  "TFSA"),
    ("QCOM",  "TFSA"),
    ("ET",    "TFSA"),
    ("SPXL",  "Investment"),
    ("FNGU",  "Investment"),
    ("TSM",   "Investment"),
    ("IBKR",  "Investment"),
    ("V",     "Investment"),
    ("LYV",   "Investment"),
    ("MSTR",  "Investment"),
    ("GBTC",  "Investment"),
    ("BYDDF", "Investment"),
    ("SPXL",  "FHSA"),
    ("UDOW",  "FHSA"),
    ("FNGU",  "FHSA"),
    ("FNGU",  "RRSP"),
    ("TSM",   "RRSP"),
    ("UDOW",  "RRSP"),
]

# -----------------------------------------------------------------------
# Fetch helpers
# -----------------------------------------------------------------------
_rate_cache: dict[str, float] = {}

def get_usdcad(date_str: str) -> float:
    """Return the USDCAD close price for date_str (YYYY-MM-DD).
    Tries up to 5 days back to handle weekends / holidays."""
    if date_str in _rate_cache:
        return _rate_cache[date_str]
    if not HAS_YF:
        return 1.3925
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    for offset in range(6):
        candidate = dt - timedelta(days=offset)
        start = candidate.strftime("%Y-%m-%d")
        end   = (candidate + timedelta(days=1)).strftime("%Y-%m-%d")
        try:
            df = yf.download("USDCAD=X", start=start, end=end,
                             progress=False, auto_adjust=True)
            if not df.empty:
                rate = float(df["Close"].iloc[-1])
                print(f"  USDCAD {date_str} → {rate:.5f} (from {start})")
                _rate_cache[date_str] = rate
                return rate
        except Exception as exc:
            print(f"  yfinance error for {date_str}: {exc}", file=sys.stderr)
        time.sleep(0.2)
    fallback = 1.3925
    print(f"  WARNING: no rate found for {date_str}, using fallback {fallback}")
    _rate_cache[date_str] = fallback
    return fallback

# -----------------------------------------------------------------------
# Parse CSV
# -----------------------------------------------------------------------
def parse_transactions(csv_path: str) -> list[dict]:
    rows = []
    with open(csv_path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            sym   = row["Symbol"].strip()
            port  = row["Portfolio"].strip()
            ccy   = row["Currency"].strip()
            txtype = row["Type"].strip()
            shares_str = (row["Shares Owned"] or "0").strip()
            price_str  = (row["Cost Per Share"] or "0").strip()
            comm_str   = (row["Commission"] or "0").strip()
            rate_str   = (row["Purchase Exchange Rate"] or "0").strip()
            date_raw   = (row["Transaction Date"] or "").strip()

            try:
                shares = float(shares_str)
                price  = float(price_str)
                comm   = float(comm_str)
                rate   = float(rate_str)
            except ValueError:
                continue

            # Normalise date  "2020-08-14 GMT+0200" → "2020-08-14"
            date = date_raw.split(" ")[0] if date_raw else ""

            rows.append({
                "symbol": sym,
                "portfolio": port,
                "ccy": ccy,
                "type": txtype,
                "shares": shares,
                "price": price,
                "comm": comm,
                "rate": rate,    # 0 means missing
                "date": date,
            })
    return rows

# -----------------------------------------------------------------------
# For each held USD position, accumulate weighted-average book rate
# -----------------------------------------------------------------------
def compute_book_rates(rows: list[dict]) -> tuple[dict[tuple, float], dict[tuple, dict]]:
    """Returns (book_rates, totals) where totals has 'usd' and 'cad' costs per position."""
    # Filter only USD buy rows for the held positions
    held_set = set(HELD_USD)

    # Accumulate: total_usd_cost and total_cad_cost per position
    totals: dict[tuple, dict] = {k: {"usd": 0.0, "cad": 0.0} for k in held_set}

    dates_needed = set()

    # First pass: collect which dates need a rate lookup
    for r in rows:
        key = (r["symbol"], r["portfolio"])
        if key not in held_set:
            continue
        if r["ccy"] != "USD":
            continue
        if r["type"] not in ("Buy",):
            continue
        if r["price"] <= 0:
            continue
        if r["rate"] == 0:
            dates_needed.add(r["date"])

    # Fetch missing rates
    print(f"\nFetching {len(dates_needed)} historical USDCAD rates...")
    for d in sorted(dates_needed):
        get_usdcad(d)

    # Second pass: accumulate
    for r in rows:
        key = (r["symbol"], r["portfolio"])
        if key not in held_set:
            continue
        if r["ccy"] != "USD":
            continue
        if r["type"] not in ("Buy",):
            continue
        if r["price"] <= 0:
            continue

        rate = r["rate"] if r["rate"] > 0 else _rate_cache.get(r["date"], 1.3925)
        usd_cost = r["shares"] * r["price"] + r["comm"]   # include commission

        totals[key]["usd"] += usd_cost
        totals[key]["cad"] += usd_cost * rate

    result = {}
    for key, t in totals.items():
        if t["usd"] > 0:
            result[key] = round(t["cad"] / t["usd"], 5)
        else:
            print(f"  WARNING: no buy data found for {key}")
            result[key] = 1.3925  # fallback

    return result, totals

# -----------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------
def main():
    csv_path = Path(__file__).parent.parent / "data" / "transactions.csv"
    if not csv_path.exists():
        print(f"CSV not found at {csv_path}", file=sys.stderr)
        sys.exit(1)

    print(f"Reading {csv_path}")
    rows = parse_transactions(str(csv_path))
    print(f"  {len(rows)} transaction rows parsed")

    book_rates, totals = compute_book_rates(rows)

    print("\n" + "=" * 70)
    print("BOOK RATES & COST TOTALS PER POSITION")
    print("=" * 70)
    print(f"  {'Ticker':<10} {'Account':<12} {'book_rate':>10}  {'cost_total USD':>16}  {'cost_total CAD':>16}")
    print("  " + "-" * 66)
    for (ticker, account), rate in sorted(book_rates.items()):
        t = totals[(ticker, account)]
        cad = t["cad"]
        usd = t["usd"]
        print(f"  {ticker:<10} {account:<12} {rate:>10.5f}  ${usd:>14,.2f}  ${cad:>14,.2f}")

    print("\n# ── index.html HOLDINGS_STATIC — paste these book_rate values ──")
    print("# For positions currently WITHOUT book_rate:")
    print("# 1. Add  book_rate:<value>  to the entry")
    print("# 2. Update cost_total to the USD amount below")
    print("# 3. Remove any hard-coded CAD cost_total (it was a fallback)")
    print()
    for (ticker, account), rate in sorted(book_rates.items()):
        t = totals[(ticker, account)]
        print(f'  # {ticker:<10} / {account:<12}  →  book_rate:{rate:.5f},  cost_total:{t["usd"]:.2f} USD  (CAD paid: ${t["cad"]:.2f})')

    return book_rates, totals

if __name__ == "__main__":
    main()
