"""Congressional copy-trader for the Alpaca paper account.

Watches a member of Congress's Periodic Transaction Reports (PTRs) and mirrors
their stock and option trades. Capitol Trades blocks automated access, so this
reads the same source it uses: the House Clerk's official disclosure feed
(https://disclosures-clerk.house.gov). Each run:

  1. Downloads the current year's filing index and finds new PTRs for TARGET.
  2. Parses each PTR PDF (pdftotext) into trades: ticker, buy/sell, shares or
     option contract (call/put, strike, expiration).
  3. Queues every copyable trade in state.json, then executes the queue while
     the market is open:
       * stock buy   -> market buy of BUDGET_PER_TRADE notional
       * option buy  -> limit buy at the ask of the same contract (OCC symbol),
                        as many contracts as fit the budget (at least 1 if one
                        contract costs <= MAX_PER_TRADE)
       * sell        -> sells what WE bought of that instrument: everything on a
                        full sale, half on "S (partial)"
       * exercise    -> exercises our matching option contract, if we hold it
  4. Appends a performance snapshot of the copied positions to performance.csv.

state.json is the bot's memory (seen filings, queue, our holdings, order
log) and is committed to git so the history survives between runs.

Credentials come from APCA_* environment variables or a .env file in the repo
root. Usage: python3 copy_trader/copy_trader.py [--dry-run]
"""

import csv
import datetime as dt
import io
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
import zipfile

TARGET = {"last": "Pelosi", "first": "Nancy", "state_dst": "CA11"}
BUDGET_PER_TRADE = 2500.0  # dollars per copied buy
MAX_PER_TRADE = 10000.0  # never spend more than this on one copied buy
MAX_COPY_EXPOSURE = 40000.0  # stop opening new copies above this total cost
BOOTSTRAP_DAYS = 45  # on the first run, also copy filings this recent
EXCLUDE = {"TSLA"}  # managed by trailing_monitor.py; never touch it here
COPY_TYPES = {"ST", "OP"}  # stocks and options; funds, LLCs, bonds are skipped

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(HERE, "state.json")
PERF_PATH = os.path.join(HERE, "performance.csv")
HOUSE = "https://disclosures-clerk.house.gov/public_disc"
DATA = "https://data.alpaca.markets"
DRY_RUN = "--dry-run" in sys.argv
UA = {"User-Agent": "Mozilla/5.0 (copy-trader research bot)"}


# ---------------------------------------------------------------- utilities

def load_env():
    path = os.path.join(os.path.dirname(HERE), ".env")
    if os.path.exists(path):
        for line in open(path):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k, v)
    return all(os.environ.get(k) for k in ("APCA_API_KEY_ID", "APCA_API_SECRET_KEY"))


def fetch(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60) as r:
        return r.read()


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
    return {"target": TARGET, "seen_filings": [], "queue": [], "holdings": {}, "log": []}


def save_state(state):
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)
        f.write("\n")


def parse_date(s):
    m, d, y = s.split("/")
    y = int(y)
    return dt.date(y + 2000 if y < 100 else y, int(m), int(d))


def occ_symbol(ticker, expiry, right, strike):
    return f"{ticker}{expiry:%y%m%d}{right[0].upper()}{int(round(strike * 1000)):08d}"


# --------------------------------------------------------- disclosure feed

def list_filings(year):
    """All PTRs (FilingType P) filed by TARGET in `year`."""
    z = zipfile.ZipFile(io.BytesIO(fetch(f"{HOUSE}/financial-pdfs/{year}FD.zip")))
    root = ET.fromstring(z.read(f"{year}FD.xml"))
    out = []
    for m in root:
        row = {c.tag: (c.text or "").strip() for c in m}
        if (row.get("FilingType") == "P" and row.get("Last") == TARGET["last"]
                and row.get("StateDst") == TARGET["state_dst"]):
            out.append({"doc_id": row["DocID"], "filed": str(parse_date(row["FilingDate"])),
                        "year": year})
    return out


TXN_LINE = re.compile(r"\s(P|S \(partial\)|S|E)\s+(\d\d/\d\d/\d{4})\s+(\d\d/\d\d/\d{4})\s+\$")
OPTION = re.compile(r"(Purchased|Sold)\s+([\d,]+)\s+(call|put)\s+options?\s+with a strike price of\s+"
                    r"\$([\d,.]+)\s+and an expiration date of\s+(\d+/\d+/\d+)", re.I)
