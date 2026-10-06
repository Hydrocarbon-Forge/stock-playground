"""Paper-trading engine — reusable momentum strategy core.

AGGRESSIVE high-risk/high-reward (per Gavin): "invest in companies you think
will go up, highest risk-to-reward, buy/sell whenever, maximize profit."

Per run:
  * Score the watchlist on a composite of momentum, relative strength vs a
    reference, and trend strength (distance above a trend SMA).
  * Go long the top N names that are above the trend SMA with non-negative
    momentum.
  * Size by inverse realized-volatility (equal risk contribution) with a
    per-position cap; hold the remainder in cash.
  * Exits (any of): hard stop from avg cost, trend break (price < trend SMA),
    or dropped from the top-N ranking.

The same core drives two portfolios: STOCK_CFG (state.json) and CRYPTO_CFG
(crypto_state.json, see cryptoengine.py). Paper $100,000 each.
"""
import datetime as dt
import json
import os

BASE = os.path.dirname(os.path.abspath(__file__))

# high-beta / high R:R stock candidate pool (SPY = relative-strength ref)
STOCK_WATCHLIST = [
    "NVDA", "TSLA", "MSTR", "PLTR", "COIN",
    "META", "AMD", "SMCI", "CRWD", "AVGO",
    "NFLX", "TSM", "INTC", "MU",
    "RIOT", "MARA", "AAON", "APP",
]


class Cfg:
    def __init__(self, name, watchlist, reference, state_path, strategy_label,
                 top_n=5, stop_pct=0.15, mom_window=20, rel_window=20,
                 trend_window=50, vol_window=20, max_pos_weight=0.35,
                 min_pos_weight=0.05, dust=0.005, start_cash=100_000.0):
        self.name = name
        self.watchlist = watchlist
        self.reference = reference
        self.state_path = state_path
        self.strategy_label = strategy_label
        self.top_n = top_n
        self.stop_pct = stop_pct
        self.mom_window = mom_window
        self.rel_window = rel_window
        self.trend_window = trend_window
        self.vol_window = vol_window
        self.max_pos_weight = max_pos_weight
        self.min_pos_weight = min_pos_weight
        self.dust = dust
        self.start_cash = start_cash


STOCK_CFG = Cfg(
    name="stock", watchlist=STOCK_WATCHLIST, reference="SPY",
    state_path=os.path.join(BASE, "state.json"),
    strategy_label="aggressive-high-rr",
)


# ----------------------------- persistence -----------------------------
def load_state(cfg):
    if os.path.exists(cfg.state_path):
        with open(cfg.state_path) as f:
            return json.load(f)
    return {
        "start_cash": cfg.start_cash,
        "cash": cfg.start_cash,
        "positions": {},          # sym -> {qty, avg_cost, last_price}
        "equity_history": [],     # [ts_iso, equity]
        "trades": [],            # [ts_iso, sym, action, qty, price, reason]
        "created": dt.datetime.now(dt.timezone.utc).isoformat(),
        "strategy": cfg.strategy_label,
    }


def save_state(state, cfg):
    with open(cfg.state_path, "w") as f:
        json.dump(state, f, indent=2)


# ----------------------------- indicators -----------------------------
def _closes(bars):
    return [b[1] for b in bars]


def _roc(closes, n):
    if len(closes) <= n:
        return None
    return closes[-1] / closes[-(n + 1)] - 1.0


def _sma(closes, n):
    if len(closes) < n:
        return None
    return sum(closes[-n:]) / n


def _vol(closes, n):
    if len(closes) < n + 1:
        return None
    rets = [closes[i] / closes[i - 1] - 1
            for i in range(len(closes) - n, len(closes))]
    m = sum(rets) / len(rets)
    var = sum((r - m) ** 2 for r in rets) / len(rets)
    return var ** 0.5


# ----------------------------- scoring -----------------------------
def analyze(quotes, cfg):
    """Per-symbol indicator dict for symbols with enough history."""
    ref_bars = quotes.get(cfg.reference)
    ref_roc = _roc(_closes(ref_bars), cfg.rel_window) if ref_bars else 0.0
    rows = {}
    for sym, bars in quotes.items():
        closes = _closes(bars)
        if len(closes) < max(cfg.trend_window, cfg.vol_window + 1) + 1:
            continue
        last = closes[-1]
        mom = _roc(closes, cfg.mom_window)
        trend = _sma(closes, cfg.trend_window)
        rel = (_roc(closes, cfg.rel_window) - (ref_roc or 0.0)) if mom is not None else None
        vol = _vol(closes, cfg.vol_window)
        trend_strength = (last / trend - 1.0) if trend else None
        rows[sym] = {
            "last": last, "mom": mom, "trend": trend,
            "rel": rel, "vol": vol, "trend_strength": trend_strength,
        }
    return rows


