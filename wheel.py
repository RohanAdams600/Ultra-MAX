"""TSLA wheel strategy for the Alpaca paper account.

Stage 1: sell a cash-secured put ~10% below the price, 14-28 days out.
Stage 2: after assignment, sell a covered call ~10% above the cost basis
         (assignment price minus every premium collected this cycle), never
         below the cost basis.
Either stage: buy the option back once it has lost 50% of its value, then
sell the next one. Called away -> back to stage 1.

Nothing is stored locally: stage, premium and cost basis are rebuilt from the
account's activity history (option fills, assignments, expirations) on every
run, so the script survives container restarts.

Usage:
  python3 wheel.py              # trade (only while the market is open)
  python3 wheel.py --dry-run    # show what it would do
  python3 wheel.py --summary    # daily summary, never trades
"""

import datetime as dt
import re
import sys
import time

from trailing_monitor import api, load_env

UNDERLYING = "TSLA"
CONTRACTS = 1
WHEEL_START = "2026-10-02"  # ignore activity before the wheel began
PUT_OTM = 0.10  # put strike ~10% below the stock price
CALL_ABOVE_BASIS = 0.10  # call strike ~10% above the cost basis
DTE_MIN, DTE_TARGET, DTE_MAX = 14, 21, 28
TAKE_PROFIT = 0.50  # close once 50% of the premium is captured
REPRICE_AFTER_MIN = 10  # cancel and re-price an unfilled sell after this long
SKIP_OPEN_MIN = 10  # spreads are wide right after the open

DRY_RUN = "--dry-run" in sys.argv
OCC = re.compile(r"^([A-Z]+)(\d{6})([CP])(\d{8})$")
DATA = "https://data.alpaca.markets"


def parse(sym):
    m = OCC.match(sym or "")
    if not m or m.group(1) != UNDERLYING:
        return None
    exp = dt.datetime.strptime(m.group(2), "%y%m%d").date()
    return {"exp": exp, "type": "put" if m.group(3) == "P" else "call", "strike": int(m.group(4)) / 1000}


def tick_round(price, down=False):
    tick = 0.01 if price < 3 else 0.05
    n = price / tick
    n = int(n) if down else round(n)
    return round(max(n, 1) * tick, 2)


def act(desc, method, path, body=None):
    print(("[dry-run] " if DRY_RUN else "ACTION ") + desc)
    if not DRY_RUN:
        return api(method, path, body)


def activities():
    out, token = [], None
    while True:
        q = f"/v2/account/activities?activity_types=FILL,OPASN,OPEXP&after={WHEEL_START}T00:00:00Z&direction=asc&page_size=100"
        page = api("GET", q + (f"&page_token={token}" if token else ""))
        out += page
        if len(page) < 100:
            return out
        token = page[-1]["id"]


def wheel_state():
    """Replay the wheel's history from Alpaca activities."""
    s = {"total_premium": 0.0, "cycle_premium": 0.0, "shares": 0, "assign_price": None,
         "realized_shares": 0.0, "cycles_done": 0, "first_capital": None, "log": []}
    for a in activities():
        o = parse(a.get("symbol"))
        if not o:
            continue
        kind = a["activity_type"]
        qty = abs(int(float(a.get("qty", 0))))
        if kind == "FILL":
            cash = float(a["price"]) * qty * 100 * (1 if a["side"].startswith("sell") else -1)
            s["total_premium"] += cash
            s["cycle_premium"] += cash
            if s["first_capital"] is None and o["type"] == "put":
                s["first_capital"] = o["strike"] * 100 * qty
            s["log"].append(f"{a['transaction_time'][:10]} {a['side']} {a['symbol']} x{qty} @ {a['price']} ({cash:+.2f})")
        elif kind == "OPASN" and o["type"] == "put":
            s["shares"] += 100 * qty
            s["assign_price"] = o["strike"]
            s["log"].append(f"{a.get('date', '')} PUT ASSIGNED {a['symbol']}: bought {100 * qty} @ {o['strike']}")
        elif kind == "OPASN" and o["type"] == "call":
            s["shares"] -= 100 * qty
            s["realized_shares"] += (o["strike"] - (s["assign_price"] or o["strike"])) * 100 * qty
            s["log"].append(f"{a.get('date', '')} CALLED AWAY {a['symbol']}: sold {100 * qty} @ {o['strike']}")
            if s["shares"] <= 0:
                s.update(shares=0, assign_price=None, cycle_premium=0.0)
                s["cycles_done"] += 1
        elif kind == "OPEXP":
            s["log"].append(f"{a.get('date', '')} EXPIRED worthless {a['symbol']}")
    s["basis"] = (s["assign_price"] - s["cycle_premium"] / s["shares"]) if s["shares"] else None
    s["stage"] = 2 if s["shares"] >= 100 else 1
    return s