EXERCISE = re.compile(r"Exercised\s+([\d,]+)\s+(call|put)\s+options?.*?strike price of\s+\$([\d,.]+)"
                      r".*?expiration date of\s+(\d+/\d+/\d+)", re.I)
SHARES = re.compile(r"(Purchased|Sold)\s+([\d,]+)\s+shares", re.I)


def parse_ptr(text, doc_id):
    """Turn PTR text (pdftotext -layout) into a list of trade dicts."""
    lines = [l for l in text.splitlines() if l.strip()]
    starts = [i for i, l in enumerate(lines) if TXN_LINE.search(l)]
    trades = []
    for n, i in enumerate(starts):
        end = starts[n + 1] if n + 1 < len(starts) else len(lines)
        block = lines[i:end]
        cut = next((k for k, l in enumerate(block) if l.lstrip().startswith("* For the complete")), len(block))
        block = [l for l in block[:cut] if not re.match(r"\s*(ID\s+Owner|Type\s+Date|\$200\?)", l.strip())]
        joined = " ".join(l.strip() for l in block)
        m = TXN_LINE.search(lines[i])
        tx_type = m.group(1)
        ticker = re.search(r"\(([A-Z][A-Z.\-]{0,6})\)", joined)
        asset = re.search(r"\[([A-Z]{2})\]", joined)
        desc = re.search(r"\bD\s+:\s*(.*)$", joined)
        trade = {
            "id": f"{doc_id}-{n}",
            "doc_id": doc_id,
            "tx_type": tx_type,
            "tx_date": str(parse_date(m.group(2))),
            "ticker": ticker.group(1).replace(".", "") if ticker else None,
            "asset_type": asset.group(1) if asset else None,
            "description": desc.group(1).strip() if desc else "",
        }
        d = trade["description"]
        if (o := OPTION.search(d)):
            trade.update(kind="option", side="buy" if o.group(1).lower() == "purchased" else "sell",
                         her_qty=int(o.group(2).replace(",", "")), right=o.group(3).lower(),
                         strike=float(o.group(4).replace(",", "")), expiry=str(parse_date(o.group(5))))
        elif (e := EXERCISE.search(d)):
            trade.update(kind="exercise", side="exercise", her_qty=int(e.group(1).replace(",", "")),
                         right=e.group(2).lower(), strike=float(e.group(3).replace(",", "")),
                         expiry=str(parse_date(e.group(4))))
        elif (s := SHARES.search(d)):
            trade.update(kind="stock", side="buy" if s.group(1).lower() == "purchased" else "sell",
                         her_qty=int(s.group(2).replace(",", "")))
        elif tx_type == "P" and trade["asset_type"] == "ST":
            trade.update(kind="stock", side="buy")
        elif tx_type.startswith("S") and trade["asset_type"] == "ST" and "contribution" not in d.lower() \
                and "gift" not in d.lower():
            trade.update(kind="stock", side="sell")
        else:
            trade.update(kind="skip")
        trade["partial"] = tx_type == "S (partial)"
        trades.append(trade)
    return trades


def ptr_trades(filing):
    pdf = fetch(f"{HOUSE}/ptr-pdfs/{filing['year']}/{filing['doc_id']}.pdf")
    with tempfile.NamedTemporaryFile(suffix=".pdf") as f:
        f.write(pdf)
        f.flush()
        text = subprocess.run(["pdftotext", "-layout", f.name, "-"], capture_output=True,
                              text=True, check=True).stdout
    return parse_ptr(text, filing["doc_id"])


def skip_reason(t):
    if t["kind"] == "skip":
        return "not a market trade (gift/contribution/fund/other)"
    if not t["ticker"]:
        return "no ticker"
    if t["asset_type"] not in COPY_TYPES:
        return f"asset type {t['asset_type']} not copied"
    if t["ticker"] in EXCLUDE:
        return "ticker excluded (managed elsewhere)"
    if t["kind"] in ("option", "exercise") and dt.date.fromisoformat(t["expiry"]) <= dt.date.today():
        return "option already expired"
    return None


# ------------------------------------------------------------ execution

