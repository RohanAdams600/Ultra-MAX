# TSLA strategy (Alpaca paper account)

Two strategies run side by side on TSLA: a **trailing-stop + ladder** position
(`trailing_monitor.py`) and the **wheel** (`wheel.py`).

## Schedule

- A Routine fires every weekday at :40 past each hour, 9:40 AM to 3:40 PM ET,
  and starts `run_quarter_hours.sh`. That runs both scripts immediately and every
  15 minutes after (Routines can't fire more often than once an hour).
- A second Routine posts a daily summary at 4:05 PM ET (read-only).
- Both scripts do nothing while the market is closed (nights, weekends, holidays).

# Part 1: trailing stop + ladder

## Entry

- 10 shares bought overnight on 2026-10-01 at **$355.99**.

## Floor (stop-loss)

- Every share is covered by a GTC sell stop at **$231.39** (−35% from $355.99).
- Ladder buys carry their own floor stop (OTO leg), so new shares are covered
  the moment they fill.
- Stops only trigger in regular hours; an overnight gap fills at the open price.

## Trailing floor

- When the price is **≥ 10% above the average entry price**, the monitor
  replaces the fixed floor with a **5% trailing stop** on the whole position.
  It only moves up.
- The trigger follows the average entry, so it comes down as ladder rungs fill:

| Rungs filled | Shares | Avg entry | Trailing turns on at |
|---|---|---|---|
| none | 10 | $355.99 | $391.59 |
| −15% | 20 | $329.29 | $362.22 |
| −15%, −22% | 35 | $307.17 | $337.89 |
| all three | 55 | $287.38 | $316.12 |

## Ladder (buy the dip)

| Rung | Drop from $355.99 | Limit | Shares | Cost | Chance of reaching it within 60 days* |
|---|---|---|---|---|---|
| 1 | −15% | $302.59 | 10 | $3,025.90 | ~45% |
| 2 | −22% | $277.67 | 15 | $4,165.05 | ~20% |
| 3 | −29% | $252.75 | 20 | $5,055.00 | ~6% |

*Share of 60-day windows over the past year where TSLA fell at least that far
(daily volatility ≈ 3%, worst drawdown −39%).

Why this shape:
- **Size grows as price falls**, so the average cost drops faster than a flat
  ladder.
- **Every rung sits above the floor.** The last rung is about 9% above $231.39,
  so it has room to recover before the stop.
- **Capped risk:** if everything fills and the floor hits, the loss is about
  **$3,079 (≈ 3% of the $100k account)**:
  10 × $124.60 + 10 × $71.20 + 15 × $46.28 + 20 × $21.36.

## What the monitor does each run

1. Keeps live stop coverage equal to the position (rebuilds it after fills).
2. Switches to the 5% trailing stop once the trigger above is reached.
3. If the position is closed by a stop, cancels the leftover ladder buys.
4. Reports each rung as open, filled (with price and date) or missing.

## Not covered yet

- Re-entry after a stop-out: no rule, so the monitor buys nothing back.
- Take-profit target: none; upside is left to the trailing stop.
- Rungs are one-shot; a filled rung is not re-armed if the price recovers.

# Part 2: the wheel

One contract (100 shares) at a time.

## Stage 1: cash-secured puts
- Sell 1 put with the strike nearest **10% below** the current price, on the
  expiration closest to **21 days** out (allowed range 14–28 days).
- Limit price is the bid/ask midpoint; if it hasn't filled after 10 minutes it is
  cancelled and re-priced on the next run.
- **Only if free cash covers assignment:** cash minus open stock buy orders
  (the ladder) minus other short-put collateral must be ≥ strike × 100, and
  Alpaca's options buying power must agree.
- Expires worthless → sell the next put. Assigned → stage 2.

## Stage 2: covered calls
- **Cost basis** = assignment price − every premium collected this cycle
  (net of buybacks), per share.
- Sell 1 call with the strike nearest **10% above the cost basis**, same
  expiration rules. **Never below the cost basis.**
- Expires worthless → sell the next call. Called away → back to stage 1 and a
  new cycle starts.

## Both stages
- **50% take-profit:** while an option is open, a buy-to-close limit at 50% of
  the premium received sits on the book each day; once it fills, the next run
  sells a new option. On expiration day the option is left to expire.
- **No saved state file:** stage, premium and cost basis are rebuilt each run
  from Alpaca's account activity (option fills, assignments, expirations since
  2026-10-02).
- **Kept apart from Part 1:** wheel shares are excluded from the trailing
  floor/stop, so a stop can never sell shares backing a covered call.

## Daily summary (4:05 PM ET)
Stage, premium collected (total and this cycle), open option, wheel shares and
cost basis, wheel total return (premium + share P/L − cost to close the open
option, also as % of the first put's collateral), and account equity return.
