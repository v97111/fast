import os
import time
import json
import math
import threading
from datetime import datetime, timezone
from dataclasses import dataclass
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
# Config
# =========================
SCAN_INTERVAL_SEC = float(os.getenv("SCAN_INTERVAL_SEC", "2"))
EMA_PERIOD = 50  # EMA50
VOL_SPIKE_MULT_SAFE = float(os.getenv("VOL_SPIKE_MULT_SAFE", "1.5"))
VOL_SPIKE_MULT_FAST = float(os.getenv("VOL_SPIKE_MULT_FAST", "1.2"))
VOL_SPIKE_MULT_NONEPATTERN = float(os.getenv("VOL_SPIKE_MULT_NONEPATTERN", "1.8"))
TP_PROFIT_PCT = float(os.getenv("TP_PROFIT_PCT", "0.01"))  # 1% TP
SAFE_DROP_PCT = float(os.getenv("SAFE_DROP_PCT", "0.003"))  # 0.3% dip
RECENT_HIGH_LOOKBACK_SAFE = int(os.getenv("RECENT_HIGH_LOOKBACK_SAFE", "50"))
RECENT_HIGH_LOOKBACK_FAST = int(os.getenv("RECENT_HIGH_LOOKBACK_FAST", "20"))
NONEPATTERN_24H_MOVE_MIN = float(os.getenv("NONE_24H_MOVE_MIN", "-0.01"))
NONEPATTERN_24H_MOVE_MAX = float(os.getenv("NONE_24H_MOVE_MAX", "0.02"))
TIME_LIMIT_MIN = int(os.getenv("TIME_LIMIT_MIN", "45"))

BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "")
client = None
if BINANCE_OK:
    try:
        client = Client(api_key=BINANCE_API_KEY or None, api_secret=BINANCE_API_SECRET or None)
    except Exception:
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
    amount: float = 20.0      # USDT to spend
    status: str = "idle"      # idle | scanning | bought | selling | buying
    symbol: Optional[str] = None
    note: str = ""
    started_at: Optional[str] = None
    position: Optional[Position] = None
    updated: Optional[str] = None
    last_pnl: Optional[float] = None  # %
    unreal_pct: Optional[float] = None
    unreal_usd: Optional[float] = None
    cur_price: Optional[float] = None
    entry_price: Optional[float] = None
    tp_price: Optional[float] = None
    trail_arm_price: Optional[float] = None
    sl_price: Optional[float] = None

state_lock = threading.Lock()
running = False
mode = "fast"  # "safe" or "fast"
debug_on = True

watchlist_text = "BTCUSDT,ETHUSDT,BNBUSDT,ADAUSDT,SOLUSDT,XRPUSDT,DOGEUSDT,MATICUSDT,AVAXUSDT,DOTUSDT"
workers: Dict[int, Worker] = {}
trades: List[Dict[str, Any]] = []
debug_events: List[Dict[str, Any]] = []
debug_cap = 5000

starting_usdt = 1000.0
current_usdt = starting_usdt
assets: Dict[str, Position] = {}  # paper positions, one per symbol

# =========
# Helpers
# =========
def utcnow_iso():
    return datetime.now(timezone.utc).isoformat()

def append_debug(ev: Dict[str, Any]):
    global debug_events
    if not debug_on:
        return
    ev2 = dict(ev)
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
        ema_prev = v if ema_prev is None else v * k + ema_prev * (1 - k)
        out.append(ema_prev)
    return out

def get_klines(symbol: str, interval="1m", limit=120) -> List[Dict[str, float]]:
    if not client:
        return []
    try:
        rows = client.get_klines(symbol=symbol, interval=interval, limit=limit)
        return [{
            "open_time": r[0],
            "open": float(r[1]),
            "high": float(r[2]),
            "low": float(r[3]),
            "close": float(r[4]),
            "volume": float(r[5]),
            "close_time": r[6]
        } for r in rows]
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
            "priceChangePercent": float(t["priceChangePercent"]) / 100.0,
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
# Strategy (paper)
# ==============================
def evaluate_symbol(symbol: str, policy: Dict[str, Any]) -> Dict[str, Any]:
    candles = get_klines(symbol, "1m", limit=max(EMA_PERIOD + 5, 60))
    if len(candles) < max(EMA_PERIOD, 20):
        return {"ok": False, "reason": "no_data", "day_ok": False, "ema_ok": False, "vol_ok": False, "pattern_ok": False}

    closes = [c["close"] for c in candles]
    vols = [c["volume"] for c in candles]
    last = candles[-1]
    prev = candles[-2]

    t24 = get_ticker_24h(symbol)
    if not t24:
        return {"ok": False, "reason": "no_ticker", "day_ok": False, "ema_ok": False, "vol_ok": False, "pattern_ok": False}
    day_chg = t24["priceChangePercent"]
    day_ok = (-0.06 <= day_chg <= 0.06)

    e = ema(closes, EMA_PERIOD)
    ema50 = e[-1]
    ema_ok = (last["close"] >= ema50 and ema50 >= e[-2])

    avg_vol = sum(vols[-30:-1]) / max(1, len(vols[-30:-1]))
    vol_mult = policy["vol_mult"]
    vol_ok = (last["volume"] >= avg_vol * vol_mult)

    pattern_ok = False
    if policy["pattern"] == "prev_high":
        dipped = (prev["close"] > 0) and ((prev["close"] - prev["low"]) / prev["close"] >= SAFE_DROP_PCT)
        recovered = last["close"] > prev["high"]
        pattern_ok = dipped and recovered
    elif policy["pattern"] == "bounce_only":
        if len(candles) >= 3:
            p1 = candles[-2]
            pattern_ok = (p1["close"] > p1["open"]) and (last["close"] >= p1["close"])

    lookback = RECENT_HIGH_LOOKBACK_FAST if policy["name"] == "fast" else RECENT_HIGH_LOOKBACK_SAFE
    rh = recent_high(closes, lookback)
    near_recent_high = (last["close"] >= rh * 0.985) if rh else True

    reason = "ok"
    if not day_ok: reason = "day_extreme"
    elif not ema_ok: reason = "below_ema50"
    elif not vol_ok: reason = "no_vol_spike"
    elif not pattern_ok: reason = "pattern_miss"
    elif not near_recent_high: reason = "far_from_recent_high"

    return {"ok": (reason == "ok"), "reason": reason, "day_ok": day_ok, "ema_ok": ema_ok, "vol_ok": vol_ok, "pattern_ok": pattern_ok}