def find_contract(t):
    """Alpaca contract matching her strike/expiry (nearest expiry within 7 days)."""
    exp = dt.date.fromisoformat(t["expiry"])
    q = (f"/v2/options/contracts?underlying_symbols={t['ticker']}&type={t['right']}"
         f"&strike_price_gte={t['strike']}&strike_price_lte={t['strike']}"
         f"&expiration_date_gte={exp - dt.timedelta(days=7)}&expiration_date_lte={exp + dt.timedelta(days=7)}"
         f"&status=active&limit=100")
    contracts = api("GET", q).get("option_contracts") or []
    if not contracts:
        return None
    return min(contracts, key=lambda c: abs((dt.date.fromisoformat(c["expiration_date"]) - exp).days))["symbol"]


def exposure(state):
    return sum(h["cost"] for h in state["holdings"].values())


def submit(state, t, order, note):
    order["client_order_id"] = f"ct-{t['id']}-{order['side']}"[:48]
    print(("[dry-run] " if DRY_RUN else "") + note)
    if DRY_RUN:
        return {"id": "dry-run", "status": "dry-run"}
    try:
        return api("POST", "/v2/orders", order)
    except RuntimeError as e:
        if "client_order_id must be unique" in str(e):  # placed on an earlier run
            return api("GET", f"/v2/orders:by_client_order_id?client_order_id={order['client_order_id']}")
        raise


def execute(state, t):
    """Try one queued trade. Returns a log entry, or None to keep it queued."""
    key = t["ticker"] if t["kind"] == "stock" else None
    if t["kind"] in ("option", "exercise"):
        key = find_contract(t)
        if not key:
            return {"result": "skipped", "reason": "no matching Alpaca option contract"}
    held = state["holdings"].get(key)

    if t["side"] == "buy":
        if exposure(state) >= MAX_COPY_EXPOSURE:
            return {"result": "skipped", "reason": f"copy exposure cap ${MAX_COPY_EXPOSURE:,.0f} reached"}
        if t["kind"] == "stock":
            px = api("GET", f"/v2/stocks/{key}/trades/latest", base=DATA)["trade"]["p"]
            order = {"symbol": key, "notional": f"{BUDGET_PER_TRADE:.2f}", "side": "buy",
                     "type": "market", "time_in_force": "day"}
            res = submit(state, t, order, f"BUY ${BUDGET_PER_TRADE:,.0f} of {key} @ ~{px}")
            qty, cost = BUDGET_PER_TRADE / px, BUDGET_PER_TRADE
        else:
            quote = api("GET", f"/v1beta1/options/quotes/latest?symbols={key}", base=DATA)["quotes"].get(key)
            ask = float(quote["ap"]) if quote else 0
            if ask <= 0:
                return None  # no live quote yet; retry next run
            per = ask * 100
            if per > MAX_PER_TRADE:
                return {"result": "skipped", "reason": f"one contract costs ${per:,.0f} > ${MAX_PER_TRADE:,.0f}"}
            qty = max(1, math.floor(BUDGET_PER_TRADE / per))
            order = {"symbol": key, "qty": str(qty), "side": "buy", "type": "limit",
                     "limit_price": f"{ask:.2f}", "time_in_force": "day"}
            res = submit(state, t, order, f"BUY {qty} {key} @ {ask:.2f} (she bought {t['her_qty']})")
            cost = qty * per
        h = state["holdings"].setdefault(key, {"qty": 0, "cost": 0.0, "kind": t["kind"]})
        h["qty"] = round(h["qty"] + qty, 6)
        h["cost"] = round(h["cost"] + cost, 2)
        return {"result": "submitted", "symbol": key, "qty": qty, "order_id": res["id"]}

    if t["side"] == "sell":
        if not held or held["qty"] <= 0:
            return {"result": "skipped", "reason": "we hold none of it"}
        pos = api("GET", f"/v2/positions/{key}")
        have = float(pos["qty"]) if pos else 0
        qty = min(held["qty"], have)
        if t["partial"]:
            qty = qty / 2 if t["kind"] == "stock" else max(1, math.floor(qty / 2))
        if qty <= 0:
            return {"result": "skipped", "reason": "position already closed"}
        order = {"symbol": key, "qty": f"{qty:.6f}".rstrip("0").rstrip("."), "side": "sell",
                 "type": "market", "time_in_force": "day"}
        res = submit(state, t, order, f"SELL {order['qty']} {key} ({'partial' if t['partial'] else 'full'} sale)")
        frac = qty / held["qty"]
        held["cost"] = round(held["cost"] * (1 - frac), 2)
        held["qty"] = round(held["qty"] - qty, 6)
        return {"result": "submitted", "symbol": key, "qty": qty, "order_id": res["id"]}

    if t["side"] == "exercise":
        if not held or held["qty"] <= 0:
            return {"result": "skipped", "reason": "we hold none of that contract"}
        print(("[dry-run] " if DRY_RUN else "") + f"EXERCISE {key}")
        if not DRY_RUN:
            api("POST", f"/v2/positions/{key}/exercise")
        state["holdings"].pop(key)
        return {"result": "exercised", "symbol": key}


