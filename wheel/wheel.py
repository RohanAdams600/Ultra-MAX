"""TSLA wheel strategy for the Alpaca paper account.

Stage 1 (put):  sell 1 cash-secured put, strike ~10% below the price,
                expiring 14-28 days out. Expired worthless -> sell another.
                Assigned -> we own 100 shares, go to stage 2.
Stage 2 (call): sell 1 covered call, strike ~10% above what we paid for the
                shares (never below the net cost basis: paid minus premiums
                collected this cycle), expiring 14-28 days out. Expired
                worthless -> sell another. Called away -> back to stage 1.
Both stages:    if the open contract can be bought back for <= 50% of the
                premium received, buy it back and sell a new one.

Hard rules: never sell a put without the cash to take assignment; never sell
a call below the cost basis. Runs only while the market is open.

The wheel keeps its own share count (state.json) so it never touches the 10
TSLA shares trailing_monitor.py manages, and that monitor only covers its own
shares with stops.

Usage:
  python3 wheel/wheel.py              # one check (scheduled every 15 minutes)
  python3 wheel/wheel.py --summary    # daily close summary, appends daily.csv
  python3 wheel/wheel.py --dry-run    # show what it would do, place nothing
"""

import csv
import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.request

SYMBOL = "TSLA"
PUT_OTM = 0.10  # put strike ~10% below the price
CALL_ABOVE_PAID = 0.10  # call strike ~10% above what we paid
MIN_DAYS, MAX_DAYS, IDEAL_DAYS = 14, 28, 21
TAKE_PROFIT = 0.50  # buy back once 50% of the premium is captured
MIN_PREMIUM = 0.05  # don't sell a contract for less than $5
CONTRACTS = 1

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(HERE, "state.json")
DAILY_PATH = os.path.join(HERE, "daily.csv")
DATA = "https://data.alpaca.markets"
DRY_RUN = "--dry-run" in sys.argv
NEW_STATE = {
    "stage": "put",
    "shares": 0,  # TSLA shares owned by the wheel
    "paid_per_share": None,  # assignment strike of the current shares
    "cycle": 1,
    "cycle_premium": 0.0,  # net premium collected in the current cycle
    "net_premium_total": 0.0,  # net premium across all cycles (sold - bought back)
    "gross_premium_total": 0.0,
    "realized_stock_pl": 0.0,
    "capital": None,  # cash reserved by the first put (denominator for returns)
    "open": None,  # the short contract currently held
    "pending": None,  # an order we placed that hasn't resolved
    "log": [],
}


# ---------------------------------------------------------------- plumbing

def load_env():
    path = os.path.join(os.path.dirname(HERE), ".env")
    if os.path.exists(path):
        for line in open(path):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k, v)
    missing = [k for k in ("APCA_API_KEY_ID", "APCA_API_SECRET_KEY") if not os.environ.get(k)]
    if missing:
        sys.exit("Missing credentials: " + ", ".join(missing))


def api(method, path, body=None, base=None):
    base = base or os.environ.get("APCA_API_BASE_URL", "https://paper-api.alpaca.markets")
    base = base.rstrip("/").removesuffix("/v2")
    req = urllib.request.Request(
        base + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "APCA-API-KEY-ID": os.environ["APCA_API_KEY_ID"],
            "APCA-API-SECRET-KEY": os.environ["APCA_API_SECRET_KEY"],
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        if e.code == 404 and path.startswith("/v2/positions/"):
            return None
        raise RuntimeError(f"{method} {path} -> {e.code} {e.read().decode()}")


def load_state():
    if os.path.exists(STATE_PATH):
        return json.load(open(STATE_PATH))
    return json.loads(json.dumps(NEW_STATE))


def save_state(state):
    if DRY_RUN:
        return
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)
        f.write("\n")


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def log(state, event, **kw):
    entry = {"at": now(), "event": event, **kw}
    state["log"].append(entry)
    print(event, json.dumps(kw))


