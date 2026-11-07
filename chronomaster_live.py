#!/usr/bin/env python3
"""
CHRONOMASTER TIME KEEPER BOT - LIVE
Calibrated for BTC - utilizing a proprietary forecasting PPT Engine

Designed by: SMS Software Group 2026
By: $HMBRGL on SOLANA - Giving back to the community
hamburgler.org

License: free to use and modify. No resale or rebranding.

"Then Jesus answered, and said unto them, Give to Caesar the things that are Caesar's, and to God, those that are
God's: and they marvelled at him." -- Mark 12:17 (Geneva Bible)

------------------------------------------------------------------------------------------------------------------------
chronomaster_live.py  -  Chronomaster Time Keeper Bot (LIVE)  -  VERSION 1.5  (2026-11-01)

LIVE: this script places REAL orders with REAL money on your Kraken account (BTC, ETH, SOL, XRP vs USD).

COMMANDS (from this folder)
  python chronomaster_live.py check     keys, balance, pair rules, a validate-only order. Places NOTHING.
  python chronomaster_live.py live      starts trading (type LIVE to confirm). Ctrl+C stops it.
  python chronomaster_live.py status    shows the state file and balances. Places nothing.

KEY FILE  config/kraken_keys.json :  {"api_key": "...", "api_secret": "..."}
  Kraken key permissions needed: Query Funds, Query Open Orders & Trades, Query Closed Orders & Trades,
  Create & Modify Orders, Cancel & Close Orders.  NEVER: Withdraw Funds.  Use a NEW key just for this bot.
Only Python's standard library is used.
"""
import base64, csv, hashlib, hmac, http.client, json, os, sys, time, urllib.error, urllib.parse, urllib.request


VERSION = "1.5"
TITLE = "CHRONOMASTER TIME KEEPER BOT - LIVE"
CHAINS = {
    "BTC": {"pair": "XBTUSD", "slots": 2, "cap": 210.0},
    "ETH": {"pair": "ETHUSD", "slots": 1, "cap": 110.0},
    "SOL": {"pair": "SOLUSD", "slots": 1, "cap": 110.0},
    "XRP": {"pair": "XRPUSD", "slots": 1, "cap": 110.0},
}
FEE_PAIR = "XBTUSD"
API = "https://api.kraken.com"
HERE = os.path.dirname(os.path.abspath(__file__))
KEY_F = os.path.join(HERE, "config", "kraken_keys.json")
STATE_F = os.path.join(HERE, "chronomaster_live_state.json")
LOG_F = os.path.join(HERE, "chronomaster_live.log")
TRADES_F = os.path.join(HERE, "chronomaster_live_trades.csv")
PAL_F = os.path.join(HERE, "chronomaster_live_pal_accuracy.csv")
ORDERS_F = os.path.join(HERE, "chronomaster_live_orders.jsonl")
LOCK_F = os.path.join(HERE, "chronomaster_live.lock")
LOCK_FRESH_S = 180

POLL_S = 60
CLIP = 100.0
GAP_SLOT2 = 0.10             # slot 2 only 10% or more below slot 1 (BTC, the only chain with 2 slots)
MAX_BUY_USD = 110.0
BOUNCE = 0.005
RANGE_POS_MAX = 0.35
RANGE_BARS = 168
SMA_DAYS = 100
PAL_AXIS_HOURS = 24
PAL_CAP = 0.05
MIN_PROFIT = 0.006          # floor of the minimum target; raised to 2 x maker fee + 0.30% when higher
FEE_MARGIN = 0.003
BUY_TRIES = 3
SELL_SLOW_AFTER, SELL_SLOW_S = 3, 900
SHOCK_DROP, SHOCK_LOOKBACK_H, SHOCK_PAUSE_H = 0.04, 24, 24
FEE = {"maker": None, "day": None}
PAL_HALF_CYCLE_H = 22.0
SIZE_A, SIZE_K = 7.6, 1.34
BUY_WAIT_S = 3600
USERREF_BASE = 77000000      # this bot's userref range (orders of other bots are never touched)

TRADE_HDR = ["coin", "slot", "buy_time_utc", "buy_avg", "qty", "buy_cost", "buy_fee", "sell_time_utc", "sell_avg",
             "sell_cost", "sell_fee", "hold_h", "deepest_dip_pct", "highest_pct", "net_usd", "buy_txid", "sell_txid"]
PAL_HDR = ["coin", "slot", "floor_time_utc", "floor_px", "axis_px", "buy_time_utc", "buy_avg", "target_px", "target_pct",
           "capped", "pred_arrival_22h_utc", "pred_arrival_size_utc", "swing_pct", "sell_time_utc", "time_miss_22h_h",
           "time_miss_size_h", "hold_h", "deepest_dip_pct", "phi_max", "net_usd"]
STATE = None


class NetError(Exception):
    """No clear answer from Kraken: the outcome is unknown."""


class LiveError(Exception):
    """Kraken refused the request."""

    def __init__(self, endpoint, errors):
        self.errors = [str(e) for e in (errors or [])]
        Exception.__init__(self, f"{endpoint}: {'; '.join(self.errors)}")

    def has(self, *needles):
        t = " ".join(self.errors).lower()
        return any(n.lower() in t for n in needles)


# ---------------------------------------------------------------- helpers
def now():
    return time.time()


def ts(u=None):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(now() if u is None else u))