def try_sell(symbol: str, pos: Position) -> Optional[Dict[str, Any]]:
    t24 = get_ticker_24h(symbol)
    if not t24:
        return None
    last_price = t24["lastPrice"]
    tp_price = pos.buy_price * (1.0 + TP_PROFIT_PCT)
    if last_price >= tp_price:
        pnl_pct = (last_price - pos.buy_price) / pos.buy_price
        trade = {
            "time": utcnow_iso(),
            "worker_id": pos.worker_id,
            "action": "SELL",
            "symbol": symbol,
            "price": last_price,
            "qty": pos.qty,
            "pnl_pct": f"{pnl_pct*100:.3f}%"
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
        worker.entry_price = price
        worker.tp_price = price * (1.0 + TP_PROFIT_PCT)
        worker.note = f"Bought at {price:.6f}"
        worker.updated = utcnow_iso()
    trade = {
        "time": utcnow_iso(),
        "worker_id": worker.id,
        "action": "BUY",
        "symbol": symbol,
        "price": price,
        "qty": qty,
        "pnl_pct": ""
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
        return {"abs": pnl_abs, "pct": pnl_pct, "assets": assets_val, "usdt": current_usdt, "net": cur}

# =====================
# Bot thread
# =====================
stop_event = threading.Event()

def bot_loop():
    global running
    next_worker_i = 0
    while not stop_event.is_set():
        time.sleep(SCAN_INTERVAL_SEC)
        with state_lock:
            run = running
            wlist = list(workers.values())
            wl_symbols = parse_watchlist(watchlist_text)
            current_mode = mode
        if not run or not wlist or not wl_symbols:
            continue

        policy = {"name": "safe", "pattern": "prev_high", "vol_mult": VOL_SPIKE_MULT_SAFE} if current_mode == "safe" \
                 else {"name": "fast", "pattern": "bounce_only", "vol_mult": VOL_SPIKE_MULT_FAST}

        w = wlist[next_worker_i % len(wlist)]
        next_worker_i += 1

        # If holding, see if we can TP
        if w.position:
            try_sell(w.position.symbol, w.position)
            # update unrealized for UI
            t24 = get_ticker_24h(w.position.symbol)
            if t24:
                cur = t24["lastPrice"]
                upct = (cur - w.position.buy_price) / w.position.buy_price * 100.0
                uusd = (cur - w.position.buy_price) * w.position.qty
                with state_lock:
                    w.cur_price = cur
                    w.unreal_pct = upct
                    w.unreal_usd = uusd
                    w.updated = utcnow_iso()
            continue

        # Otherwise scan
        for sym in wl_symbols:
            with state_lock:
                if sym in assets:
                    continue
            res = evaluate_symbol(sym, policy)
            append_debug({
                "worker_id": w.id, "symbol": sym, "reason": res["reason"],
                "day_ok": res["day_ok"], "ema_ok": res["ema_ok"],
                "vol_ok": res["vol_ok"], "pattern_ok": res["pattern_ok"]
            })
            if res["ok"]:
                try_buy(w, sym, w.amount)
                break

# =====================
# Flask Routes
# =====================
@app.route("/")
@app.route("/dashboard")
def dashboard():
    # pass safe defaults for the Jinja template
    return render_template(
        "dashboard.html",
        safe_drop_pct=SAFE_DROP_PCT * 100.0,
        tp_trigger_pct=TP_PROFIT_PCT * 100.0,
        trail_arm=0.35 * 100.0,         # placeholder
        trail_pct=0.20 * 100.0,         # placeholder
        sl_pct=0.9 * 100.0,             # placeholder
        time_limit=TIME_LIMIT_MIN,
        watchlist_list=parse_watchlist(watchlist_text),
        recent_limit=200
    )

# ---- API expected by the dashboard.js ----
@app.post("/api/start-core")
@app.post("/api/start")  # back-compat
def api_start():
    global running
    with state_lock:
        running = True
    return jsonify({"ok": True, "running": True})

@app.post("/api/stop-core")
@app.post("/api/stop")  # back-compat
def api_stop():
    global running
    with state_lock:
        running = False
    return jsonify({"ok": True, "running": False})

@app.post("/api/add-worker")
@app.post("/api/workers/add")  # back-compat
def api_add_worker():
    data = request.get_json(silent=True) or {}
    amt = float(data.get("quote") or data.get("amount") or 20)
    with state_lock:
        new_id = (max(workers.keys()) + 1) if workers else 1
        workers[new_id] = Worker(id=new_id, amount=amt, status="scanning", started_at=utcnow_iso(), updated=utcnow_iso())
    return jsonify({"ok": True, "id": new_id})

@app.post("/api/stop-worker")
def api_stop_worker():
    data = request.get_json(silent=True) or {}
    wid = int(data.get("worker_id", 0))
    with state_lock:
        if wid in workers:
            # remove worker; could also set status='idle'
            workers.pop(wid, None)
    return jsonify({"ok": True})

@app.post("/api/mode")
def api_mode():
    global mode
    data = request.get_json(silent=True) or {}
    m = str(data.get("mode", "fast")).lower()
    if m not in ("fast", "safe"):
        return jsonify({"ok": False, "error": "invalid mode"}), 400
    with state_lock:
        mode = m
    return jsonify({"ok": True, "mode": mode})

@app.post("/api/watchlist")
def api_watchlist():
    global watchlist_text
    data = request.get_json(silent=True) or {}
    wl = data.get("watchlist", "")
    if not wl:
        return jsonify({"ok": False, "error": "empty"}), 400
    with state_lock:
        watchlist_text = wl
    return jsonify({"ok": True, "count": len(parse_watchlist(watchlist_text))})

@app.get("/api/status")
def api_status():
    with state_lock:
        prof = profit_snapshot()
        wl = parse_watchlist(watchlist_text)
        # format workers as array for UI
        wlist = []
        for w in workers.values():
            # if holding, ensure UI fields are present
            if w.position:
                t24 = get_ticker_24h(w.position.symbol)
                if t24:
                    w.cur_price = t24["lastPrice"]
                    w.entry_price = w.position.buy_price
                    w.unreal_pct = (w.cur_price - w.entry_price) / w.entry_price * 100.0
                    w.unreal_usd = (w.cur_price - w.entry_price) * w.position.qty
                    w.tp_price = w.entry_price * (1.0 + TP_PROFIT_PCT)
                    w.symbol = w.position.symbol
            wlist.append({
                "id": w.id,
                "quote": w.amount,
                "status": w.status,
                "symbol": w.symbol,
                "note": w.note,
                "started": w.started_at,
                "updated": w.updated,
                "last_pnl": w.last_pnl,
                "unreal_pct": w.unreal_pct,
                "unreal_usd": w.unreal_usd,
                "cur_price": w.cur_price,
                "entry_price": w.entry_price,
                "tp_price": w.tp_price,
                "trail_arm_price": w.trail_arm_price,
                "sl_price": w.sl_price,
            })
        return jsonify({
            "mode": mode,
            "debug_enabled": bool(debug_on),
            "watchlist_count": len(wl),
            "watchlist_total": len(wl),
            "start_net_usdt": starting_usdt,
            "current_net_usdt": prof["net"],
            "profit_usd": prof["abs"],
            "profit_pct": prof["pct"],
            "workers": wlist
        })

@app.get("/api/trades")
def api_trades():
    # Return as {rows: [...]}, fields used by table renderer
    with state_lock:
        return jsonify({
            "rows": trades  # each trade already shaped for UI in try_buy/try_sell
        })

@app.get("/api/debug")
def api_debug():
    # dashboard expects {enabled, counts, events}
    limit = int(request.args.get("limit", "300"))
    with state_lock:
        evs = debug_events[-limit:]
    # counts by reason
    counts: Dict[str, int] = {}
    for e in evs:
        r = e.get("reason", "unknown")
        counts[r] = counts.get(r, 0) + 1
    return jsonify({
        "enabled": bool(debug_on),
        "counts": counts,
        "events": evs
    })

@app.post("/api/debug/toggle")
def api_debug_toggle():
    global debug_on
    data = request.get_json(silent=True) or {}
    enabled = data.get("enabled", data.get("on", True))
    with state_lock:
        debug_on = bool(enabled)
    return jsonify({"ok": True, "enabled": debug_on})

# =====================
# Startup
# =====================
def start_threads_once():
    t = threading.Thread(target=bot_loop, daemon=True)
    t.start()

start_threads_once()

if __name__ == "__main__":
    # Local run
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
