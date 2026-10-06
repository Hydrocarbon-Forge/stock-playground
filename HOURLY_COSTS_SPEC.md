# TASK: Hourly decision + forced trade + real execution costs (stock + crypto)

Project: `stock-playground` (Gavin's paper-trading playground). Both portfolios
share `engine.py`. This task makes the bot:
1. **Decide** what to buy/sell every hour (it already does — the momentum
   rebalance is the decision).
2. **Always make at least one real trade** each hour (even when the natural
   rebalance is a no-op).
3. **Calculate and account for real costs** — commission + slippage on every
   fill — so equity/P&L reflect what you'd actually pay.

## Files
- `engine.py` — the shared strategy core. `Cfg`, `load_state`, `report`,
  `run(quotes, prices, cfg)`, `rebalance(...)`, `_sell_all`, `equity`,
  `mark_to_market`, `analyze`, `pick_and_size`.
- `run_hourly.py` — trades both portfolios, posts combined P&L to the Discord
  thread, publishes the office.
- `publish_office.py` — writes `forge-office/data/trading.json`.
- State files: `state.json` (stock) and `crypto_state.json` (crypto). **Do
  not delete them** — they hold live positions.

## What to implement (all of it)

### A. Real execution costs on every fill (engine.py)
- Add to `Cfg` (with sensible defaults): `fee_rate=0.001` (0.10% commission
  per fill) and `slippage=0.001` (0.10% adverse price move per fill).
- Every **buy** fills at `price * (1 + slippage)` and pays `fee = notional *
  fee_rate` cash. Every **sell** fills at `price * (1 - slippage)` and pays
  `fee = notional * fee_rate` cash (deducted from sale proceeds).
- Accumulate `state["total_fees"] += fee` on every fill.
- Keep equity consistent: `equity() = cash + sum(qty * mark)`. Because buys
  cost more (slippage+fee) and sells pay less, equity realistically erodes —
  that's the point. After a run, the invariant
  `|cash + sum(qty*last_price) - equity|` must stay ~0.

### B. Always trade each hour (engine.py)
- `run()` is called once per hour (cron). Treat each `run()` as a **decision
  cycle**. After the natural `rebalance` runs, if **no trade** was executed
  this cycle, **force one meaningful trade** (size ~2.5% of equity):
  - Prefer a fresh entry / top-up into the **strongest-scoring candidate that
    is not already held** (score = momentum + relative strength, reuse the
    same scoring idea as `pick_and_size`).
  - If the book is already fully invested in strong names, instead **trim ~25%
    of the weakest held name**.
  - If neither is possible (no prices / no candidates), no-op gracefully.
- Record `state["last_decision"] = now.isoformat()` when a cycle executes a
  trade (natural or forced). This is for transparency/auditing, not a hard
  gate — a cron tick is ~1h so each hourly tick should normally trade.
- Backward compatible: `load_state` must `setdefault("total_fees", 0.0)` and
  `setdefault("last_decision", None)` for existing state files. **Do not drop
  existing fields.**

### C. Surface costs + decision (report + posts + office)
- `report()` gains: `total_fees` (rounded), `last_decision`, and
  `fees_pct_of_start = total_fees / start_cash * 100`.
- `run_hourly.py`: the Discord post should now also show, per portfolio, the
  **costs paid this hour** (sum of `fee` across this cycle's actions) and the
  **cumulative total fees** from `report()`. Keep the post readable.
- `publish_office.py`: include `total_fees` in each portfolio's published
  dict so the office screens can show cumulative cost (the office JS can ignore
  it if not drawn, but the data should be present).

## Constraints
- Do NOT restart any running service. The hourly cron already calls
  `run_hourly.py`; it will pick up the new engine automatically.
- Do NOT delete or reset `state.json` / `crypto_state.json`.
- Keep it dependency-free (stdlib only).
- Match existing code style.
- Guard against the empty-candidates case (market data partial) — must not
  crash or throw away the book.

## Verification (DO this, report results)
1. `python3 -c "import engine, run_hourly, publish_office"` — imports clean.
2. **Cost check**: copy `state.json` to a temp file, point a fresh `Cfg`
   at it, run a small synthetic buy+sell, and assert `total_fees > 0` and
   that the buy filled at a higher price than the raw quote (slippage applied)
   and the sell at a lower price. Show the before/after `cash` and
   `total_fees`.
3. **Invariant check**: after a full `run()`, assert
   `abs(cash + sum(qty*last_price) - equity) < 1.0` for both portfolios.
4. **Forced-trade check**: from the current fully-invested state, call `run()`
   and confirm it executes at least one trade this cycle (natural or forced) —
   print the action + fee. Then confirm `last_decision` got set.
5. **Backward-compat check**: confirm `load_state` on the real existing
   `state.json` succeeds and `total_fees`/`last_decision` are present
   (added via setdefault, existing data intact).
6. Do a real `run_hourly.py` **DRY-RUN that does NOT post to Discord** (you may
   add a `--dry-run` flag that skips the Discord `post` but still trades +
   publishes) and show the combined P&L output including fees. If a `--dry-run`
   flag is easiest, implement it; otherwise just show that the trading+publish
   path runs without the network post.

Be concrete in your final report: files changed, the cost model (fee + slippage
defaults), the forced-trade rule, and every verification result (cost check
numbers, invariant residual, the forced action + its fee, the dry-run output).
