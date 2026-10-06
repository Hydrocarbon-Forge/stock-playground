"""Crypto price data for the crypto portfolio.

Source: Yahoo Finance v8 chart endpoint with -USD suffixed tickers
(BTC-USD, ETH-USD, ...). 24/7 market, no session gating. Cached 10 min,
rate-gated parallel fetch — same mechanics as data.py.
"""
import datetime as dt
import json
import os
import threading
import time
import urllib.error
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(BASE, "cache_crypto")
os.makedirs(CACHE_DIR, exist_ok=True)

# High-risk/high-reward crypto pool (per Gavin: maximize R:R)
CRYPTO = [
    "BTC-USD", "ETH-USD", "SOL-USD",
    "DOGE-USD", "AVAX-USD", "LINK-USD",
    "PEPE-USD", "WIF-USD", "SUI-USD",
]

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

_gate_lock = threading.Lock()
_last = [0.0]
MIN_GAP = 1.4
CACHE_TTL = 600


def _throttle():
    with _gate_lock:
        wait = _last[0] + MIN_GAP - time.time()
        if wait > 0:
            time.sleep(wait)
        _last[0] = time.time()


def _get_json(url, tries=4, base_delay=6):
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


def fetch_bars(symbol, use_cache=True, max_age=CACHE_TTL):
    """[(date_iso, close)] recent range for a crypto ticker."""
    if use_cache:
        p = _cache_path(symbol)
        if os.path.exists(p):
            if time.time() - os.path.getmtime(p) < max_age:
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


def _fetch_one(symbol):
    try:
        return symbol, fetch_bars(symbol)
    except Exception:
        return symbol, None


def fetch_all(max_age=CACHE_TTL):
    """{symbol: [[date, close], ...]} for the crypto pool."""
    import concurrent.futures
    out = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        futs = {ex.submit(_fetch_one, s): s for s in CRYPTO}
        for fut in concurrent.futures.as_completed(futs):
            sym, bars = fut.result()
            if bars and len(bars) >= 30:
                out[sym] = bars
    return out


def clean(sym):
    """BTC-USD -> BTC (display name)."""
    return sym.replace("-USD", "")


if __name__ == "__main__":
    t0 = time.time()
    q = fetch_all()
    for s, bars in sorted(q.items()):
        print(f"{clean(s):6s} last={bars[-1]}")
    print(f"ok: {len(q)}/{len(CRYPTO)}  ({time.time() - t0:.1f}s)")