def wheel_shares():
    """Shares owned by the wheel (used by trailing_monitor to leave them alone)."""
    return wheel_state()["shares"]


def stock_price():
    return api("GET", f"/v2/stocks/{UNDERLYING}/trades/latest", base=DATA)["trade"]["p"]


def quotes(symbols):
    if not symbols:
        return {}
    snap = api("GET", f"/v1beta1/options/snapshots?feed=indicative&symbols={','.join(symbols)}", base=DATA)
    out = {}
    for sym, v in (snap.get("snapshots") or {}).items():
        q = v.get("latestQuote") or {}
        out[sym] = (q.get("bp") or 0, q.get("ap") or 0)
    return out


def pick_contract(kind, target, min_strike=None):
    today = dt.date.today()
    lo, hi = today + dt.timedelta(DTE_MIN), today + dt.timedelta(DTE_MAX)
    q = (f"/v2/options/contracts?underlying_symbols={UNDERLYING}&type={kind}&status=active"
         f"&expiration_date_gte={lo}&expiration_date_lte={hi}"
         f"&strike_price_gte={target * 0.9:.2f}&strike_price_lte={target * 1.1:.2f}&limit=1000")
    cs = [c for c in api("GET", q)["option_contracts"] if c.get("tradable")]
    if min_strike is not None:
        cs = [c for c in cs if float(c["strike_price"]) >= min_strike]
    if not cs:
        return None
    exps = sorted({c["expiration_date"] for c in cs},
                  key=lambda e: abs((dt.date.fromisoformat(e) - today).days - DTE_TARGET))
    for exp in exps:
        cands = sorted((c for c in cs if c["expiration_date"] == exp),
                       key=lambda c: (abs(float(c["strike_price"]) - target), float(c["strike_price"])))
        qs = quotes([c["symbol"] for c in cands[:3]])
        for c in cands[:3]:
            bid, ask = qs.get(c["symbol"], (0, 0))
            if bid > 0 and ask >= bid:
                return {"symbol": c["symbol"], "strike": float(c["strike_price"]), "exp": exp,
                        "bid": bid, "ask": ask, "limit": tick_round((bid + ask) / 2)}
    return None


def free_cash(account, orders, positions):
    """Cash not already promised to open stock buys or other short puts."""
    cash = float(account["cash"])
    for o in orders:
        if o["side"] == "buy" and o.get("asset_class") == "us_equity" and o.get("limit_price"):
            cash -= float(o["limit_price"]) * float(o["qty"])
        p = parse(o["symbol"])
        if p and p["type"] == "put" and o["side"] == "sell":
            cash -= p["strike"] * 100 * float(o["qty"])
    for pos in positions:
        p = parse(pos["symbol"])
        if p and p["type"] == "put" and float(pos["qty"]) < 0:
            cash -= p["strike"] * 100 * abs(float(pos["qty"]))
    return cash


