"""Crypto portfolio engine — reuses the momentum core with crypto params.

Same strategy as the stock portfolio (aggressive momentum + trend + stop)
but tuned for crypto:
  * Reference = BTC-USD (relative strength vs the king).
  * Trend window 20 bars (crypto trends move faster than the 50-bar stock
    one), stop -20% (crypto is 2-3x vol).
  * Top 4 of the pool, up to 40% per coin.
  * Trades 24/7 (no session gating).
State: crypto_state.json. Paper $100,000.
"""
import os
import engine
import cryptodata

_BASE = os.path.dirname(os.path.abspath(__file__))

# Watchlist uses the same "-USD" keys the quotes carry (BTC-USD, ETH-USD...).
# Display names (BTC, ETH) are derived at publish time via cryptodata.clean.
CRYPTO_CFG = engine.Cfg(
    name="crypto",
    watchlist=list(cryptodata.CRYPTO),
    reference="BTC-USD",
    state_path=os.path.join(_BASE, "crypto_state.json"),
    strategy_label="aggressive-high-rr-crypto",
    top_n=4,
    stop_pct=0.20,
    mom_window=20,
    rel_window=20,
    trend_window=20,
    vol_window=20,
    max_pos_weight=0.40,
    min_pos_weight=0.05,
    dust=0.005,
    start_cash=100_000.0,
)


if __name__ == "__main__":
    quotes = cryptodata.fetch_all()
    prices = {}
    for sym in quotes:
        prices[sym] = quotes[sym][-1][1]
    st, acts, eq = engine.run(quotes, prices, CRYPTO_CFG)
    r = engine.report(st, acts)
    print(f"CRYPTO equity=${r['equity']:,.2f}  total={r['total_pnl']:+,.2f} ({r['total_pct']:+.2f}%)  "
          f"day={r['day_pnl']:+,.2f} ({r['day_pct']:+.2f}%)  cash={r['cash']:,.2f}")
    print("holdings:", [(cryptodata.clean(s), round(q, 4), round(p, 2)) for s, q, p, v, wp in r["holdings"]])
    print("actions:", r["actions"])
