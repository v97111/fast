import os
import threading
import time
import math
import json
import queue
import random
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional

from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

# =========================
# Config & Defaults
# =========================
PORT = int(os.getenv("PORT", "8080"))
MODE = os.getenv("MODE", "fast").lower()            # "fast" | "safe"
SIMULATE = os.getenv("SIMULATE", "true").lower() == "true"

REFRESH_SEC = 2
TP_TRIGGER_PCT = float(os.getenv("TP_TRIGGER_PCT", "1.0"))   # sell take-profit trigger %
TRAIL_GIVEBACK_PCT = float(os.getenv("TRAIL_GIVEBACK_PCT", "0.4"))
STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", "1.5"))
TIME_LIMIT_MIN = int(os.getenv("TIME_LIMIT_MIN", "45"))

# FAST vs SAFE buy filters (SAFE unchanged)
FAST_DAY_MOVE_MIN = 0.5    # relaxed
FAST_PULLBACK_MIN = 0.15   # relaxed
FAST_VOL_SPIKE_X = 1.05    # relaxed

SAFE_DAY_MOVE_MIN = 2.5
SAFE_PULLBACK_MIN = 0.5
SAFE_VOL_SPIKE_X = 1.2

# Pattern logic:
#  - "prev_high": require dip then recovery over previous high (SAFE)
#  - "bounce_only": higher close than prior candle (FAST)
#  - "none": (requested) remove pattern, but tighten other filters (below)
PATTERN = os.getenv("PATTERN", "auto").lower()      # "auto"|"prev_high"|"bounce_only"|"none"

# If pattern is none, raise the other bars slightly to avoid mass buys
NONE_DAY_MOVE_BONUS = 0.3        # add 0.3% to day-move requirement
NONE_PULLBACK_BONUS = 0.1        # add 0.1% to pullback requirement
NONE_VOL_SPIKE_BONUS = 0.03      # add 0.03x to volume spike requirement

# Watchlist default
DEFAULT_WATCH = ("BTCUSDT,ETHUSDT,BNBUSDT,SOLUSDT,DOGEUSDT,ADAUSDT,XRPUSDT,DOTUSDT,"
                 "LINKUSDT,AVAXUSDT,MATICUSDT,TONUSDT,ARBUSDT,SUIUSDT,LTCUSDT,BCHUSDT")

# =========================
# App
# =========================
app = Flask(__name__, static_folder="static", static_url_path="/static")
CORS(app, resources={r"/api/*": {"origins": "*"}})

# =========================
# In-memory state
# =========================
state: Dict[str, Any] = {
    "running": False,
    "mode": MODE,  # "fast" or "safe"
    "simulate": SIMULATE,
    "start_net": 0.0,        # starting net in USDT
    "current_net": 0.0,      # live net
    "watchlist_text": DEFAULT_WATCH,
    "debug_on": True,
    "workers": {},           # worker_id -> dict
    "trades": [],            # list of fills/closed trades
    "last_status_ts": None,
}

debug_ring: queue.Queue = queue.Queue(maxsize=5000)   # ring buffer for debug events

def dbg(event: Dict[str, Any]):
    if not state.get("debug_on", True):
        return
    try:
        if debug_ring.full():
            debug_ring.get_nowait()
        debug_ring.put_nowait(event)
    except queue.Full:
        pass

# =========================
# Utility: Mock market data
# Replace with your real Binance fetches.
# =========================
def fake_price(symbol: str) -> float:
    rnd = abs(hash(f"{symbol}:{int(time.time()/5)}")) % 10_000
    base = 10 + (rnd % 500) / 10.0
    return round(base, 4)

def fake_24h_change(symbol: str) -> float:
    # percent change
    return round(random.uniform(-1.0, 3.5), 2)

def fake_volume_spike(symbol: str) -> float:
    # multiple vs baseline
    return round(random.uniform(0.9, 1.6), 2)

def fake_candles(symbol: str, n=5) -> List[Dict[str, float]]:
    # OHLC small random walk
    out = []
    px = fake_price(symbol)
    for i in range(n):
        op = px * (1 + random.uniform(-0.002, 0.002))
        hi = op * (1 + random.uniform(0.000, 0.006))
        lo = op * (1 - random.uniform(0.000, 0.006))
        cl = op * (1 + random.uniform(-0.003, 0.003))
        out.append({"open": op, "high": hi, "low": lo, "close": cl})
        px = cl
    return out