def pick_and_size(rows, cfg):
    """Return {sym: weight} for the top-N long picks, sized by inverse vol.

    Requires trend (last > trend SMA) and non-negative momentum. Weights are
    normalized to sum to 1 across picks, capped per name.
    """
    candidates = []
    for sym in cfg.watchlist:
        r = rows.get(sym)
        if r is None:
            continue
        if r["mom"] is None or r["rel"] is None or r["trend"] is None:
            continue
        if r["last"] > r["trend"] and r["mom"] >= 0:
            candidates.append(sym)
    if not candidates:
        return {}, []

    # composite z-score across the candidate set, per component
    def zmap(key):
        vals = [rows[s].get(key) for s in candidates]
        valid = [v for v in vals if v is not None]
        m = sum(valid) / len(valid) if valid else 0.0
        var = sum((v - m) ** 2 for v in valid) / len(valid) if valid else 0.0
        sd = var ** 0.5
        return {s: ((v - m) / sd if (v is not None and sd) else 0.0)
                for s, v in zip(candidates, vals)}

    zm, zr, zt = zmap("mom"), zmap("rel"), zmap("trend_strength")
    scores = {s: 0.45 * zm[s] + 0.30 * zr[s] + 0.25 * zt[s] for s in candidates}
    ranked = sorted(candidates, key=lambda s: scores[s], reverse=True)
    picks = ranked[:cfg.top_n]

    # inverse-vol sizing (equal risk), guard zero/None vol
    vols = []
    for s in picks:
        v = rows[s].get("vol")
        vols.append(v if (v and v > 1e-6)
                    else (max([rows[s2]["vol"] for s2 in picks if rows[s2]["vol"]] or [0.05]) or 0.05))
    raw = {s: (1.0 / v) for s, v in zip(picks, vols)}
    total = sum(raw.values()) or 1.0
    w = {s: raw[s] / total for s in picks}
    # cap + redistribute
    for _ in range(5):
        over = {s: w[s] - cfg.max_pos_weight for s in picks if w[s] > cfg.max_pos_weight}
        if not over:
            break
        for s in over:
            w[s] = cfg.max_pos_weight
        surplus = sum(over.values())
        under = [s for s in picks if s not in over]
        if under:
            add = surplus / len(under)
            for s in under:
                w[s] = min(w[s] + add, cfg.max_pos_weight)
        else:
            scale = sum(w.values()) / (len(picks) * cfg.max_pos_weight)
            w = {s: w[s] * scale for s in picks}
    # drop below-min picks
    w = {s: wt for s, wt in w.items() if wt >= cfg.min_pos_weight}
    total = sum(w.values())
    if total > 0:
        w = {s: wt / total for s, wt in w.items()}
    return w, [s for s in picks if s in w]


# ----------------------------- trading -----------------------------
def equity(state):
    eq = state["cash"]
    for sym, pos in state["positions"].items():
        eq += pos["qty"] * pos["last_price"]
    return eq


def mark_to_market(state, prices):
    for sym, pos in state["positions"].items():
        if sym in prices:
            pos["last_price"] = prices[sym]


def _sell_all(state, sym, px, reason, actions):
    pos = state["positions"][sym]
    state["cash"] += pos["qty"] * px
    actions.append([sym, "sell", round(pos["qty"], 4), round(px, 2), reason])
    del state["positions"][sym]


