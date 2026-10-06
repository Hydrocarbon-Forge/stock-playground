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
                 min_pos_weight=0.05, dust=0.005, start_cash=100_000.0,
                 fee_rate=0.001, slippage=0.001):
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
        self.fee_rate = fee_rate
        self.slippage = slippage


STOCK_CFG = Cfg(
    name="stock", watchlist=STOCK_WATCHLIST, reference="SPY",
    state_path=os.path.join(BASE, "state.json"),
    strategy_label="aggressive-high-rr",
)


# ----------------------------- persistence -----------------------------
def load_state(cfg):
    if os.path.exists(cfg.state_path):
        with open(cfg.state_path) as f:
            st = json.load(f)
            st.setdefault("total_fees", 0.0)
            st.setdefault("last_decision", None)
            return st
    return {
        "start_cash": cfg.start_cash,
        "cash": cfg.start_cash,
        "positions": {},          # sym -> {qty, avg_cost, last_price}
        "equity_history": [],     # [ts_iso, equity]
        "trades": [],            # [ts_iso, sym, action, qty, price, reason, fee]
        "total_fees": 0.0,
        "last_decision": None,
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


def score_candidates(rows, cfg):
    """Return (scores, ranked) for watchlist symbols above trend SMA with non-negative momentum."""
    candidates = []
    for sym in cfg.watchlist:
        r = rows.get(sym)
        if r is None:
            continue
        if r.get("mom") is None or r.get("rel") is None or r.get("trend") is None:
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
    return scores, ranked


def pick_and_size(rows, cfg):
    """Return {sym: weight} for the top-N long picks, sized by inverse vol.

    Requires trend (last > trend SMA) and non-negative momentum. Weights are
    normalized to sum to 1 across picks, capped per name.
    """
    scores, ranked = score_candidates(rows, cfg)
    if not ranked:
        return {}, []
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


def _sell_all(state, sym, px, reason, actions, cfg):
    pos = state["positions"][sym]
    fill_px = px * (1.0 - cfg.slippage)
    notional = pos["qty"] * fill_px
    fee = notional * cfg.fee_rate
    state["cash"] += (notional - fee)
    state["total_fees"] += fee
    actions.append([sym, "sell", round(pos["qty"], 4),
                    round(fill_px, 4 if fill_px < 1.0 else 2),
                    reason, round(fee, 4 if fee < 1.0 else 2)])
    del state["positions"][sym]


def rebalance(state, prices, targets, rows, actions, cfg):
    """Trade toward targets; apply stop / trend-break exits. `actions` is
    mutated; each action = [sym, action, qty, price, reason, fee]."""
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
            _sell_all(state, sym, px, f"stop (-{cfg.stop_pct*100:.0f}%)", actions, cfg)
            continue
        trend = r.get("trend")
        if trend is not None and px < trend:
            _sell_all(state, sym, px, "trend break", actions, cfg)
            continue

    # 2) sell holdings no longer targeted (only when targets was generated — guard empty candidates)
    if targets:
        for sym in list(state["positions"]):
            if sym not in targets and sym in prices:
                _sell_all(state, sym, prices[sym], "ranked out", actions, cfg)

    # 3) buy / top-up targets (equal weight of current equity)
    if targets:
        target_value = eq / max(len(targets), 1)
        for sym, weight in targets.items():
            if sym not in prices:
                continue
            px = prices[sym]
            pos = state["positions"].get(sym, {"qty": 0.0, "avg_cost": px, "last_price": px})
            cur_val = pos["qty"] * px
            delta_val = target_value - cur_val
            if abs(delta_val) < max(100.0, eq * cfg.dust):
                continue
            if delta_val > 0:
                fill_px = px * (1.0 + cfg.slippage)
                total_unit_cost = fill_px * (1.0 + cfg.fee_rate)
                delta_qty = delta_val / total_unit_cost
                cost = delta_qty * total_unit_cost
                if cost > state["cash"]:
                    delta_qty = state["cash"] / total_unit_cost
                    cost = delta_qty * total_unit_cost
                    if delta_qty <= 1e-6:
                        continue
                notional = delta_qty * fill_px
                fee = notional * cfg.fee_rate
                state["cash"] -= (notional + fee)
                state["total_fees"] += fee
                new_qty = pos["qty"] + delta_qty
                new_avg = (pos["qty"] * pos["avg_cost"] + delta_qty * fill_px) / new_qty
                state["positions"][sym] = {"qty": round(new_qty, 4),
                                           "avg_cost": round(new_avg, 4),
                                           "last_price": round(px, 4)}
                actions.append([sym, "buy", round(delta_qty, 4),
                                round(fill_px, 4 if fill_px < 1.0 else 2),
                                "conviction entry/top-up", round(fee, 4 if fee < 1.0 else 2)])
            else:
                sell_qty = min(-delta_val / px, pos["qty"])
                if sell_qty <= 1e-6:
                    continue
                fill_px = px * (1.0 - cfg.slippage)
                notional = sell_qty * fill_px
                fee = notional * cfg.fee_rate
                proceeds = notional - fee
                state["cash"] += proceeds
                state["total_fees"] += fee
                pos["qty"] = round(pos["qty"] - sell_qty, 4)
                pos["last_price"] = round(px, 4)
                actions.append([sym, "sell", round(sell_qty, 4),
                                round(fill_px, 4 if fill_px < 1.0 else 2),
                                "reduce to target", round(fee, 4 if fee < 1.0 else 2)])
                if pos["qty"] <= 1e-6:
                    del state["positions"][sym]


def force_trade(state, prices, rows, scores, ranked, actions, cfg):
    """Force one meaningful trade (~2.5% of equity) if no trades ran this cycle.

    - Prefer a fresh entry into the strongest-scoring candidate not already held.
    - If already fully invested in strong names (or no candidates), trim ~25% of the
      weakest held name.
    - If neither is possible (no prices / no candidates / no positions), no-op gracefully.
    """
    if not prices:
        return
    eq = equity(state)
    target_trade_val = eq * 0.025  # ~2.5% of equity

    # Strongest candidate not already held
    non_held = [s for s in ranked if s not in state["positions"] and s in prices and prices[s] > 0]
    min_cash_required = max(50.0, target_trade_val * 0.4)

    # 1. Fresh entry if we have candidate and sufficient cash
    if non_held and state["cash"] >= min_cash_required:
        sym = non_held[0]
        px = prices[sym]
        fill_px = px * (1.0 + cfg.slippage)
        total_unit_cost = fill_px * (1.0 + cfg.fee_rate)
        buy_dollars = min(target_trade_val, state["cash"])
        delta_qty = buy_dollars / total_unit_cost
        if delta_qty * total_unit_cost > state["cash"]:
            delta_qty = state["cash"] / total_unit_cost
        delta_qty = round(delta_qty, 4)
        if delta_qty > 0:
            notional = delta_qty * fill_px
            fee = notional * cfg.fee_rate
            state["cash"] -= (notional + fee)
            state["total_fees"] += fee
            pos = state["positions"].get(sym)
            if pos:
                new_qty = pos["qty"] + delta_qty
                new_avg = (pos["qty"] * pos["avg_cost"] + delta_qty * fill_px) / new_qty
                pos["qty"] = round(new_qty, 4)
                pos["avg_cost"] = round(new_avg, 4)
                pos["last_price"] = round(px, 4)
            else:
                state["positions"][sym] = {
                    "qty": delta_qty,
                    "avg_cost": round(fill_px, 4),
                    "last_price": round(px, 4),
                }
            actions.append([sym, "buy", delta_qty,
                            round(fill_px, 4 if fill_px < 1.0 else 2),
                            "forced entry (strongest)", round(fee, 4 if fee < 1.0 else 2)])
            return

    # 2. Book fully invested: trim ~25% of the weakest held name
    held = [s for s in state["positions"] if s in prices and state["positions"][s]["qty"] > 1e-6]
    if not held:
        return

    def _held_score(s):
        if s in scores:
            return scores[s]
        r = rows.get(s, {})
        mom = r.get("mom") or -1.0
        rel = r.get("rel") or -1.0
        ts = r.get("trend_strength") or -1.0
        return -10.0 + (0.45 * mom + 0.30 * rel + 0.25 * ts)

    weakest = min(held, key=_held_score)
    pos = state["positions"][weakest]
    px = prices[weakest]
    fill_px = px * (1.0 - cfg.slippage)
    trim_qty = round(pos["qty"] * 0.25, 4)
    if trim_qty <= 1e-6:
        trim_qty = pos["qty"]
    notional = trim_qty * fill_px
    fee = notional * cfg.fee_rate
    proceeds = notional - fee
    state["cash"] += proceeds
    state["total_fees"] += fee
    pos["qty"] = round(pos["qty"] - trim_qty, 4)
    pos["last_price"] = round(px, 4)
    actions.append([weakest, "sell", round(trim_qty, 4),
                    round(fill_px, 4 if fill_px < 1.0 else 2),
                    "forced trim (weakest)", round(fee, 4 if fee < 1.0 else 2)])
    if pos["qty"] <= 1e-6:
        del state["positions"][weakest]


def run(quotes, prices, cfg):
    """`prices` = {sym: current_price}; `quotes` = {sym: bars}."""
    state = load_state(cfg)
    mark_to_market(state, prices)
    rows = analyze(quotes, cfg)
    scores, ranked = score_candidates(rows, cfg)
    targets, picked = pick_and_size(rows, cfg)

    prev = set(state["positions"].keys())
    actions = []
    if prev != set(targets.keys()) or state["positions"] or targets:
        rebalance(state, prices, targets, rows, actions, cfg)

    # Force trade if rebalance was a no-op
    if not actions:
        force_trade(state, prices, rows, scores, ranked, actions, cfg)

    if actions:
        state["last_decision"] = dt.datetime.now(dt.timezone.utc).isoformat()

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
    start_cash = state.get("start_cash", 100_000.0)
    total_pnl = eq - start_cash
    total_pct = (total_pnl / start_cash * 100) if start_cash else 0.0
    hist = state.get("equity_history", [])
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
    total_fees = state.get("total_fees", 0.0)
    fees_pct = (total_fees / start_cash * 100) if start_cash else 0.0
    cycle_fees = sum(a[5] for a in actions if len(a) > 5)
    return {
        "equity": eq, "total_pnl": total_pnl, "total_pct": total_pct,
        "day_pnl": day_pnl, "day_pct": day_pct, "cash": state["cash"],
        "holdings": holdings, "actions": actions,
        "n_trades": len(state.get("trades", [])),
        "cash_pct": (state["cash"] / eq * 100) if eq else 0,
        "total_fees": round(total_fees, 2),
        "last_decision": state.get("last_decision"),
        "fees_pct_of_start": round(fees_pct, 4),
        "cycle_fees": round(cycle_fees, 2),
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