# =========================
# Policy helpers
# =========================
def current_policy():
    m = state["mode"]
    if m == "safe":
        pol = {
            "day_move_min": SAFE_DAY_MOVE_MIN,
            "pullback_min": SAFE_PULLBACK_MIN,
            "vol_spike_x": SAFE_VOL_SPIKE_X,
            "pattern": "prev_high",
        }
    else:
        pol = {
            "day_move_min": FAST_DAY_MOVE_MIN,
            "pullback_min": FAST_PULLBACK_MIN,
            "vol_spike_x": FAST_VOL_SPIKE_X,
            "pattern": "bounce_only",
        }

    if PATTERN in ("prev_high", "bounce_only", "none"):
        pol["pattern"] = PATTERN

    # if pattern is none: tighten other filters a little
    if pol["pattern"] == "none":
        pol = {
            **pol,
            "day_move_min": pol["day_move_min"] + NONE_DAY_MOVE_BONUS,
            "pullback_min": pol["pullback_min"] + NONE_PULLBACK_BONUS,
            "vol_spike_x": pol["vol_spike_x"] + NONE_VOL_SPIKE_BONUS,
        }
    return pol

def check_buy(symbol: str) -> Dict[str, Any]:
    """
    Returns:
      { ok: bool, reason: str, day_ok, ema_ok, vol_ok, pattern_ok }
    """
    pol = current_policy()

    # ===== Gather “features” (replace with real data) =====
    day_change = fake_24h_change(symbol)           # %
    vol_spike = fake_volume_spike(symbol)          # x multiple
    candles = fake_candles(symbol, n=5)

    # Pullback = (prev high - prev close)/prev high
    pullback = 0.0
    if len(candles) >= 2:
        prev = candles[-2]
        if prev["high"] > 0:
            pullback = max(0.0, (prev["high"] - prev["close"]) / prev["high"] * 100.0)

    # “EMA ok” proxy: last close above prior close (replace with EMA50 actual)
    ema_ok = len(candles) >= 2 and candles[-1]["close"] >= candles[-2]["close"]

    day_ok = day_change >= pol["day_move_min"]
    vol_ok = vol_spike >= pol["vol_spike_x"]
    pull_ok = pullback >= pol["pullback_min"]

    # Pattern block
    pattern_ok = False
    if pol["pattern"] == "prev_high":
        if len(candles) >= 2:
            c_prev, c_last = candles[-2], candles[-1]
            dipped = c_prev["high"] > 0 and ((c_prev["high"] - c_prev["low"]) / c_prev["high"] >= 0.005)  # ~0.5%
            recovered = c_last["close"] > c_prev["high"]
            pattern_ok = dipped and recovered
    elif pol["pattern"] == "bounce_only":
        if len(candles) >= 3:
            prev2 = candles[-3]
            prev1 = candles[-2]
            pattern_ok = prev1["close"] > prev2["close"]
    elif pol["pattern"] == "none":
        pattern_ok = True  # intentionally ignored by policy, but our tightened thresholds above gate buys

    # Decision
    ok = day_ok and ema_ok and vol_ok and pull_ok and pattern_ok
    reason = "ok" if ok else (
        "no_pullback_recovery" if (day_ok and ema_ok and vol_ok and not pull_ok) else
        "no_vol_spike" if (day_ok and ema_ok and not vol_ok) else
        "below_ema50" if (not ema_ok) else
        "low_24h_move" if (not day_ok) else
        "pattern_fail"
    )

    # Emit debug
    dbg({
        "time": datetime.now(timezone.utc).isoformat(),
        "worker_id": None,
        "symbol": symbol,
        "reason": reason,
        "day_ok": day_ok,
        "ema_ok": ema_ok,
        "vol_ok": vol_ok,
        "pattern_ok": pattern_ok,
    })

    return {
        "ok": ok,
        "reason": reason,
        "day_ok": day_ok,
        "ema_ok": ema_ok,
        "vol_ok": vol_ok,
        "pattern_ok": pattern_ok,
    }

# =========================
# Worker threads
# =========================
WORKER_LOCK = threading.Lock()
WORKER_ID_SEQ = 0

