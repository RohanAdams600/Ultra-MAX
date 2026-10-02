# TSLA strategy (Alpaca paper account)

Everything here is live on the paper account and enforced by Alpaca orders plus
`trailing_monitor.py`, which a Routine runs every weekday at 9:45, 10:45, 11:45,
12:45, 1:45, 2:45 and 3:45 PM ET.

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