def rebalance(state, prices, targets, rows, actions, cfg):
    """Trade toward targets; apply stop / trend-break exits. `actions` is
    mutated; each action = [sym, action, qty, price, reason]."""
    eq = equity(state)

    # 1) exits: stops and trend breaks on current positions
    for sym in list(state["positions"]):
        if sym not in prices:
            continue
        pos = state["positions"][sym]
        px = prices[sym]
        r = rows.get(sym, {})
        stop = pos["avg_cost"] * (1 - cfg.stop_pct)
        if px <= stop:
            _sell_all(state, sym, px, f"stop (-{cfg.stop_pct*100:.0f}%)", actions)
            continue
        trend = r.get("trend")
        if trend is not None and px < trend:
            _sell_all(state, sym, px, "trend break", actions)
            continue

    # 2) sell holdings no longer targeted
    for sym in list(state["positions"]):
        if sym not in targets and sym in prices:
            _sell_all(state, sym, prices[sym], "ranked out", actions)

    # 3) buy / top-up targets (equal weight of current equity)
    target_value = eq / max(len(targets), 1)
    for sym, weight in targets.items():
        if sym not in prices:
            continue
        px = prices[sym]
        pos = state["positions"].get(sym, {"qty": 0.0, "avg_cost": px})
        cur_val = pos["qty"] * px
        delta_val = target_value - cur_val
        if abs(delta_val) < max(100.0, eq * cfg.dust):
            continue
        delta_qty = delta_val / px
        if delta_qty > 0:
            cost = delta_qty * px
            if cost > state["cash"]:
                delta_qty = state["cash"] / px
                cost = delta_qty * px
                if delta_qty <= 0:
                    continue
            state["cash"] -= cost
            new_qty = pos["qty"] + delta_qty
            new_avg = (pos["qty"] * pos["avg_cost"] + delta_qty * px) / new_qty
            state["positions"][sym] = {"qty": round(new_qty, 4),
                                       "avg_cost": round(new_avg, 4),
                                       "last_price": round(px, 4)}
            actions.append([sym, "buy", round(delta_qty, 4), round(px, 2),
                            "conviction entry/top-up"])
        else:
            sell_qty = min(-delta_qty, pos["qty"])
            if sell_qty <= 1e-6:
                continue
            state["cash"] += sell_qty * px
            pos["qty"] = round(pos["qty"] - sell_qty, 4)
            if pos["qty"] <= 1e-6:
                del state["positions"][sym]
            else:
                actions.append([sym, "sell", round(sell_qty, 4), round(px, 2),
                                "reduce to target"])


def run(quotes, prices, cfg):
    """`prices` = {sym: current_price}; `quotes` = {sym: bars}."""
    state = load_state(cfg)
    mark_to_market(state, prices)
    rows = analyze(quotes, cfg)
    targets, picked = pick_and_size(rows, cfg)

    prev = set(state["positions"].keys())
    actions = []
    if prev != set(targets.keys()) or state["positions"] or targets:
        rebalance(state, prices, targets, rows, actions, cfg)

    eq = equity(state)
    state["equity_history"].append([
        dt.datetime.now(dt.timezone.utc).isoformat(), round(eq, 2)])
    if len(state["equity_history"]) > 10000:
        state["equity_history"] = state["equity_history"][-10000:]
    for a in actions:
        state["trades"].append([dt.datetime.now(dt.timezone.utc).isoformat()] + a)
    if len(state["trades"]) > 8000:
        state["trades"] = state["trades"][-8000:]
    save_state(state, cfg)
    return state, actions, eq


def report(state, actions):
    eq = equity(state)
    total_pnl = eq - state["start_cash"]
    total_pct = total_pnl / state["start_cash"] * 100
    hist = state["equity_history"]
    day_start = None
    today = dt.date.today().isoformat()
    for ts, e in hist:
        if ts.startswith(today):
            day_start = e
            break
    day_pnl = (eq - day_start) if day_start else 0.0
    day_pct = (day_pnl / day_start * 100) if day_start else 0.0
    holdings = []
    for sym, pos in state["positions"].items():
        val = pos["qty"] * pos["last_price"]
        holdings.append((sym, pos["qty"], pos["last_price"], val,
                         val / eq * 100 if eq else 0))
    holdings.sort(key=lambda h: h[3], reverse=True)
    return {
        "equity": eq, "total_pnl": total_pnl, "total_pct": total_pct,
        "day_pnl": day_pnl, "day_pct": day_pct, "cash": state["cash"],
        "holdings": holdings, "actions": actions,
        "n_trades": len(state["trades"]),
        "cash_pct": state["cash"] / eq * 100 if eq else 0,
    }


if __name__ == "__main__":
    import data
    quotes = data.fetch_all()
    prices = {}
    for sym in data.UNIVERSE:
        try:
            px, _ = data.current_price(sym)
            if px:
                prices[sym] = px
        except Exception:
            pass
    st, acts, eq = run(quotes, prices, STOCK_CFG)
    r = report(st, acts)
    print(f"equity=${r['equity']:,.2f}  total={r['total_pnl']:+,.2f} ({r['total_pct']:+.2f}%)  "
          f"day={r['day_pnl']:+,.2f} ({r['day_pct']:+.2f}%)  cash={r['cash']:,.2f}")
    print("holdings:", [(s, round(q, 2), round(p, 2)) for s, q, p, v, wp in r["holdings"]])
    print("actions:", r["actions"])