def next_worker_id() -> int:
    global WORKER_ID_SEQ
    with WORKER_LOCK:
        WORKER_ID_SEQ += 1
        return WORKER_ID_SEQ

def coins_in_play() -> set:
    """Coins any worker is currently holding (to avoid double buying)."""
    held = set()
    for w in state["workers"].values():
        if w.get("status") == "in_position" and w.get("symbol"):
            held.add(w["symbol"])
    return held

def worker_loop(worker_id: int):
    w = state["workers"][worker_id]
    amount = w["amount"]
    watch = [s.strip().upper() for s in state["watchlist_text"].split(",") if s.strip()]
    i = 0
    while state["running"] and w["enabled"]:
        try:
            # pick next symbol
            symbol = watch[i % len(watch)]
            i += 1

            # skip if already held by another worker
            if symbol in coins_in_play() and w.get("symbol") != symbol:
                time.sleep(0.05)
                continue

            # if not in position, check buy
            if w["status"] == "idle":
                res = check_buy(symbol)
                # Show spinner activity
                w["last_scan"] = {
                    "symbol": symbol, "ok": res["ok"], "reason": res["reason"],
                    "t": datetime.now(timezone.utc).isoformat()
                }

                if res["ok"]:
                    # BUY (simulate)
                    w["status"] = "in_position"
                    w["symbol"] = symbol
                    w["entry_px"] = fake_price(symbol)
                    w["tp_anchor"] = w["entry_px"] * (1 + TP_TRIGGER_PCT/100.0)
                    w["trail_max"] = w["tp_anchor"]
                    w["started_at"] = datetime.now(timezone.utc).isoformat()
                    w["note"] = f"Bought {symbol} @ {w['entry_px']:.4f} (sim)"
                    state["trades"].append({
                        "time": w["started_at"],
                        "side": "BUY",
                        "symbol": symbol,
                        "price": w["entry_px"],
                        "amount": amount,
                        "worker": worker_id
                    })
                else:
                    time.sleep(0.02)

            else:
                # manage position
                px = fake_price(w["symbol"])
                # trail logic after TP trigger
                if px >= w["tp_anchor"]:
                    w["trail_max"] = max(w["trail_max"], px)
                    giveback_px = w["trail_max"] * (1 - TRAIL_GIVEBACK_PCT/100.0)
                    if px <= giveback_px:
                        # SELL on trail giveback
                        pnl = (px - w["entry_px"]) / w["entry_px"] * 100.0
                        state["trades"].append({
                            "time": datetime.now(timezone.utc).isoformat(),
                            "side": "SELL",
                            "symbol": w["symbol"],
                            "price": px,
                            "amount": amount,
                            "worker": worker_id,
                            "pnl_pct": pnl
                        })
                        w.update({"status": "idle", "symbol": None, "entry_px": None, "note": f"Sold on trail @ {px:.4f}"})
                # hard stop
                stop_px = w["entry_px"] * (1 - STOP_LOSS_PCT/100.0)
                if px <= stop_px:
                    pnl = (px - w["entry_px"]) / w["entry_px"] * 100.0
                    state["trades"].append({
                        "time": datetime.now(timezone.utc).isoformat(),
                        "side": "SELL",
                        "symbol": w["symbol"],
                        "price": px,
                        "amount": amount,
                        "worker": worker_id,
                        "pnl_pct": pnl
                    })
                    w.update({"status": "idle", "symbol": None, "entry_px": None, "note": f"Sold on stop @ {px:.4f}"})
                time.sleep(0.05)

        except Exception as e:
            w["note"] = f"Worker error: {e}"
            time.sleep(0.2)

# =========================
# Routes
# =========================
@app.route("/")
def root():
    return jsonify({"message": "Bot backend running", "mode": state["mode"], "ok": True, "simulate": state["simulate"]})

@app.route("/dashboard")
def serve_dashboard():
    return send_from_directory("static", "index.html")

@app.route("/api/start", methods=["POST"])
def api_start():
    if state["running"]:
        return jsonify({"ok": True, "message": "already running"})
    state["running"] = True
    state["last_status_ts"] = datetime.now(timezone.utc).isoformat()
    # start existing workers
    for worker_id, w in state["workers"].items():
        w["enabled"] = True
        threading.Thread(target=worker_loop, args=(worker_id,), daemon=True).start()
    return jsonify({"ok": True})

