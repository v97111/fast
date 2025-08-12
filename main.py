import os
import time
import json
import math
import queue
import threading
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Dict, Any, List, Optional

from flask import Flask, jsonify, request, render_template
from dotenv import load_dotenv

# Optional: python-binance (public endpoints work without keys)
BINANCE_OK = True
try:
    from binance import Client
    from binance.exceptions import BinanceAPIException
except Exception:
    BINANCE_OK = False
    Client = None
    BinanceAPIException = Exception

load_dotenv()

app = Flask(__name__, template_folder="templates")

# =========================
# Config (edit if you want)
# =========================
SCAN_INTERVAL_SEC = float(os.getenv("SCAN_INTERVAL_SEC", "2"))
EMA_PERIOD = 50  # EMA50
VOL_SPIKE_MULT_SAFE = float(os.getenv("VOL_SPIKE_MULT_SAFE", "1.5"))
VOL_SPIKE_MULT_FAST = float(os.getenv("VOL_SPIKE_MULT_FAST", "1.2"))
VOL_SPIKE_MULT_NONEPATTERN = float(os.getenv("VOL_SPIKE_MULT_NONEPATTERN", "1.8"))  # when pattern="none"
TP_PROFIT_PCT = float(os.getenv("TP_PROFIT_PCT", "0.01"))  # 1% take profit
# Safe buy pattern window
SAFE_DROP_PCT = float(os.getenv("SAFE_DROP_PCT", "0.003"))  # 0.3% dip on previous candle
# "Recent high" lookback for rebound checks (fast mode uses smaller)
RECENT_HIGH_LOOKBACK_SAFE = int(os.getenv("RECENT_HIGH_LOOKBACK_SAFE", "50"))  # candles
RECENT_HIGH_LOOKBACK_FAST = int(os.getenv("RECENT_HIGH_LOOKBACK_FAST", "20"))

# When pattern == "none", we tighten other conditions (to avoid mass buys)
NONEPATTERN_24H_MOVE_MIN = float(os.getenv("NONE_24H_MOVE_MIN", "-0.01"))  # -1%
NONEPATTERN_24H_MOVE_MAX = float(os.getenv("NONE_24H_MOVE_MAX", "0.02"))   # +2%

# =========================
# Binance client (public OK)
# =========================
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "")
if BINANCE_OK:
    try:
        client = Client(api_key=BINANCE_API_KEY or None, api_secret=BINANCE_API_SECRET or None)
    except Exception:
        client = None
else:
    client = None


# =========
# State
# =========
@dataclass
class Position:
    symbol: str
    qty: float
    buy_price: float
    ts: str
    worker_id: int


@dataclass
class Worker:
    id: int
    amount: float = 20.0           # USDT to spend
    status: str = "idle"           # idle | scanning | bought
    symbol: Optional[str] = None
    note: str = ""
    started_at: Optional[str] = None
    position: Optional[Position] = None


state_lock = threading.Lock()
running = False
mode = "fast"  # "safe" or "fast"
debug_on = True

# default watchlist (you can paste your big list here)
watchlist_text = "BTCUSDT,ETHUSDT,BNBUSDT,ADAUSDT,SOLUSDT,XRPUSDT,DOGEUSDT,MATICUSDT,AVAXUSDT,DOTUSDT"
workers: Dict[int, Worker] = {}
trades: List[Dict[str, Any]] = []
debug_events: List[Dict[str, Any]] = []
debug_cap = 5000

# Profit tracking
starting_usdt = 1000.0
current_usdt = starting_usdt
assets: Dict[str, Position] = {}  # symbol -> Position (paper trading one per symbol)


# =========
# Helpers
# =========
def utcnow_iso():
    return datetime.now(timezone.utc).isoformat()

def append_debug(ev: Dict[str, Any]):
    global debug_events
    if not debug_on:
        return
    ev2 = {k: v for k, v in ev.items()}
    ev2["time"] = utcnow_iso()
    debug_events.append(ev2)
    if len(debug_events) > debug_cap:
        debug_events = debug_events[-debug_cap:]

def ema(values: List[float], period: int) -> List[float]:
    if not values:
        return []
    k = 2 / (period + 1)
    out = []
    ema_prev = None
    for v in values:
        if ema_prev is None:
            ema_prev = v
        else:
            ema_prev = v * k + ema_prev * (1 - k)
        out.append(ema_prev)
    return out