def stock_price():
    return float(api("GET", f"/v2/stocks/{SYMBOL}/trades/latest", base=DATA)["trade"]["p"])


def tsla_qty():
    pos = api("GET", f"/v2/positions/{SYMBOL}")
    return float(pos["qty"]) if pos else 0.0


def quote(sym):
    q = api("GET", f"/v1beta1/options/quotes/latest?symbols={sym}", base=DATA)["quotes"].get(sym)
    return (float(q["bp"]), float(q["ap"])) if q else (0.0, 0.0)


def order(sym, side, limit):
    body = {"symbol": sym, "qty": str(CONTRACTS), "side": side, "type": "limit",
            "limit_price": f"{limit:.2f}", "time_in_force": "day"}
    print(("[dry-run] " if DRY_RUN else "") + f"{side} {CONTRACTS} {sym} @ {limit:.2f}")
    if DRY_RUN:
        return {"id": "dry-run"}
    return api("POST", "/v2/orders", body)


# ------------------------------------------------------------ contract pick

def pick_contract(right, target_strike, min_strike=None):
    """Expiry closest to IDEAL_DAYS within the window, then the strike nearest
    the target (puts: at or below it; calls: at or above it and >= min_strike)."""
    today = dt.date.today()
    q = (f"/v2/options/contracts?underlying_symbols={SYMBOL}&type={right}&status=active&limit=1000"
         f"&expiration_date_gte={today + dt.timedelta(days=MIN_DAYS)}"
         f"&expiration_date_lte={today + dt.timedelta(days=MAX_DAYS)}")
    contracts, token = [], None
    while True:
        page = api("GET", q + (f"&page_token={token}" if token else ""))
        contracts += page.get("option_contracts") or []
        token = page.get("next_page_token")
        if not token:
            break
    if not contracts:
        return None
    exp = min({c["expiration_date"] for c in contracts},
              key=lambda e: abs((dt.date.fromisoformat(e) - today).days - IDEAL_DAYS))
    same = [c for c in contracts if c["expiration_date"] == exp]
    if right == "put":
        ok = [c for c in same if float(c["strike_price"]) <= target_strike]
        best = max(ok, key=lambda c: float(c["strike_price"])) if ok else None
    else:
        floor = max(target_strike, min_strike or 0)
        ok = [c for c in same if float(c["strike_price"]) >= floor]
        best = min(ok, key=lambda c: float(c["strike_price"])) if ok else None
    return best


# ------------------------------------------------------------- the wheel

def net_basis(state):
    """What we actually paid per share, net of premiums collected this cycle."""
    return state["paid_per_share"] - state["cycle_premium"] / state["shares"]


def resolve_pending(state):
    """Settle the order we placed on an earlier run (re-pricing it if it hasn't filled)."""
    p = state["pending"]
    if not p or (DRY_RUN and p["order_id"] == "dry-run"):
        return
    o = api("GET", f"/v2/orders/{p['order_id']}")
    if o["status"] not in ("filled", "canceled", "expired", "rejected", "done_for_day"):
        # Still working: cancel it so this run can re-price, then re-read in case it filled meanwhile.
        try:
            api("DELETE", f"/v2/orders/{p['order_id']}")
        except RuntimeError:
            pass
        o = api("GET", f"/v2/orders/{p['order_id']}")
        if o["status"] != "filled":
            state["retry_attempts"] = p.get("attempts", 0) + 1
            print(f"{p['symbol']} unfilled, re-pricing (try {state['retry_attempts']})")
    state["pending"] = None
    if o["status"] != "filled":
        return
    price = float(o["filled_avg_price"])
    if p["action"] == "open":
        prem = price * 100 * CONTRACTS
        state["open"] = {"symbol": p["symbol"], "right": p["right"], "strike": p["strike"],
                         "expiry": p["expiry"], "premium": price, "opened": now()}
        state["cycle_premium"] += prem
        state["net_premium_total"] += prem
        state["gross_premium_total"] += prem
        if p["right"] == "put" and state["capital"] is None:
            state["capital"] = p["strike"] * 100 * CONTRACTS
        state.pop("retry_attempts", None)
        log(state, "sold", symbol=p["symbol"], price=price, premium=round(prem, 2))
    else:
        cost = price * 100 * CONTRACTS
        kept = state["open"]["premium"] * 100 * CONTRACTS - cost
        state["cycle_premium"] -= cost
        state["net_premium_total"] -= cost
        log(state, "bought back at 50%+ profit", symbol=p["symbol"], price=price, kept=round(kept, 2))
        state["open"] = None


