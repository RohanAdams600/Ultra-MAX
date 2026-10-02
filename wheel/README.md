# TSLA wheel (Alpaca paper account)

One contract (100 shares) at a time.

## Stage 1: cash-secured puts
- Sell 1 TSLA put with the strike nearest **10% below** the current price
  (at or below it), expiring **14–28 days** out (closest to 21).
- Only if the account's options buying power covers assignment
  (strike × 100, about $32,000 with TSLA near $355). Otherwise it waits.
- Expires worthless: sell another. Assigned: we own 100 shares, go to stage 2.

## Stage 2: covered calls
- Sell 1 call with the strike nearest **10% above what we paid** (the put
  strike), at or above it, expiring 14–28 days out.
- **Never below the cost basis:** paid per share minus the premium collected
  this cycle.
- Expires worthless: sell another. Called away: record the stock gain, start a
  new cycle at stage 1.

## Both stages
- **50% take-profit:** once the contract can be bought back for half (or less)
  of the premium received, buy it back and sell a new one.
- Orders are limit orders at the bid/ask midpoint. If one hasn't filled by the
  next check, it is cancelled and re-priced a step closer to the bid.
- Premium is tracked net (sold minus bought back) across all cycles.

## Schedule
- **Every 15 minutes, 9:30–3:45 ET on weekdays:** `python3 wheel/wheel.py`.
  It does nothing when the market is closed (weekends, holidays, outside hours).
- **4:05 PM ET:** `python3 wheel/wheel.py --summary` reports the stage, premium
  collected, positions and total return, and appends a row to `daily.csv`.
- Total return = net premium + realized stock gains + unrealized gain on wheel
  shares, as a % of the cash the first put reserved.

## Living alongside the other TSLA strategy
The account already holds 10 TSLA shares with stop-losses and dip-buy orders
(`STRATEGY.md`). The wheel keeps its own share count in `state.json`.
`trailing_monitor.py` subtracts those shares, so its stops never cover the
wheel's shares, which have to stay free for covered calls.

## Files
- `wheel.py` is the strategy. Add `--dry-run` to place no orders.
- `state.json` is the stage, open contract, pending order, premium totals and an event log.
- `daily.csv` has one row per trading day.
