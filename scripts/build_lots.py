#!/usr/bin/env python3
"""
build_lots.py — per-purchase lots for every position held when the
transaction log began, replayed from data/transactions.csv.

The dashboard's ledger starts from _HOLDINGS_BASE (one aggregate row per
position) plus the transactions logged since 2026-06-04. That is enough for
position totals but not for P&L by purchase. This replays the full CSV history
up to the log start and writes data/lots_base.json, which the holding detail
panel loads; logged trades are then replayed on top in the browser.

Method (Canadian average-cost):
  • each Buy opens a lot; its CAD cost uses the row's exchange rate, or that
    day's USDCAD close when the row has none; commission is added (CAD);
  • a Sell reduces EVERY open lot by the same fraction, so remaining lot costs
    always sum to the position's ACB; realized = proceeds − average cost;
  • a Split multiplies the shares of every open lot;
  • a Dividend is shared across open lots by shares held that day.
Finally each position is reconciled to _HOLDINGS_BASE: share counts must
match, and lot costs and dividends are scaled so they sum exactly to the
base ACB and dividends the dashboard already shows.

Run: python3 scripts/build_lots.py   (needs network for USDCAD history)
"""
import csv
import json
import re
import sys
import time
from bisect import bisect_right
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
CUTOFF = "2026-06-04"            # first day of the dashboard's transaction log
ALIASES = {("BMO", "TFSA"): "BMO.TO"}   # bought as NYSE:BMO, held as BMO.TO
_UA = {"User-Agent": "Mozilla/5.0 (Macintosh) PortfolioPulse/1.0"}


def load_base() -> dict:
    src = (ROOT / "index.html").read_text()
    i = src.index("const _HOLDINGS_BASE = [")
    j = src.index("];", i)
    out = {}
    for obj in re.findall(r"\{[^{}]+\}", src[i:j]):
        f = dict(re.findall(r'(\w+):("[^"]*"|[-\d.]+)', obj))
        val = lambda k, d=None: (f[k].strip('"') if f.get(k, "").startswith('"') else float(f[k])) if k in f else d
        key = f'{val("ticker")}|{val("account")}'
        out[key] = {"ticker": val("ticker"), "account": val("account"), "ccy": val("ccy"),
                    "shares": val("shares"), "cost_total": val("cost_total"),
                    "book_rate": val("book_rate"), "dividends": val("dividends", 0.0),
                    "realized": val("realized", 0.0)}
    return out


