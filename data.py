"""Price data for the stock playground.

Source: Yahoo Finance v8 chart endpoint (daily bars), no auth needed.
The v7 quote endpoint is auth-gated, so "current price" = latest bar close
(last completed session, or today's bar while the market is open).

Every fetch is cached to data/cache/<SYMBOL>.json (TTL 10 min) so the
hourly job stays cheap even if Yahoo gets grumpy, and parallel fetches
share a rate gate to avoid 429 storms.
"""
import datetime as dt
import json
import os
import threading
import time
import urllib.error
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(BASE, "cache")
os.makedirs(CACHE_DIR, exist_ok=True)

# High-beta / high R:R names (plus market refs SPY+QQQ; SPY is used for
# relative strength, QQQ is context only — candidates come from WATCHLIST).
UNIVERSE = [
    "SPY", "QQQ",
    "NVDA", "TSLA", "MSTR", "PLTR", "COIN",
    "META", "AMD", "SMCI", "CRWD", "AVGO", "NFLX",
    "TSM", "INTC", "MU", "RIOT", "MARA", "AAON", "APP",
]

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

_gate_lock = threading.Lock()
_last_fetch = [0.0]
MIN_GAP = 1.2  # seconds between any two Yahoo hits

CACHE_TTL = 600  # 10 minutes


def _throttle():
    with _gate_lock:
        now = time.time()
        wait = _last_fetch[0] + MIN_GAP - now
        if wait > 0:
            time.sleep(wait)
        _last_fetch[0] = time.time()


def _get_json(url, tries=4, base_delay=5):
    last = None
    for i in range(tries):
        _throttle()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            last = e
            if e.code == 429:
                # Yahoo rate limit: back off hard, honor retry-after if present
                ra = e.headers.get("Retry-After")
                pause = float(ra) if ra and ra.isdigit() else base_delay * (i + 1) ** 2
                time.sleep(min(pause, 90))
            else:
                time.sleep(base_delay * (i + 1))
        except (urllib.error.URLError, ValueError, TimeoutError) as e:
            last = e
            time.sleep(base_delay * (i + 1))
    raise RuntimeError(f"get failed after {tries} tries: {last}")


def _cache_path(symbol):
    return os.path.join(CACHE_DIR, f"{symbol}.json")


def fetch_bars(symbol, days=90, use_cache=True, max_age=CACHE_TTL):
    """[(date, close)] for the recent range; None-quotes dropped."""
    if use_cache:
        p = _cache_path(symbol)
        if os.path.exists(p):
            age = time.time() - os.path.getmtime(p)
            if age < max_age:
                with open(p) as f:
                    return json.load(f)

    d = _get_json("https://query1.finance.yahoo.com/v8/finance/chart/"
                  f"{symbol}?range=3mo&interval=1d")
    res = d["chart"]["result"][0]
    ts = res["timestamp"]
    closes = res["indicators"]["quote"][0]["close"]
    out = []
    for t, c in zip(ts, closes):
        if c is None:
            continue
        out.append([
            dt.datetime.fromtimestamp(t, dt.timezone.utc).date().isoformat(),
            round(c, 4),
        ])
    if out:
        with open(_cache_path(symbol), "w") as f:
            json.dump(out, f)
    return out


def current_price(symbol, max_age_minutes=25):
    """Best current price: freshest 5m bar if the market is active,
    else the latest daily close. Returns (price, source)."""
    try:
        d = _get_json("https://query1.finance.yahoo.com/v8/finance/chart/"
                      f"{symbol}?range=1d&interval=5m")
        res = d["chart"]["result"][0]
        ts = res["timestamp"]
        closes = res["indicators"]["quote"][0]["close"]
        for t, c in zip(reversed(ts), reversed(closes)):
            if c is None:
                continue
            if time.time() - t < max_age_minutes * 60:
                return round(c, 4), "intraday"
    except Exception:
        pass
    bars = fetch_bars(symbol, use_cache=True)
    if bars:
        return bars[-1][1], "daily"
    return None, None


def _fetch_one(symbol):
    try:
        return symbol, fetch_bars(symbol)
    except Exception as e:
        return symbol, None


def fetch_all(max_age=CACHE_TTL):
    """{symbol: [[date, close], ...]} for the universe.

    Parallel workers (4) under a shared rate gate; a symbol that fails
    is dropped (the strategy trades whatever is available).
    """
    import concurrent.futures
    out = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        futs = {ex.submit(_fetch_one, s): s for s in UNIVERSE}
        for fut in concurrent.futures.as_completed(futs):
            sym, bars = fut.result()
            if bars and len(bars) >= 30:
                out[sym] = bars
    return out


if __name__ == "__main__":
    t0 = time.time()
    q = fetch_all()
    for s, bars in sorted(q.items()):
        print(f"{s:6s} last={bars[-1]} prev={bars[-2]}")
    print(f"ok: {len(q)}/{len(UNIVERSE)}  ({time.time() - t0:.1f}s)")
