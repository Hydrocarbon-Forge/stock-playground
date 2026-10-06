#!/usr/bin/env python3
"""Hourly playground runner (stock + crypto).

Run by cron (no_agent). Each tick:
  1. Trades the STOCK portfolio (Yahoo daily + intraday).
  2. Trades the CRYPTO portfolio (Yahoo crypto, 24/7).
  3. Publishes both to forge-office/data/trading.json (the office screens).
  4. Posts a combined P&L to Gavin's Discord thread.

Silent on success; loud on failure.
Env: DISCORD_BOT_TOKEN must be set (runner sources ~/.hermes/.env if not).
"""
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import data  # noqa: E402
import engine  # noqa: E402
import cryptodata  # noqa: E402
import cryptoengine  # noqa: E402
import publish_office  # noqa: E402

THREAD_ID = "1556689793750925363"  # Gavin's trading thread
LOG = os.path.join(BASE, "log.txt")


def get_token():
    tok = os.environ.get("DISCORD_BOT_TOKEN")
    if tok:
        return tok.strip().strip('"').strip("'")
    env_path = os.path.expanduser("~/.hermes/.env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            m = re.search(r"DISCORD_BOT_TOKEN\s*=\s*(\S+)", f.read())
            if m:
                return m.group(1).strip().strip('"').strip("'")
    raise SystemExit("DISCORD_BOT_TOKEN not found")


def discord_post(content, token):
    url = f"https://discord.com/api/v10/channels/{THREAD_ID}/messages"
    body = json.dumps({"content": content}).encode()
    req = urllib.request.Request(url, data=body, headers={
        "Authorization": f"Bot {token}",
        "User-Agent": "HydrocarbonForge-Bot (discord, 1.0)",
        "Content-Type": "application/json",
    })
    for i in range(4):
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status
        except urllib.error.HTTPError as e:
            if e.code == 429:
                try:
                    ra = json.load(e).get("retry_after", 2.0)
                except Exception:
                    ra = 2.0
                time.sleep(ra + 0.5)
            else:
                raise
    raise SystemExit("discord post failed after retries")


def post_fail(reason, token):
    try:
        discord_post(f"playground update failed: {reason}", token)
    except Exception:
        pass


def log(msg):
    line = f"[{dt.datetime.now(dt.timezone.utc).isoformat()}] {msg}"
    with open(LOG, "a") as f:
        f.write(line + "\n")


def fmt_money(x):
    sign = "+" if x >= 0 else "-"
    return f"{sign}${abs(x):,.0f}"


def build_section(name, r, clean=None):
    """One portfolio block for the Discord message."""
    clean = clean or (lambda s: s)
    lines = [
        f"**{name}**  equity **${r['equity']:,.0f}**  "
        f"{fmt_money(r['total_pnl'])} ({r['total_pct']:+.2f}%)  "
        f"today {fmt_money(r['day_pnl'])} ({r['day_pct']:+.2f}%)",
    ]
    if r["holdings"]:
        hh = ", ".join(
            f"{clean(sym)} {wp:.0f}%" for sym, q, px, val, wp in r["holdings"][:6])
        lines.append(f"  holding: {hh}")
    else:
        lines.append("  holding: all cash")
    if r["actions"]:
        lines.append("  trades: " + ", ".join(
            f"{a[1].upper()} {clean(a[0])}" for a in r["actions"][:6]))
    return "\n".join(lines)


def main():
    token = get_token()

    # ---- STOCK portfolio ----
    stock_err = None
    r_stock = None
    try:
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
        r_stock = engine.report(st, acts)
    except Exception as e:
        stock_err = f"{type(e).__name__}: {e}"
        log(f"stock run failed: {stock_err}")

    # ---- CRYPTO portfolio ----
    crypto_err = None
    r_crypto = None
    try:
        quotes = cryptodata.fetch_all()
        prices = {s: b[-1][1] for s, b in quotes.items()}
        st, acts, eq = engine.run(quotes, prices, cryptoengine.CRYPTO_CFG)
        r_crypto = engine.report(st, acts)
    except Exception as e:
        crypto_err = f"{type(e).__name__}: {e}"
        log(f"crypto run failed: {crypto_err}")

    if stock_err and crypto_err:
        post_fail(f"both portfolios failed (stock={stock_err} | crypto={crypto_err})", token)
        return

    # ---- Publish to the office (screens update here) ----
    try:
        pub = publish_office.publish()
        log(f"office published: stock={'ok' if pub['stock'] else 'FAIL'} "
            f"crypto={'ok' if pub['crypto'] else 'FAIL'}")
    except Exception as e:
        log(f"office publish failed (non-fatal): {e}")

    # ---- Post combined P&L ----
    now = dt.datetime.now(dt.timezone.utc)
    head = f"**playground — {now:%Y-%m-%d %H:%M} UTC** (aggressive high R:R, stock + crypto)"
    parts = [head]
    if r_stock:
        parts.append(build_section("STOCKS", r_stock))
    else:
        parts.append(f"**STOCKS** — update failed ({stock_err})")
    if r_crypto:
        parts.append(build_section("CRYPTO", r_crypto, clean=lambda s: s))
    else:
        parts.append(f"**CRYPTO** — update failed ({crypto_err})")
    msg = "\n\n".join(parts)
    status = discord_post(msg, token)
    log(f"posted combined update (http {status})")


if __name__ == "__main__":
    t0 = time.time()
    main()
    print(f"done in {time.time() - t0:.1f}s")