def usdcad_history() -> tuple:
    url = ("https://query2.finance.yahoo.com/v8/finance/chart/USDCAD=X"
           f"?period1=1546300800&period2={int(time.time())}&interval=1d")
    r = requests.get(url, headers=_UA, timeout=20)
    r.raise_for_status()
    res = r.json()["chart"]["result"][0]
    pts = [(datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%d"), c)
           for t, c in zip(res["timestamp"], res["indicators"]["quote"][0]["close"]) if c]
    return [d for d, _ in pts], [c for _, c in pts]


def main() -> int:
    base = load_base()
    fx_dates, fx_vals = usdcad_history()

    def fx_on(date: str) -> float:
        i = max(0, bisect_right(fx_dates, date) - 1)
        return fx_vals[i]

    rows = list(csv.DictReader(open(ROOT / "data" / "transactions.csv")))
    rows = [r for r in rows if r["Transaction Date"] and r["Type"]]
    rows.sort(key=lambda r: (r["Transaction Date"][:10], r["Transaction Time"] or ""))

    pos = {}
    num = lambda s: float(s) if s not in ("", None) else 0.0
    for r in rows:
        date = r["Transaction Date"][:10]
        if date >= CUTOFF:
            continue
        sym, acct, typ, ccy = r["Symbol"], r["Portfolio"], r["Type"], r["Currency"]
        ticker = ALIASES.get((sym, acct), sym)
        p = pos.setdefault(f"{ticker}|{acct}", {"lots": [], "events": [], "div_unalloc": 0.0})
        rate = num(r["Purchase Exchange Rate"])
        fx = 1.0 if ccy != "USD" else (rate if rate > 0 else fx_on(date))
        sh, px, comm = num(r["Shares Owned"]), num(r["Cost Per Share"]), num(r["Commission"])
        open_sh = sum(l["shares"] for l in p["lots"])
        if typ == "Buy":
            cost = sh * px * fx + comm
            p["lots"].append({"date": date, "bought": sh, "shares": sh, "price": px, "ccy": ccy,
                              "fx": round(fx, 6), "split": 1.0, "cost": cost, "orig_cost": cost, "div": 0.0})
            p["events"].append({"date": date, "type": "BUY", "shares": sh, "price": px, "ccy": ccy,
                                "fx": round(fx, 6), "commission": comm})
        elif typ in ("Sell", "Sell All"):
            qty = open_sh if (typ == "Sell All" or sh <= 0) else min(sh, open_sh)
            if qty <= 0:
                continue
            acb_total = sum(l["cost"] for l in p["lots"])
            acb_sold = acb_total * qty / open_sh
            proceeds = qty * px * fx - comm
            frac = qty / open_sh
            for l in p["lots"]:
                l["shares"] *= 1 - frac
                l["cost"] *= 1 - frac
            p["lots"] = [l for l in p["lots"] if l["shares"] > 1e-9]
            p["events"].append({"date": date, "type": "SELL", "shares": round(qty, 6), "price": px, "ccy": ccy,
                                "fx": round(fx, 6), "commission": comm, "acb": round(acb_sold, 2),
                                "proceeds": round(proceeds, 2), "realized": round(proceeds - acb_sold, 2)})
        elif typ == "Split":
            ratio = sh / (px or 1)
            for l in p["lots"]:
                l["shares"] *= ratio
                l["split"] *= ratio
            p["events"].append({"date": date, "type": "SPLIT", "ratio": ratio})
        elif typ == "Dividend":
            amt = sh * fx
            if open_sh > 0:
                for l in p["lots"]:
                    l["div"] += amt * l["shares"] / open_sh
            else:
                p["div_unalloc"] += amt
            p["events"].append({"date": date, "type": "DIVIDEND", "amount": sh, "ccy": ccy,
                                "fx": round(fx, 6), "amount_cad": round(amt, 2)})

    out, problems = {}, []
    for key, b in base.items():
        p = pos.get(key)
        acb_base = b["cost_total"] * (b["book_rate"] or 1) if b["ccy"] == "USD" else b["cost_total"]
        lot_sh = sum(l["shares"] for l in p["lots"]) if p else 0
        if not p or abs(lot_sh - b["shares"]) > 0.01:
            problems.append(f"{key}: base {b['shares']} sh, history {lot_sh:.4f}")
            continue
        lot_cost = sum(l["cost"] for l in p["lots"])
        lot_div = sum(l["div"] for l in p["lots"])
        cs = acb_base / lot_cost if lot_cost else 1.0
        ds = b["dividends"] / lot_div if lot_div else 0.0
        out[key] = {
            "ticker": b["ticker"], "account": b["account"], "ccy": b["ccy"],
            "cost_scale": round(cs, 6),
            "lots": [{"date": l["date"], "bought": l["bought"], "shares": round(l["shares"], 6),
                      "price": l["price"], "ccy": l["ccy"], "fx": l["fx"], "split": l["split"],
                      "cost": round(l["cost"] * cs, 2), "orig_cost": round(l["orig_cost"], 2),
                      "div": round(l["div"] * ds, 2)} for l in p["lots"]],
            "events": p["events"],
            "div_unalloc": round(b["dividends"] - (lot_div * ds), 2),
            "realized_history": round(sum(e.get("realized", 0) for e in p["events"]), 2),
        }
    doc = {"cutoff": CUTOFF, "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
           "method": "average cost; lot costs scaled to the dashboard base ACB", "positions": out}
    (ROOT / "data" / "lots_base.json").write_text(json.dumps(doc, separators=(",", ":")))
    print(f"wrote {len(out)} positions; {len(problems)} not reconciled")
    for msg in problems:
        print("  ✗", msg)
    big = [(k, v["cost_scale"]) for k, v in out.items() if abs(v["cost_scale"] - 1) > 0.03]
    for k, s in big:
        print(f"  ⚠ {k}: lot costs scaled ×{s:.3f} to match the dashboard ACB")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