def get_klines(symbol: str, interval="1m", limit=120) -> List[Dict[str, float]]:
    if not client:
        return []
    try:
        rows = client.get_klines(symbol=symbol, interval=interval, limit=limit)
        out = []
        for r in rows:
            out.append({
                "open_time": r[0],
                "open": float(r[1]),
                "high": float(r[2]),
                "low": float(r[3]),
                "close": float(r[4]),
                "volume": float(r[5]),
                "close_time": r[6]
            })
        return out
    except Exception as e:
        append_debug({"symbol": symbol, "reason": "klines_error", "error": str(e)})
        return []

def get_ticker_24h(symbol: str) -> Dict[str, Any]:
    if not client:
        return {}
    try:
        t = client.get_ticker(symbol=symbol)
        return {
            "lastPrice": float(t["lastPrice"]),
            "priceChangePercent": float(t["priceChangePercent"]) / 100.0,  # -0.05 for -5%
            "highPrice": float(t["highPrice"]),
            "lowPrice": float(t["lowPrice"])
        }
    except Exception as e:
        append_debug({"symbol": symbol, "reason": "ticker_error", "error": str(e)})
        return {}

def recent_high(closes: List[float], lookback: int) -> Optional[float]:
    if not closes:
        return None
    look = closes[-lookback:] if len(closes) >= lookback else closes
    return max(look) if look else None


# ==============================
# Buy logic / Sell logic (paper)
# ==============================
def evaluate_symbol(symbol: str, policy: Dict[str, Any]) -> Dict[str, Any]:
    """
    Returns:
      {
        ok: bool,
        reason: str,
        day_ok, ema_ok, vol_ok, pattern_ok: bool
      }
    """
    candles = get_klines(symbol, "1m", limit=max(EMA_PERIOD + 5, 60))
    if len(candles) < max(EMA_PERIOD, 20):
        return {"ok": False, "reason": "no_data", "day_ok": False, "ema_ok": False, "vol_ok": False, "pattern_ok": False}

    closes = [c["close"] for c in candles]
    vols = [c["volume"] for c in candles]
    last = candles[-1]
    prev = candles[-2]

    # Day filter
    t24 = get_ticker_24h(symbol)
    if not t24:
        return {"ok": False, "reason": "no_ticker", "day_ok": False, "ema_ok": False, "vol_ok": False, "pattern_ok": False}
    day_chg = t24["priceChangePercent"]  # e.g. 0.03 for +3%
    # relaxed but avoids extremes
    day_ok = (-0.06 <= day_chg <= 0.06)

    # Trend (EMA50 up & price at/above EMA)
    e = ema(closes, EMA_PERIOD)
    ema50 = e[-1]
    ema_ok = (last["close"] >= ema50 and ema50 >= e[-2])  # trending up and price not below EMA

    # Volume spike
    avg_vol = sum(vols[-30:-1]) / max(1, len(vols[-30:-1]))
    if policy["pattern"] == "none":
        vol_mult = max(policy["vol_mult"], VOL_SPIKE_MULT_NONEPATTERN)  # stricter if no pattern
    else:
        vol_mult = policy["vol_mult"]
    vol_ok = (last["volume"] >= avg_vol * vol_mult)

    # Pattern
    pattern_ok = False
    if policy["pattern"] == "prev_high":  # SAFE
        if len(candles) >= 2:
            c_prev = candles[-2]
            # prior candle dipped then price recovered above prior high
            dipped = (c_prev["close"] > 0) and ((c_prev["close"] - c_prev["low"]) / c_prev["close"] >= SAFE_DROP_PCT)
            recovered = last["close"] > c_prev["high"]
            pattern_ok = dipped and recovered

    elif policy["pattern"] == "bounce_only":  # FAST
        if len(candles) >= 3:
            c_prev2 = candles[-3]
            c_prev1 = candles[-2]
            # green continuation
            pattern_ok = (c_prev1["close"] > c_prev1["open"]) and (last["close"] >= c_prev1["close"])

    elif policy["pattern"] == "none":
        # No explicit pattern, but tighten other criteria:
        # - last candle must be green
        # - 24h move inside narrow band
        # - (already used stronger vol spike above)
        green = last["close"] > last["open"]
        narrow_day = (NONEPATTERN_24H_MOVE_MIN <= day_chg <= NONEPATTERN_24H_MOVE_MAX)
        pattern_ok = green and narrow_day

    # Additional rebound guard (both modes): last close near recent high (strength)
    lookback = RECENT_HIGH_LOOKBACK_FAST if policy["name"] == "fast" else RECENT_HIGH_LOOKBACK_SAFE
    rh = recent_high(closes, lookback)
    if rh:
        # price should be within 1.5% of recent high to avoid catching weak bounces
        near_recent_high = (last["close"] >= rh * 0.985)
    else:
        near_recent_high = True

    reason = "ok"
    if not day_ok:
        reason = "day_extreme"
    elif not ema_ok:
        reason = "below_ema50"
    elif not vol_ok:
        reason = "no_vol_spike"
    elif not pattern_ok:
        reason = "pattern_miss"
    elif not near_recent_high:
        reason = "far_from_recent_high"

    return {
        "ok": (reason == "ok"),
        "reason": reason,
        "day_ok": day_ok,
        "ema_ok": ema_ok,
        "vol_ok": vol_ok,
        "pattern_ok": pattern_ok
    }