def trade():
    load_env()
    clock = api("GET", "/v2/clock")
    now = dt.datetime.fromisoformat(clock["timestamp"])
    if not clock["is_open"]:
        if not DRY_RUN:
            print("Market closed: doing nothing.")
            return
        print("Market closed: dry run continues anyway to show what would happen.")
    elif (now - now.replace(hour=9, minute=30, second=0, microsecond=0)).total_seconds() < SKIP_OPEN_MIN * 60:
        print("First minutes after the open: waiting for spreads to settle.")
        return

    s = wheel_state()
    price = stock_price()
    account = api("GET", "/v2/account")
    orders = api("GET", "/v2/orders?status=open&limit=500")
    positions = api("GET", "/v2/positions")
    opt_orders = [o for o in orders if parse(o["symbol"])]
    shorts = [p for p in positions if parse(p["symbol"]) and float(p["qty"]) < 0]
    print(f"stage={s['stage']} price={price} wheel_shares={s['shares']} basis={s['basis']} "
          f"premium_total={s['total_premium']:.2f}")

    # 1. Holding a short option: make sure the 50% take-profit order is working.
    if shorts:
        for p in shorts:
            entry = float(p["avg_entry_price"])
            target = tick_round(entry * (1 - TAKE_PROFIT), down=True)
            btc = [o for o in opt_orders if o["symbol"] == p["symbol"] and o["side"] == "buy"]
            bid, ask = quotes([p["symbol"]]).get(p["symbol"], (0, 0))
            info = parse(p["symbol"])
            print(f"short {p['symbol']} x{abs(int(float(p['qty'])))} sold @ {entry} now {bid}/{ask}, "
                  f"take-profit @ {target}")
            if info["exp"] <= dt.date.today():
                print("Expires today: letting it run to expiration.")
                continue
            if not btc:
                act(f"buy to close {p['symbol']} @ {target} (50% profit)", "POST", "/v2/orders",
                    {"symbol": p["symbol"], "qty": str(abs(int(float(p["qty"])))), "side": "buy",
                     "type": "limit", "limit_price": str(target), "time_in_force": "day",
                     "position_intent": "buy_to_close"})
        return

    # 2. An unfilled sell from an earlier run: re-price it if it has been sitting.
    for o in [o for o in opt_orders if o["side"] == "sell"]:
        age = (now - dt.datetime.fromisoformat(o["submitted_at"].replace("Z", "+00:00"))).total_seconds() / 60
        if age < REPRICE_AFTER_MIN:
            print(f"waiting on sell {o['symbol']} @ {o['limit_price']} ({age:.0f} min old)")
            return
        act(f"cancel stale sell {o['symbol']} @ {o['limit_price']} to re-price", "DELETE", f"/v2/orders/{o['id']}")
        if not DRY_RUN:
            time.sleep(2)
            if api("GET", f"/v2/orders/{o['id']}")["status"] == "filled":
                print("It filled while cancelling; nothing more to do.")
                return
        orders = [x for x in orders if x["id"] != o["id"]]

    # 3. No option open: sell the next one for the current stage.
    if s["stage"] == 1:
        c = pick_contract("put", price * (1 - PUT_OTM))
        if not c:
            print("No suitable put found.")
            return
        need = c["strike"] * 100 * CONTRACTS
        cash = free_cash(account, orders, positions)
        if cash < need or float(account.get("options_buying_power", 0)) < need:
            print(f"SKIP: put needs ${need:,.0f} cash, only ${cash:,.0f} free "
                  f"(options buying power ${float(account.get('options_buying_power', 0)):,.0f}).")
            return
        side, intent = "cash-secured put", "sell_to_open"
    else:
        target = s["basis"] * (1 + CALL_ABOVE_BASIS)
        c = pick_contract("call", target, min_strike=s["basis"])
        if not c:
            print("No suitable call at or above the cost basis.")
            return
        side, intent = "covered call", "sell_to_open"
    act(f"sell {CONTRACTS} {side} {c['symbol']} (strike {c['strike']}, exp {c['exp']}) "
        f"@ {c['limit']} [bid {c['bid']} / ask {c['ask']}]", "POST", "/v2/orders",
        {"symbol": c["symbol"], "qty": str(CONTRACTS), "side": "sell", "type": "limit",
         "limit_price": str(c["limit"]), "time_in_force": "day", "position_intent": intent})


def summary():
    load_env()
    s = wheel_state()
    price = stock_price()
    account = api("GET", "/v2/account")
    positions = api("GET", "/v2/positions")
    shorts = [p for p in positions if parse(p["symbol"])]
    open_cost = 0.0
    print(f"=== TSLA wheel summary {dt.date.today()} ===")
    print(f"Stage: {s['stage']} ({'selling puts' if s['stage'] == 1 else 'selling covered calls'})"
          f" | completed cycles: {s['cycles_done']} | TSLA ${price}")
    print(f"Premium collected (net of buybacks): ${s['total_premium']:,.2f} total, "
          f"${s['cycle_premium']:,.2f} this cycle")
    for p in shorts:
        mark = float(p.get("current_price") or 0)
        open_cost += mark * 100 * abs(float(p["qty"]))
        print(f"Open option: {p['symbol']} x{p['qty']} sold @ {p['avg_entry_price']}, now {mark} "
              f"(cost to close ${mark * 100 * abs(float(p['qty'])):,.2f})")
    unreal = 0.0
    if s["shares"]:
        unreal = (price - s["assign_price"]) * s["shares"]
        print(f"Wheel shares: {s['shares']} assigned @ {s['assign_price']}, cost basis ${s['basis']:.2f}, "
              f"unrealized ${unreal:+,.2f}")
    if not shorts and not s["shares"]:
        print("No open wheel position.")
    pl = s["total_premium"] + s["realized_shares"] + unreal - open_cost
    cap = s["first_capital"] or 0
    pct = f" ({pl / cap:+.2%} on ${cap:,.0f} put collateral)" if cap else ""
    print(f"Wheel total return: ${pl:+,.2f}{pct}")
    eq = float(account["equity"])
    print(f"Account equity: ${eq:,.2f} ({eq / 100000 - 1:+.2%} since $100,000 start), cash ${float(account['cash']):,.2f}")
    if s["log"]:
        print("History:")
        for line in s["log"][-10:]:
            print("  " + line)


if __name__ == "__main__":
    summary() if "--summary" in sys.argv else trade()