def check_open_contract(state):
    """Detect expiry / assignment of the open short contract."""
    o = state["open"]
    if not o:
        return
    pos = api("GET", f"/v2/positions/{o['symbol']}")
    if pos and float(pos["qty"]) != 0:
        return
    # The contract is gone and we didn't buy it back: expired or assigned.
    acts = api("GET", "/v2/account/activities?activity_types=OPASN,OPEXP,OPEXC&direction=desc&page_size=100") or []
    used = set(state.setdefault("used_activity_ids", []))
    mine = [a for a in acts if a.get("symbol") == o["symbol"] and a.get("id") not in used]
    kinds = {a.get("activity_type") for a in mine}
    state["used_activity_ids"] += [a["id"] for a in mine if a.get("id")]
    assigned = "OPASN" in kinds
    if not kinds:
        # Alpaca usually posts the record by the next morning. Past that, infer from
        # the TSLA share count we saw on the previous run (the TSLA ladder moves at most 55 shares).
        if (dt.date.today() - dt.date.fromisoformat(o["expiry"])).days < 3:
            print(f"{o['symbol']} is gone but Alpaca has no assignment/expiry record yet; waiting")
            return
        delta = tsla_qty() - state.get("last_tsla_qty", 0)
        assigned = delta >= 100 if o["right"] == "put" else delta <= -100
        log(state, "no Alpaca record; inferred from share count", delta=delta, assigned=assigned)
    n = 100 * CONTRACTS
    if o["right"] == "put" and assigned:
        state["shares"] += n
        state["paid_per_share"] = o["strike"]
        state["stage"] = "call"
        log(state, "put assigned: bought shares", shares=n, price=o["strike"],
            net_basis=round(net_basis(state), 2))
    elif o["right"] == "call" and assigned:
        pl = (o["strike"] - state["paid_per_share"]) * n
        state["realized_stock_pl"] += pl
        log(state, "call assigned: shares called away", price=o["strike"], stock_pl=round(pl, 2),
            cycle_premium=round(state["cycle_premium"], 2), cycle=state["cycle"])
        state.update(shares=state["shares"] - n, paid_per_share=None, stage="put",
                     cycle=state["cycle"] + 1, cycle_premium=0.0)
    else:
        log(state, f"{o['right']} expired worthless", symbol=o["symbol"], kept=round(o["premium"] * 100 * CONTRACTS, 2))
    state["open"] = None


def manage_open(state):
    """Take profit at 50%."""
    o = state["open"]
    bid, ask = quote(o["symbol"])
    print(f"open {o['symbol']} sold @ {o['premium']:.2f}, now bid {bid:.2f} / ask {ask:.2f}")
    if 0 < ask <= o["premium"] * (1 - TAKE_PROFIT):
        res = order(o["symbol"], "buy", ask)
        state["pending"] = {"action": "close", "order_id": res["id"], "symbol": o["symbol"]}