@app.route("/api/stop", methods=["POST"])
def api_stop():
    state["running"] = False
    for w in state["workers"].values():
        w["enabled"] = False
    return jsonify({"ok": True})

@app.route("/api/mode", methods=["POST"])
def api_mode():
    data = request.get_json(force=True)
    m = data.get("mode", "").lower()
    if m in ("fast", "safe"):
        state["mode"] = m
        return jsonify({"ok": True, "mode": m})
    return jsonify({"ok": False, "error": "mode must be fast|safe"}), 400

@app.route("/api/watchlist", methods=["POST"])
def api_watch():
    data = request.get_json(force=True)
    wl = data.get("watchlist", "")
    if not wl:
        return jsonify({"ok": False, "error": "watchlist required"}), 400
    state["watchlist_text"] = wl
    return jsonify({"ok": True})

@app.route("/api/debug/toggle", methods=["POST"])
def api_debug_toggle():
    data = request.get_json(force=True)
    state["debug_on"] = bool(data.get("on", True))
    return jsonify({"ok": True, "on": state["debug_on"]})

@app.route("/api/debug", methods=["GET"])
def api_debug():
    lim = int(request.args.get("limit", "500"))
    items = []
    try:
        # copy non-destructively
        qlist = list(debug_ring.queue)
        items = qlist[-lim:]
    except Exception:
        pass
    return jsonify(items)

@app.route("/api/status", methods=["GET"])
def api_status():
    # quick global profit calc (simulate)
    usdt_bal = 0.0
    assets_val = 0.0
    for w in state["workers"].values():
        if w.get("status") == "in_position" and w.get("symbol"):
            px = fake_price(w["symbol"])
            # assume amount is USDT quote size
            assets_val += w["amount"]  # simplified
        else:
            usdt_bal += w["amount"]

    current_net = usdt_bal + assets_val
    start_net = state.get("start_net") or current_net
    state["start_net"] = start_net
    state["current_net"] = current_net
    profit_abs = current_net - start_net
    profit_pct = (profit_abs / start_net * 100.0) if start_net > 0 else 0.0

    return jsonify({
        "running": state["running"],
        "mode": state["mode"],
        "simulate": state["simulate"],
        "watchlist": state["watchlist_text"],
        "start_net": round(start_net, 2),
        "current_net": round(current_net, 2),
        "profit": {"abs": round(profit_abs, 2), "pct": round(profit_pct, 2)},
        "tp": TP_TRIGGER_PCT,
        "trail": TRAIL_GIVEBACK_PCT,
        "sl": STOP_LOSS_PCT,
        "time_limit_min": TIME_LIMIT_MIN,
        "workers": state["workers"],
        "ts": datetime.now(timezone.utc).isoformat()
    })

@app.route("/api/trades", methods=["GET"])
def api_trades():
    return jsonify(state["trades"][-1000:])

@app.route("/api/workers/add", methods=["POST"])
def api_workers_add():
    data = request.get_json(force=True)
    amount = float(data.get("amount", 20))
    wid = next_worker_id()
    w = {
        "id": wid,
        "amount": amount,
        "status": "idle",
        "symbol": None,
        "entry_px": None,
        "tp_anchor": None,
        "trail_max": None,
        "started_at": None,
        "enabled": True,
        "note": "",
        "last_scan": None,
    }
    state["workers"][wid] = w
    if state["running"]:
        threading.Thread(target=worker_loop, args=(wid,), daemon=True).start()
    return jsonify({"ok": True, "worker": w})

@app.route("/api/workers/remove", methods=["POST"])
def api_workers_remove():
    data = request.get_json(force=True)
    wid = int(data.get("id", 0))
    w = state["workers"].pop(wid, None)
    if w:
        w["enabled"] = False
    return jsonify({"ok": True})

# =========================
# Run
# =========================
if __name__ == "__main__":
    # start with one worker by default for demos
    if not state["workers"]:
        for _ in range(0):
            requests.post("http://localhost:8080/api/workers/add", json={"amount": 20})
    app.run(host="0.0.0.0", port=PORT, debug=False)
