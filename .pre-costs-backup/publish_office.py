#!/usr/bin/env python3
"""Publish both playground portfolios to the forge-office data folder.

Writes forge-office/data/trading.json, which the office polls at
/data/trading.json for its Trading Floor screens. This is the bridge
between the hourly trading job and the office screens — the screens
update whenever the hourly job (or a manual run) re-publishes.

The office also shows live 24h deltas from its own spot checks, but the
canonical portfolio state comes from here.
"""
import datetime as dt
import json
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

import data        # noqa: E402
import engine      # noqa: E402
import cryptodata  # noqa: E402
import cryptoengine  # noqa: E402

OFFICE_DATA_DIR = os.path.expanduser(
    "~/HydrocarbonForge/Projects/forge-office/data")
OUT = os.path.join(OFFICE_DATA_DIR, "trading.json")


def snapshot_stock():
    """Run the stock engine and return a screen-ready dict."""
    quotes = data.fetch_all()
    prices = {}
    for sym in quotes:
        try:
            px, _ = data.current_price(sym)
            if px:
                prices[sym] = px
        except Exception:
            pass
    if not prices:
        prices = {s: b[-1][1] for s, b in quotes.items()}
    st, acts, eq = engine.run(quotes, prices, engine.STOCK_CFG)
    r = engine.report(st, acts)
    return {
        "label": "STOCKS",
        "subtitle": "aggressive high R:R",
        "equity": round(r["equity"], 2),
        "start": r["equity"] - r["total_pnl"],
        "total_pnl": round(r["total_pnl"], 2),
        "total_pct": round(r["total_pct"], 2),
        "day_pnl": round(r["day_pnl"], 2),
        "day_pct": round(r["day_pct"], 2),
        "cash": round(r["cash"], 2),
        "cash_pct": round(r["cash_pct"], 1),
        "holdings": [
            {"sym": sym, "qty": round(q, 2), "px": round(px, 2),
             "val": round(val, 2), "w": round(wp, 1)}
            for sym, q, px, val, wp in r["holdings"][:8]
        ],
        "trades_now": [
            {"sym": s, "act": a, "qty": round(q, 2), "px": round(p, 2),
             "reason": reason}
            for s, a, q, p, reason in acts[:6]
        ],
        "n_trades": r["n_trades"],
        "quotes_ok": len(quotes),
        "quotes_total": len(data.UNIVERSE),
    }


def snapshot_crypto():
    """Run the crypto engine and return a screen-ready dict."""
    quotes = cryptodata.fetch_all()
    prices = {s: b[-1][1] for s, b in quotes.items()}
    st, acts, eq = engine.run(quotes, prices, cryptoengine.CRYPTO_CFG)
    r = engine.report(st, acts)
    return {
        "label": "CRYPTO",
        "subtitle": "aggressive high R:R, 24/7",
        "equity": round(r["equity"], 2),
        "start": r["equity"] - r["total_pnl"],
        "total_pnl": round(r["total_pnl"], 2),
        "total_pct": round(r["total_pct"], 2),
        "day_pnl": round(r["day_pnl"], 2),
        "day_pct": round(r["day_pct"], 2),
        "cash": round(r["cash"], 2),
        "cash_pct": round(r["cash_pct"], 1),
        "holdings": [
            {"sym": cryptodata.clean(sym), "qty": round(q, 4),
             "px": round(px, 2), "val": round(val, 2), "w": round(wp, 1)}
            for sym, q, px, val, wp in r["holdings"][:8]
        ],
        "trades_now": [
            {"sym": cryptodata.clean(s), "act": a, "qty": round(q, 4),
             "px": round(p, 2), "reason": reason}
            for s, a, q, p, reason in acts[:6]
        ],
        "n_trades": r["n_trades"],
        "quotes_ok": len(quotes),
        "quotes_total": len(cryptodata.CRYPTO),
    }


def publish(force=True):
    os.makedirs(OFFICE_DATA_DIR, exist_ok=True)
    now = dt.datetime.now(dt.timezone.utc)
    payload = {
        "updated": now.isoformat(),
        "updated_ts": now.timestamp(),
        "stock": None,
        "crypto": None,
        "errors": [],
    }
    try:
        payload["stock"] = snapshot_stock()
    except Exception as e:
        payload["errors"].append(f"stock: {type(e).__name__}: {e}")
    try:
        payload["crypto"] = snapshot_crypto()
    except Exception as e:
        payload["errors"].append(f"crypto: {type(e).__name__}: {e}")
    with open(OUT, "w") as f:
        json.dump(payload, f, indent=2)
    return payload


if __name__ == "__main__":
    p = publish()
    print(f"published -> {OUT}  at {p['updated']}")
    for k in ("stock", "crypto"):
        if p[k]:
            print(f"  {k}: equity=${p[k]['equity']:,.0f} "
                  f"total={p[k]['total_pct']:+.2f}% day={p[k]['day_pct']:+.2f}% "
                  f"holdings={len(p[k]['holdings'])} quotes={p[k]['quotes_ok']}/{p[k]['quotes_total']}")
        else:
            print(f"  {k}: FAILED ({p['errors'][-1] if p['errors'] else '?'})")
