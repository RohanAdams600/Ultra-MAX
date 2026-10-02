"""TSLA floor / trailing-floor / ladder monitor for the Alpaca paper account.

Run during market hours (see the scheduled Routine). It reads everything it
needs from Alpaca, so it keeps no state of its own:

  * Floor: every share is covered by a sell stop at FLOOR_PRICE (-35% from
    the first fill at $355.99).
  * Trailing floor: once the price is >= 10% above the average entry price,
    the fixed floor is replaced by a 5% trailing stop (only ever moves up).
  * Ladder: the -15% / -22% / -29% limit buys (10 / 15 / 20 shares) are standing GTC orders with their own
    floor stops attached; this script only checks that they are still there.
  * If the position is gone (floor or trailing stop hit), leftover TSLA buy
    orders are cancelled so the ladder never re-buys a closed position.

Credentials come from APCA_* environment variables or a local .env file.
Usage: python3 trailing_monitor.py [--dry-run]
"""

import json
import os
import sys
import urllib.error
import urllib.request

SYMBOL = "TSLA"
FLOOR_PRICE = 231.39  # -35% from the first fill at $355.99
TRAIL_TRIGGER = 0.10  # switch to the trailing stop at +10% over avg entry
TRAIL_PERCENT = 5  # trailing stop distance
LADDER = [(302.59, 10), (277.67, 15), (252.75, 20)]  # (limit price, qty): -15%, -22%, -29%

DRY_RUN = "--dry-run" in sys.argv


def load_env():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
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
    req = urllib.request.Request(
        base.rstrip("/") + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "APCA-API-KEY-ID": os.environ["APCA_API_KEY_ID"],
            "APCA-API-SECRET-KEY": os.environ["APCA_API_SECRET_KEY"],
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req) as r:
            raw = r.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        if e.code == 404 and path.startswith("/v2/positions/"):
            return None
        raise RuntimeError(f"{method} {path} -> {e.code} {e.read().decode()}")


def wheel_shares():
    """(shares, price paid) held by the wheel strategy."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wheel", "state.json")
    if not os.path.exists(path):
        return 0, 0.0
    w = json.load(open(path))
    return int(w.get("shares", 0)), float(w.get("paid_per_share") or 0)


def act(desc, method, path, body=None):
    print(("[dry-run] " if DRY_RUN else "") + desc)
    if not DRY_RUN:
        return api(method, path, body)


def main():
    load_env()
    clock = api("GET", "/v2/clock")
    price = api("GET", f"/v2/stocks/{SYMBOL}/trades/latest", base="https://data.alpaca.markets")["trade"]["p"]
    pos = api("GET", f"/v2/positions/{SYMBOL}")
    orders = [o for o in api("GET", f"/v2/orders?status=open&nested=false&symbols={SYMBOL}&limit=500")]
    qty = int(float(pos["qty"])) if pos else 0
    # Shares owned by the wheel strategy (wheel/state.json) back its covered calls; leave them alone.
    total, (wheel, wheel_paid) = qty, wheel_shares()
    qty = max(0, qty - wheel)
    print(f"market_open={clock['is_open']} price={price} position={qty} (excluding {wheel} wheel shares)")

    buys = [o for o in orders if o["side"] == "buy"]
    # "held" stops are OTO legs waiting on an unfilled ladder buy; they are not live yet.
    live_sells = [o for o in orders if o["side"] == "sell" and o["status"] != "held"
                  and o["type"] in ("stop", "trailing_stop")]

    if qty == 0:
        print("No position: strategy is closed.")
        for o in buys:
            act(f"cancel leftover buy {o['qty']} @ {o.get('limit_price')} ({o['id']})", "DELETE", f"/v2/orders/{o['id']}")
        return

    avg = float(pos["avg_entry_price"])
    if wheel:  # Alpaca blends all TSLA shares; take the wheel's shares back out
        avg = (avg * total - wheel_paid * wheel) / qty
    trail_on = any(o["type"] == "trailing_stop" for o in live_sells)
    want_trail = trail_on or price >= avg * (1 + TRAIL_TRIGGER)
    covered = sum(int(float(o["qty"])) for o in live_sells)
    print(f"avg_entry={avg:.2f} trail_trigger={avg * (1 + TRAIL_TRIGGER):.2f} "
          f"live_stop_qty={covered} trailing={'on' if trail_on else 'off'}")

    # Rebuild protection when the type should change or coverage doesn't match the position.
    mixed = want_trail and any(o["type"] != "trailing_stop" for o in live_sells)
    if covered != qty or mixed:
        for o in live_sells:
            act(f"cancel {o['type']} sell {o['qty']} ({o['id']})", "DELETE", f"/v2/orders/{o['id']}")
        if want_trail:
            act(f"place trailing stop sell {qty} @ {TRAIL_PERCENT}%", "POST", "/v2/orders",
                {"symbol": SYMBOL, "qty": str(qty), "side": "sell", "type": "trailing_stop",
                 "trail_percent": str(TRAIL_PERCENT), "time_in_force": "gtc"})
        else:
            act(f"place floor stop sell {qty} @ {FLOOR_PRICE}", "POST", "/v2/orders",
                {"symbol": SYMBOL, "qty": str(qty), "side": "sell", "type": "stop",
                 "stop_price": str(FLOOR_PRICE), "time_in_force": "gtc"})
    else:
        print("Protection OK, no change.")

    def key(o):
        return (round(float(o["limit_price"]), 2), int(float(o["qty"])))
    open_ladder = {key(o) for o in buys if o.get("limit_price")}
    closed = api("GET", f"/v2/orders?status=closed&symbols={SYMBOL}&side=buy&limit=500")
    filled = {key(o): o for o in closed if o.get("limit_price") and o["status"] == "filled"}
    for step in LADDER:
        if step in open_ladder:
            state = "open"
        elif step in filled:
            state = f"FILLED @ {filled[step]['filled_avg_price']} on {filled[step]['filled_at'][:10]}"
        else:
            state = "MISSING (cancelled?)"
        print(f"ladder {step[1]} @ {step[0]}: {state}")


if __name__ == "__main__":
    main()