def snapshot(state):
    rows = []
    for sym, h in state["holdings"].items():
        if h["qty"] <= 0:
            continue
        pos = api("GET", f"/v2/positions/{sym}")
        if pos:
            rows.append((sym, float(pos["market_value"]), float(pos["cost_basis"]), float(pos["unrealized_pl"])))
    mv, cb, pl = (sum(r[i] for r in rows) for i in (1, 2, 3))
    new = not os.path.exists(PERF_PATH)
    with open(PERF_PATH, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["timestamp", "positions", "market_value", "cost_basis", "unrealized_pl", "unrealized_pct"])
        w.writerow([dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes"), len(rows),
                    f"{mv:.2f}", f"{cb:.2f}", f"{pl:.2f}", f"{(pl / cb * 100) if cb else 0:.2f}"])
    for sym, m, c, p in rows:
        print(f"  {sym:24} value ${m:>10,.2f}  cost ${c:>10,.2f}  P/L ${p:>+10,.2f}")
    print(f"Copy portfolio: {len(rows)} positions, value ${mv:,.2f}, cost ${cb:,.2f}, P/L ${pl:+,.2f}")


# ------------------------------------------------------------------ main

def main():
    have_creds = load_env()
    state = load_state()
    first_run = not state["seen_filings"]
    today = dt.date.today()

    filings = []
    for year in sorted({today.year - 1, today.year}):
        try:
            filings += list_filings(year)
        except urllib.error.HTTPError:
            pass
    new = [f for f in filings if f["doc_id"] not in state["seen_filings"]]
    print(f"{TARGET['first']} {TARGET['last']}: {len(filings)} PTRs on file, {len(new)} new")

    for f in sorted(new, key=lambda f: f["filed"]):
        recent = (today - dt.date.fromisoformat(f["filed"])).days <= BOOTSTRAP_DAYS
        if first_run and not recent:
            state["seen_filings"].append(f["doc_id"])
            continue
        trades = ptr_trades(f)
        print(f"Filing {f['doc_id']} (filed {f['filed']}): {len(trades)} transactions")
        for t in trades:
            reason = skip_reason(t)
            if reason:
                state["log"].append({**t, "result": "skipped", "reason": reason})
                print(f"  skip {t['ticker']} {t['tx_type']}: {reason}")
            else:
                state["queue"].append(t)
                print(f"  queue {t['side']} {t['ticker']} {t['kind']}: {t['description']}")
        state["seen_filings"].append(f["doc_id"])

    if not have_creds:
        print("Missing Alpaca credentials (APCA_API_KEY_ID / APCA_API_SECRET_KEY): queued only, no orders.")
        if not DRY_RUN:
            save_state(state)
        return

    if state["queue"]:
        clock = api("GET", "/v2/clock")
        if not clock["is_open"]:
            print(f"Market closed; {len(state['queue'])} trade(s) queued until {clock['next_open']}")
        else:
            # Oldest first so a buy is placed before a later sale of the same thing.
            remaining = []
            for t in sorted(state["queue"], key=lambda t: (t["tx_date"], t["id"])):
                try:
                    entry = execute(state, t)
                except RuntimeError as e:
                    entry = {"result": "error", "reason": str(e)[:300]}
                if entry is None:
                    remaining.append(t)
                    continue
                state["log"].append({**t, **entry, "at": dt.datetime.now(dt.timezone.utc).isoformat()})
                if entry["result"] != "submitted" and entry["result"] != "exercised":
                    print(f"  {t['ticker']} {t['side']}: {entry['result']} - {entry.get('reason')}")
            state["queue"] = remaining

    snapshot(state)
    if not DRY_RUN:
        save_state(state)


if __name__ == "__main__":
    main()
