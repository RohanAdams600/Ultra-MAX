# Congressional copy-trader (Alpaca paper account)

Copies the disclosed stock and option trades of **Rep. Nancy Pelosi (CA-11)**
into the Alpaca paper account, then tracks how the copies perform.

## Why Pelosi

- She is the most-tracked congressional trader. Trackers such as Unusual
  Whales have reported her portfolio beating the S&P 500 in recent years, and she is still active in 2026 (PTRs filed in January,
  June and August).
- Her filings give the **exact option contract**: "Purchased 100 call options
  with a strike price of $100 and an expiration date of 6/17/27." The bot can
  buy the same contract, not a guess. Most members disclose only "stock, $1k–$15k".
- Her trades are concentrated, high-conviction bets: deep-in-the-money LEAP
  calls on large tech and energy names.

To follow someone else, change `TARGET` in `copy_trader.py` (House members
only; `StateDst` is in the House filing index).

## Where the data comes from

Capitol Trades blocks automated access, so the bot reads its source directly:
the House Clerk's disclosure feed
(`disclosures-clerk.house.gov/public_disc/financial-pdfs/<year>FD.zip` for the
index, `ptr-pdfs/<year>/<DocID>.pdf` for each report). New filings show up
there the same day Capitol Trades gets them.

**Delay:** the law gives members up to 45 days to disclose. You are buying
weeks after she did, often at a different price.

## Rules

| Her trade | What the bot does |
|---|---|
| Buys shares | Market buy of **$2,500** notional |
| Buys calls/puts | Limit buy at the ask on the **same contract** (nearest Alpaca expiry within 7 days), as many contracts as $2,500 covers, at least 1 if one costs ≤ $10,000 |
| Sells (full) | Sells everything **we** bought of that instrument |
| Sells (partial) | Sells half of ours |
| Exercises options | Exercises our matching contract, if we hold it |
| Gifts, donations, funds, LLCs, spin-offs | Skipped and logged |

Limits: no new buys once the copies' total cost reaches **$40,000**. **TSLA** is
never touched here, because `trailing_monitor.py` manages it.

First run: filings from the last 45 days were copied (Aug 21 2026: BE shares
and calls, INTC shares and calls). Older filings were marked as seen.

## Files

- `copy_trader.py` is the bot. Run `python3 copy_trader/copy_trader.py`, or add `--dry-run` to place no orders.
- `state.json` is its memory: seen filings, queued trades, our holdings, and every action with order IDs.
- `performance.csv` gets one row per run: copy positions, market value, cost basis, unrealized P/L.

Orders go out only while the market is open. Otherwise trades stay queued for
the next run. Order IDs are derived from the trade, so a re-run can't double-buy.