def log(msg):
    line = f"[{ts()}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_F, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def clog(coin, msg):
    """A log line that starts with the chain."""
    log(f"[{coin}] {msg}")


def append_csv(path, hdr, row):
    new = not os.path.exists(path)
    try:
        with open(path, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if new:
                w.writerow(hdr)
            w.writerow(row)
    except OSError as e:
        print(f"  (could not write {os.path.basename(path)}: {e})")


def migrate_csv(path, hdr):
    """Old single-chain file -> copy kept, rewritten with the coin column first (old rows = BTC)."""
    if not os.path.exists(path):
        return
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.reader(f))
    if not rows or (rows[0] and rows[0][0] == "coin"):
        return
    pre = path[:-4] + "_pre_v1_3.csv"
    if not os.path.exists(pre):
        with open(pre, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerows(rows)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(hdr)
        for r in rows[1:]:
            if r:
                w.writerow(["BTC"] + r)
    log(f"FILES: {os.path.basename(path)} now has a coin column ({len(rows) - 1} earlier rows marked BTC); "
        f"old copy kept as {os.path.basename(pre)}")


def audit(rec):
    try:
        with open(ORDERS_F, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
    except OSError:
        pass


def fnum(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


# red / green direction colors on the screen (the log file stays plain text)
GREEN, RED, RESET = "\033[32m", "\033[31m", "\033[0m"
_PREV_UNR = {}


def colors_on():
    if os.name == "nt":
        os.system("")                     # switches on color codes in the Windows terminal


def cdir(key, val, txt):
    """green if val went up since the last status line, red if it went down; plain if unchanged or first line."""
    v = round(val, 2)
    prev = _PREV_UNR.get(key)
    _PREV_UNR[key] = v
    if prev is None or v == prev:
        return txt
    return (GREEN if v > prev else RED) + txt + RESET


def fpx(coin, x):
    """A price with that pair's decimals."""
    return f"{(x or 0):,.{PI.get(coin, {}).get('pdec', 2)}f}"


# ---------------------------------------------------------------- Kraken API
_KEYS = {}
_NONCE = [0]
_LAST_PUB = [0.0]


def load_keys():
    with open(KEY_F, encoding="utf-8") as f:
        k = json.load(f)
    if not k.get("api_key") or not k.get("api_secret"):
        raise SystemExit(f"{KEY_F} needs api_key and api_secret")
    _KEYS.update(k)


def pub(method, params=None):
    wait = 0.35 - (time.time() - _LAST_PUB[0])
    if wait > 0:
        time.sleep(wait)
    _LAST_PUB[0] = time.time()
    q = urllib.parse.urlencode(params or {})
    req = urllib.request.Request(f"{API}/0/public/{method}" + (f"?{q}" if q else ""),
                                 headers={"User-Agent": f"chronomaster_live/{VERSION}"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            d = json.loads(r.read().decode())
    except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as e:
        raise NetError(f"{method}: {e}")
    if d.get("error"):
        raise LiveError(method, d["error"])
    return d["result"]


def nonce():
    n = int(time.time() * 1000)
    if n <= _NONCE[0]:
        n = _NONCE[0] + 1
    _NONCE[0] = n
    return str(n)


def priv(endpoint, data=None):
    """One signed request. NetError = outcome unknown; LiveError = refused. Never retried here."""
    path = "/0/private/" + endpoint
    d = dict(data or {})
    d["nonce"] = nonce()
    post = urllib.parse.urlencode(d)
    msg = path.encode() + hashlib.sha256((d["nonce"] + post).encode()).digest()
    sig = base64.b64encode(hmac.new(base64.b64decode(_KEYS["api_secret"]), msg, hashlib.sha512).digest()).decode()
    req = urllib.request.Request(API + path, data=post.encode(),
                                 headers={"API-Key": _KEYS["api_key"], "API-Sign": sig,
                                          "User-Agent": f"chronomaster_live/{VERSION}"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            p = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise NetError(f"{endpoint}: HTTP {e.code}")
    except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as e:
        raise NetError(f"{endpoint}: {e}")
    if p.get("error"):
        raise LiveError(endpoint, p["error"])
    return p.get("result") or {}


def priv_read(endpoint, data=None):
    """Read-only call: safe to repeat."""
    last = None
    for i in range(3):
        try:
            return priv(endpoint, data)
        except NetError as e:
            last = e
        except LiveError as e:
            if not e.has("busy", "unavailable", "internal", "service"):
                raise
            last = e
        time.sleep(2 * (i + 1))
    raise NetError(str(last))


PI = {}                                                         # pair rules per chain


def pair_info(coin):
    r = pub("AssetPairs", {"pair": CHAINS[coin]["pair"]})
    k = next(iter(r))
    p = r[k]
    PI[coin] = {"key": k, "pdec": int(p["pair_decimals"]), "ldec": int(p["lot_decimals"]),
                "ordermin": fnum(p.get("ordermin")), "costmin": fnum(p.get("costmin")),
                "base": p.get("base"), "quote": p.get("quote")}


def free_bal(coin):
    """(free USD, free coin) = balance minus what is held in open orders."""
    r = priv_read("BalanceEx")
    def fr(*codes):
        for c in codes:
            if c in r:
                return fnum(r[c].get("balance")) - fnum(r[c].get("hold_trade"))
        return 0.0
    base = PI[coin].get("base") or coin
    alt = {"XXBT": "XBT", "XETH": "ETH", "XXRP": "XRP"}.get(base, base)
    return fr("ZUSD", "USD"), fr(base, alt)


def min_profit():
    """Minimum target over the buy price."""
    if FEE["maker"] is None:
        return MIN_PROFIT
    return max(MIN_PROFIT, 2 * FEE["maker"] / 100.0 + FEE_MARGIN)


def fee_tick(force=False):
    """BTC/USD maker fee from Kraken, at start and once a day (used for every chain)."""
    day = int(now() // 86400)
    if not force and FEE["day"] == day:
        return
    FEE["day"] = day
    try:
        r = priv_read("TradeVolume", {"pair": FEE_PAIR})
        fm = r.get("fees_maker") or {}
        f = fm.get(PI.get("BTC", {}).get("key")) or (next(iter(fm.values())) if fm else None)
        FEE["maker"] = fnum(f.get("fee")) if f else None
        log(f"FEE: BTC/USD maker fee {FEE['maker']:.2f}% each way, 30-day volume ${fnum(r.get('volume')):,.0f}; "
            f"minimum target +{min_profit() * 100:.2f}% on every chain" if FEE["maker"] is not None
            else "FEE: not shown by Kraken; minimum target +0.60%")
    except (NetError, LiveError) as e:
        log(f"FEE: could not read the fee tier ({e}); minimum target stays +{min_profit() * 100:.2f}%")


def lock_take():
    """Refuse a second copy."""
    if os.path.exists(LOCK_F) and now() - os.path.getmtime(LOCK_F) < LOCK_FRESH_S:
        raise SystemExit(f"Another copy looks like it is running ({os.path.basename(LOCK_F)} updated in the last 3 min). "
                         f"Stop it, wait 3 minutes, then start again.")
    lock_touch()


def lock_touch():
    try:
        with open(LOCK_F, "w") as f:
            f.write(f"{os.getpid()} {ts()}\n")
    except OSError:
        pass


def floor_vol(coin, v):
    q = 10 ** PI[coin]["ldec"]
    return int(v * q + 1e-9) / q


def add_order(coin, side, price, vol, ref, validate=False):
    P = PI[coin]
    data = {"pair": CHAINS[coin]["pair"], "type": side, "ordertype": "limit", "price": f"{price:.{P['pdec']}f}",
            "volume": f"{vol:.{P['ldec']}f}", "oflags": "post,fciq", "userref": str(int(ref))}
    if validate:
        data["validate"] = "true"
    rec = {"ts": ts(), "coin": coin, "side": side, "price": data["price"], "vol": data["volume"], "ref": ref,
           "validate": validate}
    if side == "buy" and price * vol > MAX_BUY_USD + 1e-6:
        rec["outcome"] = "REFUSED (over the buy ceiling)"
        audit(rec)
        raise LiveError("guard", [f"buy of ${price * vol:.2f} is over the ${MAX_BUY_USD:.0f} ceiling"])
    if vol < P["ordermin"] or price * vol < P["costmin"]:
        rec["outcome"] = "REFUSED (under the pair minimum)"
        audit(rec)
        raise LiveError("guard", [f"order {vol} {coin} (${price * vol:.2f}) is under Kraken's minimum"])
    try:
        res = priv("AddOrder", data)
    except (NetError, LiveError) as e:
        rec["outcome"] = f"ERROR {e}"
        audit(rec)
        raise
    tx = (res.get("txid") or [None])[0]
    rec["outcome"], rec["txid"] = ("VALIDATED" if validate else "SENT"), tx
    audit(rec)
    return tx


def query(txid):
    return priv_read("QueryOrders", {"txid": txid}).get(txid) or {}


def cancel(txid):
    try:
        priv("CancelOrder", {"txid": txid})
    except LiveError as e:
        if not e.has("unknown order"):
            raise


def find_by_ref(ref):
    """An order whose answer was lost: look it up by userref (open, then closed)."""
    for ep in ("OpenOrders", "ClosedOrders"):
        r = priv_read(ep, {"userref": str(int(ref))})
        o = r.get("open" if ep == "OpenOrders" else "closed") or {}
        if o:
            return sorted(o.items(), key=lambda kv: fnum(kv[1].get("opentm")))[-1][0]
    return None


# ---------------------------------------------------------------- market data (public)
class Market:
    def __init__(self, coin):
        self.coin = coin                                        # one Market per chain
        self.pair = CHAINS[coin]["pair"]
        self.bid = self.ask = self.last = None
        self.cursor = None
        self.lo = self.hi = None
        self.hourly = []
        self.last_closed_h = None
        self.sma = None
        self.sma_day = None

    def tick(self):
        day = int(now() // 86400)
        if self.sma_day != day:
            r = pub("OHLC", {"pair": self.pair, "interval": 1440})
            closes = [fnum(c[4]) for c in r[next(k for k in r if k != "last")]][:-1]
            self.sma = sum(closes[-SMA_DAYS:]) / SMA_DAYS if len(closes) >= SMA_DAYS else None
            self.sma_day = day
        r = pub("Ticker", {"pair": self.pair})
        t = r[next(iter(r))]
        self.ask, self.bid, self.last = fnum(t["a"][0]), fnum(t["b"][0]), fnum(t["c"][0])
        lo = hi = None
        if self.cursor is None:
            self.cursor = pub("Trades", {"pair": self.pair})["last"]
        else:
            for _ in range(10):
                r = pub("Trades", {"pair": self.pair, "since": self.cursor})
                tr = r[next(k for k in r if k != "last")]
                for x in tr:
                    p = fnum(x[0])
                    lo = p if lo is None else min(lo, p)
                    hi = p if hi is None else max(hi, p)
                self.cursor = r["last"]
                if len(tr) < 1000:
                    break
                time.sleep(1.1)
        self.lo, self.hi = lo, hi
        r = pub("OHLC", {"pair": self.pair, "interval": 60})
        rows = [(int(c[0]), fnum(c[1]), fnum(c[2]), fnum(c[3]), fnum(c[4])) for c in r[next(k for k in r if k != "last")]]
        closed = rows[:-1]
        self.hourly = closed[-(RANGE_BARS + 5):]
        newest = closed[-1] if closed else None
        if newest and newest[0] != self.last_closed_h:
            first = self.last_closed_h is None
            self.last_closed_h = newest[0]
            return None if first else newest
        return None

    def pos_7d(self, c):
        w = self.hourly[-(RANGE_BARS + 1):]
        if not w:
            return None
        hh, ll = max(x[2] for x in w), min(x[3] for x in w)
        return (c - ll) / (hh - ll) if hh > ll else 0.5


# ---------------------------------------------------------------- state
def new_chain():
    return {"low": None, "low_t": None, "pending": None, "open": [],
            "banked": 0.0, "sells": 0, "buys": 0, "missed": 0, "sell_fail_run": 0, "halt": ""}


def new_state():
    return {"version": VERSION, "started": ts(), "ref_n": 0, "coins": {c: new_chain() for c in CHAINS}}


def load_state():
    if not os.path.exists(STATE_F):
        return new_state()
    with open(STATE_F, encoding="utf-8") as f:
        g = json.load(f)
    if "coins" not in g:                                        # the old BTC-only state moves under BTC
        pre = STATE_F[:-5] + "_pre_v1_3.json"
        if not os.path.exists(pre):
            with open(pre, "w", encoding="utf-8") as f:
                json.dump(g, f, indent=1)
        btc = {k: v for k, v in g.items() if k not in ("version", "started", "ref_n")}
        g = {"version": VERSION, "started": g.get("started") or ts(), "ref_n": g.get("ref_n", 0), "coins": {"BTC": btc}}
        log(f"STATE: the BTC state moved under coins -> BTC unchanged ({len(btc.get('open', []))} open trade(s)); "
            f"old copy kept as {os.path.basename(pre)}")
    for c in CHAINS:
        g["coins"].setdefault(c, new_chain())
    g["version"] = VERSION
    return g


def save_state(g):
    tmp = STATE_F + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(g, f, indent=1)
    os.replace(tmp, STATE_F)


def next_ref(g):
    g["ref_n"] += 1                                             # one sequence for all chains
    return USERREF_BASE + g["ref_n"]


def size_arrival(floor_t, floor, tgt):
    sw = max(tgt / floor - 1.0, 1e-6) * 100.0
    return floor_t + SIZE_A * sw ** SIZE_K * 3600.0, sw


# ---------------------------------------------------------------- trading
def place_buy(g, coin, s, m, slot, tgt, pal, tries=1, sig_t=None):
    px = m.bid
    vol = floor_vol(coin, CLIP / px)
    fu, _ = free_bal(coin)
    deployed = sum(p["cost"] + p["fee"] for p in s["open"])
    cap = CHAINS[coin]["cap"]
    if px * vol > fu:
        clog(coin, f"SKIP BUY: free USD ${fu:,.2f} is under the ${px * vol:,.2f} buy")
        return
    if deployed + px * vol > cap:
        clog(coin, f"SKIP BUY: ${deployed:,.2f} already in trades; another ${px * vol:,.2f} would pass ${cap:.0f}")
        return
    ref = next_ref(g)
    s["pending"] = {"slot": slot, "px": px, "vol": vol, "t": now(), "ref": ref, "txid": None, "tgt": tgt, "pal": pal,
                    "tries": tries, "sig_t": sig_t or now()}
    save_state(g)
    try:
        s["pending"]["txid"] = add_order(coin, "buy", px, vol, ref)
    except NetError as e:
        clog(coin, f"BUY slot {slot}: no clear answer from Kraken ({e}); will look it up by its reference")
    except LiveError as e:
        s["pending"] = None
        clog(coin, f"BUY REFUSED slot {slot} @ {fpx(coin, px)}: {e}" + (" (price moved; next hour)" if e.has("post only") else ""))
    save_state(g)
    if s["pending"] and s["pending"]["txid"]:
        clog(coin, f"BUY PLACED slot {slot}  {vol} {coin} @ {fpx(coin, px)} (${px * vol:,.2f})  txid {s['pending']['txid']}  "
             f"sell target {fpx(coin, tgt)}" + (f"  (try {tries} of {BUY_TRIES})" if tries > 1 else ""))


def book_fill(coin, s, pb, info):
    vx, cost, fee = fnum(info.get("vol_exec")), fnum(info.get("cost")), fnum(info.get("fee"))
    s["pending"] = None
    if vx <= 0:
        return
    avg = cost / vx
    pos = {"slot": pb["slot"], "avg": avg, "vol": vx, "cost": cost, "fee": fee, "t": fnum(info.get("closetm")) or now(),
           "tgt": pb["tgt"], "pal": pb["pal"], "buy_txid": pb["txid"], "sell_txid": None, "sell_ref": None,
           "part_vol": 0.0, "part_cost": 0.0, "part_fee": 0.0, "min": avg, "max": avg, "max_t": now(), "dust": False}
    if vx < PI[coin]["ordermin"] or vx * pb["tgt"] < PI[coin]["costmin"]:
        pos["dust"] = True
        clog(coin, f"BUY FILLED slot {pb['slot']} only {vx} {coin} (${cost:,.2f}): under Kraken's minimum to sell; "
             f"left in the account")
        return
    s["open"].append(pos)
    s["buys"] += 1
    s["low"], s["low_t"] = avg, now()
    clog(coin, f"BUY FILLED slot {pos['slot']}  {vx} {coin} @ avg {fpx(coin, avg)}  cost ${cost:,.2f} + fee ${fee:,.4f}  "
         f"-> sell at {fpx(coin, pos['tgt'])}")


def check_pending(g, coin, s, m=None):
    pb = s["pending"]
    if not pb:
        return
    if not pb.get("txid"):
        tx = find_by_ref(pb["ref"])
        if tx:
            pb["txid"] = tx
            clog(coin, f"RECOVERED buy order {tx} (found by reference {pb['ref']})")
        elif now() - pb["t"] > 90:
            clog(coin, f"BUY slot {pb['slot']}: no order found on Kraken after 90 s; treated as never placed")
            s["pending"] = None
        return
    info = query(pb["txid"])
    st = info.get("status")
    if st == "closed":
        book_fill(coin, s, pb, info)
    elif st in ("canceled", "expired"):
        if fnum(info.get("vol_exec")) > 0:
            book_fill(coin, s, pb, info)
        else:
            s["pending"] = None
            clog(coin, f"BUY slot {pb['slot']} {st} on Kraken with no fill")
            buy_retry(g, coin, s, m, pb)
    elif now() - pb["t"] >= BUY_WAIT_S:
        cancel(pb["txid"])
        time.sleep(1.5)
        info = query(pb["txid"])
        if info.get("status") in ("open", "pending"):
            return
        if fnum(info.get("vol_exec")) > 0:
            book_fill(coin, s, pb, info)
        else:
            s["pending"] = None
            s["missed"] += 1
            clog(coin, f"MISSED BUY slot {pb['slot']} @ {fpx(coin, pb['px'])}: not filled in 60 min; canceled")


def buy_retry(g, coin, s, m, pb):
    """Kraken canceled the buy with no fill -> try again at the new bid while the signal still holds."""
    tries = pb.get("tries", 1)
    sig_t = pb.get("sig_t", pb["t"])
    why = None
    if m is None or not m.bid:
        why = "no fresh price"
    elif tries >= BUY_TRIES:
        why = f"{BUY_TRIES} tries used"
    elif now() - sig_t >= BUY_WAIT_S:
        why = "the signal's 60 minutes are over"
    elif s.get("halt") or len(s["open"]) >= CHAINS[coin]["slots"]:
        why = "no free slot"
    elif shock_on(s):
        why = f"aftershock pause until {ts(s['shock_until'])} UTC"
    elif m.sma is None or m.bid < m.sma:
        why = f"{coin} is under the 100-day average ({fpx(coin, m.sma)})"
    elif pb["tgt"] < m.bid * (1 + min_profit()):
        why = f"target {fpx(coin, pb['tgt'])} is now under +{min_profit() * 100:.2f}% over the bid {fpx(coin, m.bid)}"
    elif s["open"] and m.bid > s["open"][0]["avg"] * (1 - GAP_SLOT2):
        why = "slot 2 spacing no longer met"
    if why:
        s["missed"] += 1
        clog(coin, f"MISSED BUY slot {pb['slot']}: no retry ({why})")
        return
    clog(coin, f"BUY RETRY slot {pb['slot']}: placing again at the new bid {fpx(coin, m.bid)} (try {tries + 1} of {BUY_TRIES})")
    place_buy(g, coin, s, m, pb["slot"], pb["tgt"], pb["pal"], tries + 1, sig_t)


def place_sell(g, coin, s, pos, m):
    if now() < pos.get("sell_retry_ts", 0):
        return
    left = floor_vol(coin, pos["vol"] - pos["part_vol"])
    _, fb = free_bal(coin)
    qty = floor_vol(coin, min(left, fb))
    if qty < left:
        clog(coin, f"SELL slot {pos['slot']}: Kraken shows {fb} {coin} free, this trade has {left}; selling {qty}")
    price = pos["tgt"]
    if m.ask and m.ask > price:
        price = m.ask
        clog(coin, f"SELL slot {pos['slot']}: market already above the target {fpx(coin, pos['tgt'])}; "
             f"resting at the ask {fpx(coin, price)}")
    ref = next_ref(g)
    pos["sell_ref"] = ref
    save_state(g)
    try:
        pos["sell_txid"] = add_order(coin, "sell", price, qty, ref)
        pos["sell_px"] = price
        s["sell_fail_run"] = 0
        pos["sell_fails"] = 0
        clog(coin, f"SELL PLACED slot {pos['slot']}  {qty} {coin} @ {fpx(coin, price)}  txid {pos['sell_txid']}  "
             f"({(price / pos['avg'] - 1) * 100:+.2f}% over the buy)")
    except NetError as e:
        pos["sell_lost"] = now()
        clog(coin, f"SELL slot {pos['slot']}: no clear answer from Kraken ({e}); will look it up by its reference")
    except LiveError as e:
        if e.has("post only") and m.ask:
            clog(coin, f"SELL slot {pos['slot']}: post-only refused (market at the price); trying again next minute")
        else:
            s["sell_fail_run"] += 1
            pos["sell_fails"] = pos.get("sell_fails", 0) + 1
            if pos["sell_fails"] >= SELL_SLOW_AFTER:
                pos["sell_retry_ts"] = now() + SELL_SLOW_S
            clog(coin, f"SELL REFUSED slot {pos['slot']} (#{s['sell_fail_run']}): {e}"
                 + (" (next try in 15 min)" if pos["sell_fails"] >= SELL_SLOW_AFTER else ""))
            if s["sell_fail_run"] >= 5:
                s["halt"] = f"sells keep failing: {e}"
                clog(coin, "NEW BUYS STOPPED on this chain: 5 failed sells in a row. Check the account, then restart.")
    save_state(g)


def check_open(g, coin, s, m):
    keep = []
    for pos in s["open"]:
        if m.lo is not None and m.lo < pos["min"]:
            pos["min"] = m.lo
        if m.hi is not None and m.hi > pos["max"]:
            pos["max"], pos["max_t"] = m.hi, now()
        pal = pos["pal"]
        span = pos["tgt"] - pal["floor"]
        if m.hi is not None and span > 0:
            phi = (m.hi - pal["floor"]) / span
            pal["phi_max"] = max(pal.get("phi_max", 0.0), phi)
            if phi >= 0.5 and not pal.get("axis_t"):
                pal["axis_t"] = now()
        if not pos.get("sell_txid"):
            if pos.get("sell_lost"):
                tx = find_by_ref(pos["sell_ref"])
                if tx:
                    pos["sell_txid"] = tx
                    pos.pop("sell_lost", None)
                    clog(coin, f"RECOVERED sell order {tx} (found by reference {pos['sell_ref']})")
                elif now() - pos["sell_lost"] > 90:
                    pos.pop("sell_lost", None)
                keep.append(pos)
                continue
            place_sell(g, coin, s, pos, m)
            keep.append(pos)
            continue
        info = query(pos["sell_txid"])
        st = info.get("status")
        if st == "closed":
            finish_sale(coin, s, pos, info)
            continue
        if st in ("canceled", "expired"):
            vx = fnum(info.get("vol_exec"))
            if vx > 0:
                pos["part_vol"] += vx
                pos["part_cost"] += fnum(info.get("cost"))
                pos["part_fee"] += fnum(info.get("fee"))
                clog(coin, f"PARTIAL SELL KEPT slot {pos['slot']}: {vx} {coin} sold for ${fnum(info.get('cost')):,.2f} "
                     f"before the order was {st}")
            clog(coin, f"SELL slot {pos['slot']} was {st} outside the bot; placing it again for what is left")
            pos["sell_txid"] = None
        keep.append(pos)
    s["open"] = keep


def finish_sale(coin, s, pos, info):
    t = fnum(info.get("closetm")) or now()
    sc = fnum(info.get("cost")) + pos["part_cost"]
    sf = fnum(info.get("fee")) + pos["part_fee"]
    sv = fnum(info.get("vol_exec")) + pos["part_vol"]
    net = sc - sf - pos["cost"] - pos["fee"]
    hold_h = (t - pos["t"]) / 3600
    s["banked"] += net
    s["sells"] += 1
    s["low"], s["low_t"] = None, None
    d = PI[coin]["pdec"]
    append_csv(TRADES_F, TRADE_HDR, [
        coin, pos["slot"], ts(pos["t"]), round(pos["avg"], d), pos["vol"], round(pos["cost"], 4), round(pos["fee"], 4), ts(t),
        round(sc / sv, d) if sv else "", round(sc, 4), round(sf, 4), round(hold_h, 2),
        round((pos["min"] / pos["avg"] - 1) * 100, 3), round((pos["max"] / pos["avg"] - 1) * 100, 3), round(net, 4),
        pos["buy_txid"], pos["sell_txid"]])
    pal = pos["pal"]
    ax = pal.get("axis_t")
    append_csv(PAL_F, PAL_HDR, [
        coin, pos["slot"], ts(pal["floor_t"]), round(pal["floor"], d), round(pal["axis"], d), ts(pos["t"]),
        round(pos["avg"], d), round(pos["tgt"], d), round((pos["tgt"] / pos["avg"] - 1) * 100, 3), 1 if pal["capped"] else 0,
        ts(pal["arrival"]), ts(pal["arrival2"]), round(pal["swing"], 3), ts(t), round((t - pal["arrival"]) / 3600, 2),
        round((t - pal["arrival2"]) / 3600, 2), round(hold_h, 2), round((pos["min"] / pos["avg"] - 1) * 100, 3),
        round(max(pal.get("phi_max", 0.0), 1.0), 3), round(net, 4)])
    clog(coin, f"SOLD slot {pos['slot']}  {sv} {coin} for ${sc:,.2f}  held {hold_h:.1f} h  net ${net:+.2f}  | "
         f"banked ${s['banked']:+.2f}" + (f"  (axis crossed after {(ax - pal['floor_t']) / 3600:.1f} h)" if ax else ""))


def shock_check(coin, s, m, bar):
    """A 4% drop vs the highest close of the 24 h before -> no new buys for 24 h (per chain)."""
    prev = [x[4] for x in m.hourly if bar[0] - SHOCK_LOOKBACK_H * 3600 <= x[0] < bar[0]]
    if len(prev) < SHOCK_LOOKBACK_H // 2:
        return
    top = max(prev)
    if bar[4] <= top * (1 - SHOCK_DROP):
        until = now() + SHOCK_PAUSE_H * 3600
        first = not s.get("shock_until") or s["shock_until"] < now()
        s["shock_until"] = until
        clog(coin, f"AFTERSHOCK PAUSE{'' if first else ' (extended)'}: {coin} closed {fpx(coin, bar[4])}, "
             f"{(bar[4] / top - 1) * 100:.2f}% under the 24 h high close {fpx(coin, top)}; no new buys until {ts(until)} UTC")


def shock_on(s):
    return bool(s.get("shock_until")) and now() < s["shock_until"]


def hourly_signal(g, coin, s, m, bar):
    t, o, h, l, c = bar
    if s["low"] is None or l < s["low"]:
        s["low"], s["low_t"] = l, t + 3599
    shock_check(coin, s, m, bar)
    if s.get("halt") or s["pending"] or len(s["open"]) >= CHAINS[coin]["slots"]:
        return
    if c < s["low"] * (1 + BOUNCE):
        return
    if shock_on(s):
        clog(coin, f"SKIP signal: bounce {(c / s['low'] - 1) * 100:.2f}% off {fpx(coin, s['low'])} but aftershock pause "
             f"until {ts(s['shock_until'])} UTC")
        return
    pos = m.pos_7d(c)
    why = None
    if pos is None or pos > RANGE_POS_MAX:
        why = f"not in the bottom 35% of the 7-day range ({(pos or 0) * 100:.0f}%)"
    elif m.sma is None or c < m.sma:
        why = f"under the 100-day average ({fpx(coin, m.sma)})"
    elif s["open"]:
        need = s["open"][0]["avg"] * (1 - GAP_SLOT2)
        if c > need:
            why = f"slot 2 needs {fpx(coin, need)} or lower"
    if why:
        clog(coin, f"SKIP signal: bounce {(c / s['low'] - 1) * 100:.2f}% off {fpx(coin, s['low'])} but {why}")
        return
    closes = [x[4] for x in m.hourly[-PAL_AXIS_HOURS:]]
    axis = sum(closes) / len(closes)
    floor = s["low"]
    raw = 2 * axis - floor
    px = m.bid or c
    if raw < px * (1 + min_profit()):
        clog(coin, f"SKIP signal: palindrome target {fpx(coin, raw)} (axis {fpx(coin, axis)}, floor {fpx(coin, floor)}) "
             f"is under +{min_profit() * 100:.2f}% over {fpx(coin, px)}")
        return
    tgt = min(raw, px * (1 + PAL_CAP))
    floor_t = s.get("low_t") or now()
    arr2, sw = size_arrival(floor_t, floor, tgt)
    pal = {"axis": axis, "floor": floor, "floor_t": floor_t, "capped": raw > tgt,
           "arrival": floor_t + PAL_HALF_CYCLE_H * 3600, "arrival2": arr2, "swing": sw}
    slot = 1 + max([p["slot"] for p in s["open"]], default=0)
    clog(coin, f"SIGNAL slot {slot}: bounce {(c / floor - 1) * 100:.2f}% off {fpx(coin, floor)}, 7-day range "
         f"{pos * 100:.0f}%, 100-day {fpx(coin, m.sma)} | palindrome: axis {fpx(coin, axis)} -> target {fpx(coin, tgt)} "
         f"(+{(tgt / px - 1) * 100:.2f}%{', capped' if raw > tgt else ''}), arrival {ts(pal['arrival'])} UTC (22 h) / "
         f"{ts(arr2)} UTC (size rule)")
    place_buy(g, coin, s, m, slot, tgt, pal)


def status(g, mk):
    """One title line, then one line per chain and one per open trade."""
    tot = sum(cs["banked"] for cs in g["coins"].values())
    print(f"=== {TITLE}  v{VERSION}  |  {ts()} UTC  |  all chains banked ${tot:+.2f}", flush=True)
    for coin in CHAINS:
        s, m = g["coins"][coin], mk[coin]
        if not m.last:
            print(f"   {coin}: no price yet", flush=True)
            continue
        px = m.bid or m.last
        unr = sum(px * (p["vol"] - p["part_vol"]) + p["part_cost"] - p["cost"] - p["fee"] for p in s["open"])
        print(f"   {coin} {fpx(coin, m.last)} (bid {fpx(coin, m.bid)})  100-day {fpx(coin, m.sma)}  |  open "
              f"{len(s['open'])}/{CHAINS[coin]['slots']}  sells {s['sells']}  banked ${s['banked']:+.2f}  unrealized "
              f"{cdir(coin, unr, f'${unr:+.2f}')}"
              + (f"  | buy resting @ {fpx(coin, s['pending']['px'])}" if s["pending"] else "")
              + (f"  | HALTED: {s['halt']}" if s.get("halt") else "")
              + (f"  | AFTERSHOCK until {ts(s['shock_until'])} UTC" if shock_on(s) else ""), flush=True)
        for p in s["open"]:
            span = p["tgt"] - p["pal"]["floor"]
            phi = (px - p["pal"]["floor"]) / span if span > 0 else 0
            print(f"      slot {p['slot']}: {p['vol'] - p['part_vol']:.8f} {coin} bought {fpx(coin, p['avg'])} -> sell "
                  f"{fpx(coin, p.get('sell_px', p['tgt']))}  now {(px / p['avg'] - 1) * 100:+.2f}%  phi {phi:.2f}", flush=True)


# ---------------------------------------------------------------- commands
def cmd_check():
    print(f"{TITLE} v{VERSION} - CHECK (places NOTHING)")
    load_keys()
    print(f"  PASS key file read: {KEY_F}")
    for coin in CHAINS:
        pair_info(coin)
        P = PI[coin]
        print(f"  PASS pair {CHAINS[coin]['pair']}: price decimals {P['pdec']}, {coin} decimals {P['ldec']}, "
              f"min {P['ordermin']} {coin} / ${P['costmin']}")
    fu, fb = free_bal("BTC")
    print(f"  PASS balance read: free USD ${fu:,.2f}, free BTC {fb:.8f}")
    fee_tick(force=True)
    for coin in CHAINS:
        r = pub("Ticker", {"pair": CHAINS[coin]["pair"]})
        bid = fnum(r[next(iter(r))]["b"][0])
        px = round(bid * 0.9, PI[coin]["pdec"])
        vol = floor_vol(coin, CLIP / px)
        add_order(coin, "buy", px, vol, USERREF_BASE, validate=True)
        print(f"  PASS validate-only buy {vol} {coin} @ {fpx(coin, px)} accepted by Kraken (NOT placed)")
    print("  Key permissions look right for trading. Make sure Withdraw is OFF on this key.")
    print(f"  Ready: python {os.path.basename(__file__)} live")


def cmd_status():
    s = load_state()
    print(json.dumps({k: v for k, v in s.items()}, indent=1)[:4000])


LOGO = r"""
                    |
               .----+----.
            .'  ^   ^   ^  '.
           /    |   |   |    \
          |     \___|___/     |        C H R O N O M A S T E R
     -----+---------+---------+-----      S N I P E R   B O T
          |       --+--       |        L I V E  -  BTC  ETH  SOL  XRP
           \        |__      /         proprietary forecasting PPT Engine
            '.      |  \   .'
               '----+--/-'
                    | /
"""


def intro():
    print(LOGO)
    print("Designed by: SMS Software Group 2026   |   By: $HMBRGL on SOLANA - Giving back to the community   |   hamburgler.org")
    print(f"\n{TITLE}  v{VERSION}  -  REAL ORDERS. Book B-pal, ${CLIP:.0f} a trade, no buy over ${MAX_BUY_USD:.0f}:")
    for coin, c in CHAINS.items():
        print(f"   {coin} ({c['pair']}): at most {c['slots']} open, at most ${c['cap']:.0f} in trades")
    print()


def cmd_live():
    global STATE
    colors_on()
    intro()
    load_keys()
    for coin in CHAINS:
        pair_info(coin)
    fu, fb = free_bal("BTC")
    print(f"Kraken: free USD ${fu:,.2f}, free BTC {fb:.8f}")
    if input("Type LIVE to start real trading: ").strip() != "LIVE":
        print("Not started.")
        return
    lock_take()
    g = STATE = load_state()
    migrate_csv(TRADES_F, TRADE_HDR)
    migrate_csv(PAL_F, PAL_HDR)
    for coin in CHAINS:
        s = g["coins"][coin]
        if s.get("halt"):
            clog(coin, f"Previous halt cleared at start ({s['halt']})")
            s["halt"] = ""
            s["sell_fail_run"] = 0
    save_state(g)
    mk = {coin: Market(coin) for coin in CHAINS}
    log(f"START v{VERSION} LIVE: B-pal on " + ", ".join(f"{c} ${CLIP:.0f} x {CHAINS[c]['slots']} "
        f"(open {len(g['coins'][c]['open'])})" for c in CHAINS) + f"; banked ${sum(cs['banked'] for cs in g['coins'].values()):+.2f}")
    while True:
        t0 = now()
        lock_touch()
        fee_tick()
        for coin in CHAINS:                                 # each chain on its own
            s, m = g["coins"][coin], mk[coin]
            try:
                bar = m.tick()
                check_pending(g, coin, s, m)
                check_open(g, coin, s, m)
                if bar:
                    hourly_signal(g, coin, s, m, bar)
            except (NetError, LiveError, KeyError, ValueError, IndexError, StopIteration) as e:
                clog(coin, f"ERROR: {e} - retrying next minute")
            lock_touch()
        try:
            save_state(g)
        except OSError as e:
            log(f"ERROR: could not save the state ({e})")
        status(g, mk)
        time.sleep(max(1.0, POLL_S - (now() - t0)))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        if cmd == "check":
            cmd_check()
        elif cmd == "live":
            cmd_live()
        elif cmd == "status":
            cmd_status()
        else:
            print(__doc__.split("COMMANDS")[1].split("KEY FILE")[0])
    except KeyboardInterrupt:
        print("\nstopping ... (sells stay up on Kraken)")
        if STATE is not None:
            save_state(STATE)
            log("STOP (Ctrl+C) - state saved; resting sells stay on Kraken")
            try:
                os.remove(LOCK_F)
            except OSError:
                pass
    except FileNotFoundError as e:
        print(f"Missing file: {e}. Put your key in config/kraken_keys.json (see the top of this file).")