def try_sell(symbol: str, pos: Position) -> Optional[Dict[str, Any]]:
    """Simple TP sell: +1% (configurable)."""
    t24 = get_ticker_24h(symbol)
    if not t24:
        return None
    last_price = t24["lastPrice"]
    tp_price = pos.buy_price * (1.0 + TP_PROFIT_PCT)
    if last_price >= tp_price:
        # Paper trade "sell"
        pnl_pct = (last_price - pos.buy_price) / pos.buy_price
        trade = {
            "time": utcnow_iso(),
            "worker": pos.worker_id,
            "side": "SELL",
            "symbol": symbol,
            "price": last_price,
            "amount": pos.qty * last_price,
            "pnl_pct": pnl_pct * 100.0
        }
        with state_lock:
            global current_usdt
            current_usdt += pos.qty * last_price
            assets.pop(symbol, None)
            trades.append(trade)
        append_debug({"worker_id": pos.worker_id, "symbol": symbol, "reason": "tp_sell", "price": last_price, "pnl_pct": pnl_pct})
        return trade
    return None


def try_buy(worker: Worker, symbol: str, amount_usdt: float) -> Optional[Dict[str, Any]]:
    t24 = get_ticker_24h(symbol)
    if not t24:
        return None
    price = t24["lastPrice"]
    if price <= 0:
        return None
    qty = amount_usdt / price
    pos = Position(symbol=symbol, qty=qty, buy_price=price, ts=utcnow_iso(), worker_id=worker.id)
    with state_lock:
        global current_usdt
        current_usdt -= amount_usdt
        assets[symbol] = pos
        worker.position = pos
        worker.status = "bought"
        worker.symbol = symbol
        worker.note = f"Bought at {price:.4f}"
    trade = {
        "time": utcnow_iso(),
        "worker": worker.id,
        "side": "BUY",
        "symbol": symbol,
        "price": price,
        "amount": amount_usdt,
        "pnl_pct": 0.0
    }
    with state_lock:
        trades.append(trade)
    append_debug({"worker_id": worker.id, "symbol": symbol, "reason": "buy", "price": price})
    return trade


def parse_watchlist(text: str) -> List[str]:
    return [s.strip().upper() for s in text.replace("\n", ",").split(",") if s.strip()]


def profit_snapshot():
    with state_lock:
        assets_val = 0.0
        for sym, pos in assets.items():
            t24 = get_ticker_24h(sym)
            if t24:
                assets_val += pos.qty * t24["lastPrice"]
        cur = current_usdt + assets_val
        start = starting_usdt
        pnl_abs = cur - start
        pnl_pct = (pnl_abs / start * 100.0) if start > 0 else 0.0
        return {"abs": pnl_abs, "pct": pnl_pct, "assets": assets_val, "usdt": current_usdt}


# =====================
# Bot thread
# =====================
stop_event = threading.Event()