def open_new(state):
    price = stock_price()
    attempts = state.pop("retry_attempts", 0)
    if state["stage"] == "put":
        c = pick_contract("put", price * (1 - PUT_OTM))
        if not c:
            print("no put contract in the 14-28 day window")
            return
        strike = float(c["strike_price"])
        acct = api("GET", "/v2/account")
        cash = float(acct.get("options_buying_power") or acct["cash"])
        need = strike * 100 * CONTRACTS
        if cash < need:
            print(f"skipped put: need ${need:,.0f} cash to cover assignment, have ${cash:,.0f}")
            return
    else:
        if state["shares"] < 100 * CONTRACTS:
            print("stage call but fewer than 100 wheel shares; nothing to cover")
            return
        basis = net_basis(state)
        c = pick_contract("call", state["paid_per_share"] * (1 + CALL_ABOVE_PAID), min_strike=basis)
        if not c:
            print("no call contract in the 14-28 day window at or above the basis")
            return
        strike = float(c["strike_price"])
        assert strike >= basis, "never sell a call below the cost basis"
    bid, ask = quote(c["symbol"])
    if bid < MIN_PREMIUM:
        print(f"{c['symbol']} bid {bid:.2f} is below the minimum premium; waiting")
        return
    mid = (bid + ask) / 2
    limit = max(bid, mid - (mid - bid) * min(attempts, 4) / 4)  # step toward the bid on re-tries
    res = order(c["symbol"], "sell", round(limit, 2))
    state["pending"] = {"action": "open", "order_id": res["id"], "symbol": c["symbol"],
                        "right": c["type"], "strike": strike, "expiry": c["expiration_date"],
                        "attempts": attempts}
    print(f"stage {state['stage']}: TSLA {price:.2f}, selling {c['symbol']} (strike {strike}, "
          f"exp {c['expiration_date']}) @ {limit:.2f}")


def run():
    state = load_state()
    clock = api("GET", "/v2/clock")
    if not clock["is_open"]:
        print("Market closed; doing nothing.")
        return
    resolve_pending(state)
    check_open_contract(state)
    if state["open"] and not state["pending"]:
        manage_open(state)
    if not state["open"] and not state["pending"]:
        open_new(state)
    state["last_tsla_qty"] = tsla_qty()
    save_state(state)


def summary():
    state = load_state()
    price = stock_price()
    unreal = (price - state["paid_per_share"]) * state["shares"] if state["shares"] else 0.0
    open_val = 0.0
    o = state["open"]
    if o:
        bid, ask = quote(o["symbol"])
        open_val = (o["premium"] - (bid + ask) / 2) * 100 * CONTRACTS  # unrealized gain on the short
    total = state["net_premium_total"] + state["realized_stock_pl"] + unreal
    pct = total / state["capital"] * 100 if state["capital"] else 0.0
    today = dt.date.today().isoformat()
    print(f"TSLA wheel, {today} close (TSLA ${price:,.2f})")
    print(f"Stage: {'1 - selling puts' if state['stage'] == 'put' else '2 - selling covered calls'}, cycle {state['cycle']}")
    print(f"Premium collected: ${state['net_premium_total']:,.2f} net "
          f"(${state['gross_premium_total']:,.2f} sold, this cycle ${state['cycle_premium']:,.2f})")
    if o:
        print(f"Open: short {o['symbol']} strike {o['strike']} exp {o['expiry']}, sold @ {o['premium']:.2f}, "
              f"unrealized ${open_val:+,.2f}")
    else:
        print("Open: no contract")
    if state["shares"]:
        print(f"Shares: {state['shares']} TSLA paid {state['paid_per_share']:.2f}, "
              f"net basis {net_basis(state):.2f}, unrealized ${unreal:+,.2f}")
    print(f"Total return: ${total:+,.2f} ({pct:+.2f}% on ${state['capital'] or 0:,.0f} capital)")
    if not DRY_RUN:
        new = not os.path.exists(DAILY_PATH)
        with open(DAILY_PATH, "a", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["date", "tsla", "stage", "cycle", "net_premium", "realized_stock_pl",
                            "unrealized_shares", "open_contract", "total_return", "total_return_pct"])
            w.writerow([today, f"{price:.2f}", state["stage"], state["cycle"], f"{state['net_premium_total']:.2f}",
                        f"{state['realized_stock_pl']:.2f}", f"{unreal:.2f}", o["symbol"] if o else "",
                        f"{total:.2f}", f"{pct:.2f}"])


if __name__ == "__main__":
    load_env()
    summary() if "--summary" in sys.argv else run()
