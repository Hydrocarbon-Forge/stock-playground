# Stock Playground

Paper-trading playground (Gavin's request, 2026-10-05): a bot that trades a
virtual $100,000 portfolio against **live market prices** and posts P&L to
Gavin's Discord thread every hour. No real money, no brokerage identity
required — prices are real (Yahoo Finance daily bars + intraday when the
market is open), decisions are real, P&L is real.

## Why not a paper brokerage account
Real brokerages (Alpaca/Tradier) require a personal identity (SSN, DOB,
address) + email verification at signup — C6 can't complete that for a
human. So the playground is self-hosted: same gameplay, zero identity.

## Layout
- `data.py` — Yahoo v8 chart endpoint (daily bars, cached 10 min, rate-gated
  parallel fetch). `current_price()` = freshest 5m bar if the market is
  active, else latest daily close.
- `engine.py` — the reusable strategy core (see below). `STOCK_CFG` owns
  `state.json`; `CRYPTO_CFG` (in `cryptoengine.py`) owns
  `crypto_state.json`.
- `cryptodata.py` — Yahoo v8 crypto prices (`BTC-USD`, `ETH-USD`, ... 24/7).
- `cryptoengine.py` — `CRYPTO_CFG`: same momentum strategy tuned for crypto
  (BTC-relative, 20-bar trend, -20% stop, top 4 of 9, up to 40% per coin).
- `run_hourly.py` — trades BOTH portfolios, posts a combined P&L update to
  thread `1556689793750925363`, and publishes both to the forge-office.
- `publish_office.py` — snapshots both portfolios to
  `~/HydrocarbonForge/Projects/forge-office/data/trading.json`, which the
  office's Trading Floor screens poll at `/data/trading.json` (the office
  polls it every 60 s).
- `~/.hermes/scripts/stock_playground_hourly.sh` — cron wrapper (silent on
  success, one-line failure alert).
- cron job `stock-playground-hourly` (`5e8939f591a3`), every 60 min,
  no_agent, failure → thread.

## Strategy (aggressive, high risk/reward — per Gavin)
Candidate pool = high-beta names (NVDA, TSLA, MSTR, PLTR, COIN, META, AMD,
SMCI, CRWD, AVGO, NFLX, TSM, INTC, MU, RIOT, MARA, AAON, APP).
Each hourly tick:
1. Score candidates on 20-day momentum (45%), 20-day relative strength vs SPY
   (30%), trend strength above 50-SMA (25%) — z-scored across the set.
2. Go long the top 5 that are above their 50-SMA and have non-negative
   momentum. Size by inverse realized-vol (equal risk contribution), capped
   at 35% per name.
3. Exits, any of: hard stop -15% from avg cost, trend break (price < 50-SMA),
   or ranked out of the top 5.
4. Rebalance toward target weights; dust moves (<0.5% of equity) ignored.

Honest framing: this is a momentum/conviction strategy on a 21-symbol pool,
rebalanced hourly. It will lose in choppy markets and win in trends. It is a
playground, not advice.

## State
- `state.json` — cash, positions, equity history, trade log (reason per trade).
- `log.txt` — runner log (each tick: equity, P&L, http status).
- To reset: delete `state.json` (next run starts fresh $100k).

## Manual run
    python3 run_hourly.py     # trade + post to the thread
    python3 engine.py         # trade only (no post)