def bot_loop():
    global running
    next_worker_id_to_scan = 0
    while not stop_event.is_set():
        time.sleep(SCAN_INTERVAL_SEC)
        with state_lock:
            run = running
            wlist = list(workers.values())
            wl_symbols = parse_watchlist(watchlist_text)
            current_mode = mode
        if not run or not wlist or not wl_symbols:
            continue

        # Mode policy
        if current_mode == "safe":
            policy = {"name": "safe", "pattern": "prev_high", "vol_mult": VOL_SPIKE_MULT_SAFE}
        else:
            # fast
            policy = {"name": "fast", "pattern": "bounce_only", "vol_mult": VOL_SPIKE_MULT_FAST}

        # Rotate through workers, each scans the whole watchlist (lightweight)
        worker = wlist[next_worker_id_to_scan % len(wlist)]
        next_worker_id_to_scan += 1

        # If worker has position, check sell
        if worker.position:
            try_sell(worker.position.symbol, worker.position)
            continue

        # Otherwise scan for buys
        best_candidate = None
        for sym in wl_symbols:
            with state_lock:
                # don’t buy same coin if already held by any worker
                if sym in assets:
                    continue
            res = evaluate_symbol(sym, policy)
            append_debug({
                "worker_id": worker.id,
                "symbol": sym,
                "reason": res["reason"],
                "day_ok": res["day_ok"],
                "ema_ok": res["ema_ok"],
                "vol_ok": res["vol_ok"],
                "pattern_ok": res["pattern_ok"]
            })
            if res["ok"]:
                # pick the first ok (or you can rank by volume)
                best_candidate = sym
                break

        if best_candidate:
            try_buy(worker, best_candidate, worker.amount)


# =====================
# Flask Routes
# =====================
@app.route("/")
@app.route("/dashboard")
def dashboard():
    # render the Jinja template; the page uses JS to call APIs
    return render_template("dashboard.html")

@app.route("/api/start", methods=["POST"])
def api_start():
    global running
    with state_lock:
        running = True
    return jsonify({"ok": True})

@app.route("/api/stop", methods=["POST"])
def api_stop():
    global running
    with state_lock:
        running = False
    return jsonify({"ok": True})

@app.route("/api/mode", methods=["POST"])
def api_mode():
    global mode
    data = request.get_json(force=True, silent=True) or {}
    m = data.get("mode", "fast").lower()
    if m not in ("fast", "safe"):
        return jsonify({"ok": False, "error": "invalid mode"}), 400
    with state_lock:
        mode = m
    return jsonify({"ok": True, "mode": mode})

@app.route("/api/watchlist", methods=["POST"])
def api_watchlist():
    global watchlist_text
    data = request.get_json(force=True, silent=True) or {}
    wl = data.get("watchlist", "")
    if not wl:
        return jsonify({"ok": False, "error": "empty"}), 400
    with state_lock:
        watchlist_text = wl
    return jsonify({"ok": True, "count": len(parse_watchlist(watchlist_text))})

@app.route("/api/workers/add", methods=["POST"])
def api_workers_add():
    data = request.get_json(force=True, silent=True) or {}
    amt = float(data.get("amount", 20))
    with state_lock:
        new_id = (max(workers.keys()) + 1) if workers else 1
        workers[new_id] = Worker(id=new_id, amount=amt, status="scanning", started_at=utcnow_iso())
    return jsonify({"ok": True, "id": new_id})

@app.route("/api/workers/clear", methods=["POST"])
def api_workers_clear():
    with state_lock:
        workers.clear()
    return jsonify({"ok": True})

@app.route("/api/status")
def api_status():
    with state_lock:
        prof = profit_snapshot()
        w = {wid: vars(wk) | {"position": vars(wk.position) if wk.position else None} for wid, wk in workers.items()}
        return jsonify({
            "running": running,
            "mode": mode,
            "watchlist": watchlist_text,
            "workers": w,
            "profit": prof
        })

@app.route("/api/trades")
def api_trades():
    with state_lock:
        return jsonify(trades)

@app.route("/api/debug")
def api_debug():
    limit = int(request.args.get("limit", "300"))
    with state_lock:
        return jsonify(debug_events[-limit:])

@app.route("/api/debug/toggle", methods=["POST"])
def api_debug_toggle():
    global debug_on
    data = request.get_json(force=True, silent=True) or {}
    turn_on = bool(data.get("on", True))
    with state_lock:
        debug_on = turn_on
    return jsonify({"ok": True, "debug": debug_on})


# =====================
# Startup
# =====================
def start_threads_once():
    t = threading.Thread(target=bot_loop, daemon=True)
    t.start()

start_threads_once()

if __name__ == "__main__":
    # Local run: http://127.0.0.1:5000/
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
