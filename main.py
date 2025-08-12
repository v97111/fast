# main.py
import os, time, math, threading, json, traceback, csv
from collections import deque, defaultdict
from datetime import datetime, timedelta, timezone

from flask import Flask, jsonify, request, send_file, Response
from dotenv import load_dotenv

# Optional: real client only if SIMULATE=false
BINANCE_READY = False
try:
    from binance.client import Client
    from binance.enums import SIDE_BUY, SIDE_SELL, ORDER_TYPE_MARKET
    BINANCE_READY = True
except Exception:
    BINANCE_READY = False

load_dotenv()

# ====== CONFIG ======
PORT = int(os.getenv("PORT", "8000"))
MODE = os.getenv("MODE", "fast").lower()      # "fast" or "safe"
SIMULATE = os.getenv("SIMULATE", "true").lower() == "true"
API_KEY = os.getenv("BINANCE_API_KEY", "")
API_SECRET = os.getenv("BINANCE_API_SECRET", "")

QUOTE = os.getenv("QUOTE", "USDT")            # quote currency
INTERVAL = os.getenv("INTERVAL", "1m")        # klines interval
CANDLE_LIMIT = int(os.getenv("CANDLE_LIMIT", "120"))

# Worker / scanning
WORKERS = int(os.getenv("WORKERS", "6"))
SCAN_SLEEP = float(os.getenv("SCAN_SLEEP", "0.25"))  # seconds between symbol scans per worker
REFRESH_SYMBOLS_EVERY = int(os.getenv("REFRESH_SYMBOLS_EVERY", "600"))  # seconds

# Risk & entries
MIN_DAY_VOLATILITY_PCT = float(os.getenv("MIN_DAY_VOLATILITY_PCT", "0.02"))  # 2% 24h range/close minimum
SAFE_DROP_PCT = float(os.getenv("SAFE_DROP_PCT", "0.008"))  # 0.8% dip for prev_high pattern (safe mode)
EMA_LEN = int(os.getenv("EMA_LEN", "50"))

# Fast/Safe exits
# Take-profit aims for ~+1% net after fees; adjust if your fee differs.
TP_GROSS = float(os.getenv("TP_GROSS", "0.0125"))        # +1.25% hard TP
TRAIL_ARM = float(os.getenv("TRAIL_ARM", "0.016"))       # arm trail after +1.6%
TRAIL_GIVEBACK = float(os.getenv("TRAIL_GIVEBACK", "0.004"))  # -0.4% from peak
STOP_LOSS = float(os.getenv("STOP_LOSS", "0.015"))       # -1.5% SL
MAX_HOLD_MIN = int(os.getenv("MAX_HOLD_MIN", "45"))      # 45 min timeout

# Per-card spend (in quote asset)
DEFAULT_SPEND = float(os.getenv("DEFAULT_SPEND", "20"))  # e.g., 20 USDT per card

# Debug
DEBUG_BUFFER_MAX = int(os.getenv("DEBUG_BUFFER_MAX", "20000"))
DEBUG_ENABLED_DEFAULT = os.getenv("DEBUG_ENABLED", "true").lower() == "true"

# ====== APP STATE ======
app = Flask(__name__)

lock = threading.RLock()
debug_enabled = DEBUG_ENABLED_DEFAULT
debug_events = deque(maxlen=DEBUG_BUFFER_MAX)     # rows for /api/debug
trades = []                                       # closed trades
open_positions = {}                                # symbol -> dict(position)
watch_symbols = []                                 # list of symbols being scanned
active_cards = {}                                  # card_id -> dict(config/state)
next_card_id = 1

start_balances = {"quote": 1000.0, "assets": 0.0}  # simulated starting balances
balances = {"quote": 1000.0}                       # simulated balances (USDT)
asset_balances = defaultdict(float)                # simulated balances per base asset
fees_rate = 0.001                                  # 0.1% taker as rough default

# Real Binance client (only if SIMULATE=false and keys present)
client = None
if not SIMULATE and BINANCE_READY and API_KEY and API_SECRET:
    client = Client(API_KEY, API_SECRET)

# ====== UTILS ======
def utcnow_iso():
    return datetime.now(timezone.utc).isoformat()

def add_debug(reason, symbol, worker_id, day_ok, ema_ok, vol_ok, pattern_ok):
    if not debug_enabled:
        return
    debug_events.append({
        "time": utcnow_iso(),
        "worker_id": worker_id,
        "symbol": symbol,
        "reason": reason,
        "day_ok": day_ok,
        "ema_ok": ema_ok,
        "vol_ok": vol_ok,
        "pattern_ok": pattern_ok
    })

def ema(values, n):
    if not values or n <= 0: return 0.0
    k = 2.0 / (n + 1)
    e = values[0]
    for v in values[1:]:
        e = v * k + e * (1 - k)
    return e

def pct(a, b):
    if b == 0: return 0.0
    return (a - b) / b

def get_symbols_from_binance():
    # Pull spot USDT pairs with trading status TRADING
    syms = []
    if client:
        ex = client.get_exchange_info()
        for s in ex["symbols"]:
            if s["status"] == "TRADING" and s["quoteAsset"] == QUOTE and s.get("isSpotTradingAllowed", True):
                syms.append(s["symbol"])
    else:
        # Fallback: a compact static list (extend as you wish)
        syms = ["BTCUSDT","ETHUSDT","BNBUSDT","SOLUSDT","XRPUSDT","ADAUSDT","DOGEUSDT","AVAXUSDT",
                "TONUSDT","DOTUSDT","LINKUSDT","UNIUSDT","MATICUSDT","LTCUSDT","ATOMUSDT",
                "NEARUSDT","APTUSDT","OPUSDT","ARBUSDT","INJUSDT","SUIUSDT","ICPUSDT","FILUSDT",
                "AAVEUSDT","COMPUSDT","ALGOUSDT","HBARUSDT","IMXUSDT","SANDUSDT","MANAUSDT",
                "GALAUSDT","CHZUSDT","CRVUSDT","DYDXUSDT","PEPEUSDT","MINAUSDT","KAVAUSDT",
                "CFXUSDT","STGUSDT","LRCUSDT","CELOUSDT","RLCUSDT","IDEXUSDT","AXSUSDT",
                "ETCUSDT","XLMUSDT","ZILUSDT","ZRXUSDT","HOTUSDT","IOTAUSDT","ONEUSDT",
                "SKLUSDT","IOSTUSDT","XTZUSDT","ROSEUSDT","KNCUSDT","C98USDT","API3USDT",
                "ILVUSDT","BICOUSDT","BANDUSDT","DENTUSDT","MASKUSDT","MAGICUSDT","FXSUSDT",
                "PYRUSDT","CTKUSDT","GLMRUSDT","THETAUSDT","ENSUSDT","REIUSDT","ACHUSDT",
                "MKRUSDT","SUIUSDT","CFXUSDT"]
    return syms

def fetch_candles(symbol, interval=INTERVAL, limit=CANDLE_LIMIT):
    if client:
        kl = client.get_klines(symbol=symbol, interval=interval, limit=limit)
        candles = []
        for o in kl:
            candles.append({
                "open_time": o[0],
                "open": float(o[1]),
                "high": float(o[2]),
                "low": float(o[3]),
                "close": float(o[4]),
                "volume": float(o[5]),
                "close_time": o[6]
            })
        return candles
    # Simulator: fabricate gentle waves
    base = abs(hash(symbol)) % 10000
    nowp = 1.0 + (time.time() % 300) / 300.0
    price = (base/1000.0) * nowp
    candles = []
    for i in range(limit):
        p = price * (1 + 0.0008 * math.sin((i + time.time()/5) * 0.8))
        candles.append({
            "open_time": i,
            "open": p * (1 - 0.0005),
            "high": p * (1 + 0.001),
            "low": p * (1 - 0.001),
            "close": p,
            "volume": 100 + 10 * math.sin(i*0.7),
            "close_time": i
        })
    return candles

def make_policy(mode: str):
    mode = (mode or "fast").lower()
    if mode == "safe":
        return {
            "ema_relax": 1.005,             # allow up to +0.5% above EMA50
            "vol_mult": 1.1,                 # 10% above avg vol
            "min_day_vol": MIN_DAY_VOLATILITY_PCT,
            "pattern": "prev_high",          # requires dip then break prev high
        }
    # FAST (pattern removed, but stronger EMA/vol so not everything passes)
    return {
        "ema_relax": 0.996,                  # price must be <= 0.4% ABOVE EMA50 (i.e., close to EMA)
        "vol_mult": 1.0,                     # >= average volume
        "min_day_vol": MIN_DAY_VOLATILITY_PCT,
        "pattern": "none",                   # no strict pattern gate
    }

def evaluate_buy_checks(symbol: str, policy: dict, worker_id: int):
    candles = fetch_candles(symbol)
    if len(candles) < max(EMA_LEN, 3):
        add_debug("not_enough_candles", symbol, worker_id, False, False, False, False)
        return {"ok": False, "reason": "not_enough_candles", "pattern_ok": False, "ema_ok": False, "vol_ok": False, "day_ok": False}

    closes = [c["close"] for c in candles]
    vols = [max(0.0, c["volume"]) for c in candles]
    last = candles[-1]
    prev = candles[-2]

    # 24h "range/close" as simple volatility proxy (use your better one if you have 24h stats)
    full_high = max(c["high"] for c in candles[-60:])
    full_low = min(c["low"] for c in candles[-60:])
    day_vol = (full_high - full_low) / max(1e-9, prev["close"])
    day_ok = day_vol >= policy["min_day_vol"]

    # EMA50 and distance
    ema50 = ema(closes[-EMA_LEN:], EMA_LEN)
    ema_ok = last["close"] <= ema50 * policy["ema_relax"]

    # Volume spike vs average of last N
    avg_vol = sum(vols[-30:]) / max(1, len(vols[-30:]))
    vol_ok = last["volume"] >= (avg_vol * policy["vol_mult"])

    # --- Pattern Check ---
    pattern_ok = False
    if policy["pattern"] == "prev_high":  # Safe mode pattern
        # require a dip in prev candle and current close > prev high
        dipped = (prev["close"] > 0) and ((prev["close"] - prev["low"]) / prev["close"] >= SAFE_DROP_PCT)
        recovered = last["close"] > prev["high"]
        pattern_ok = dipped and recovered

    elif policy["pattern"] == "none":
        # light selectivity: require tiny positive momentum (avoid buying total flat/down)
        small_gain = (last["close"] - prev["close"]) / max(1e-9, prev["close"]) >= 0.002  # +0.2%
        pattern_ok = small_gain

    else:  # "bounce_only" (kept for backward compatibility)
        pprev = candles[-3]
        pattern_ok = prev["close"] > pprev["close"]

    ok = day_ok and ema_ok and vol_ok and pattern_ok
    reason = "ok"
    if not day_ok: reason = "low_24h_move"
    elif not ema_ok: reason = "above_ema50"
    elif not vol_ok: reason = "no_vol_spike"
    elif not pattern_ok: reason = "no_pullback_recovery"

    add_debug(reason, symbol, worker_id, day_ok, ema_ok, vol_ok, pattern_ok)
    return {
        "ok": ok,
        "reason": reason,
        "day_ok": day_ok,
        "ema_ok": ema_ok,
        "vol_ok": vol_ok,
        "pattern_ok": pattern_ok,
        "last_price": last["close"]
    }

def simulate_fill_buy(symbol, spend_quote):
    price = fetch_candles(symbol)[-1]["close"]
    qty = spend_quote / price
    fee = spend_quote * fees_rate
    spend_after_fee = spend_quote + fee
    with lock:
        balances["quote"] -= spend_after_fee
        base = symbol.replace(QUOTE, "")
        asset_balances[base] += qty
    return {"price": price, "qty": qty, "fee_quote": fee}

def simulate_fill_sell(symbol, qty):
    price = fetch_candles(symbol)[-1]["close"]
    gross = price * qty
    fee = gross * fees_rate
    receive = gross - fee
    with lock:
        balances["quote"] += receive
        base = symbol.replace(QUOTE, "")
        asset_balances[base] -= qty
    return {"price": price, "qty": qty, "fee_quote": fee, "receive_quote": receive}

def place_market_buy(symbol, spend_quote):
    if SIMULATE or not client:
        return simulate_fill_buy(symbol, spend_quote)
    # Real order (quoteOrderQty requires futures on some endpoints; spot alternative: calc qty by price)
    price = float(client.get_symbol_ticker(symbol=symbol)["price"])
    qty = max(0.0, (spend_quote / price))
    order = client.order_market_buy(symbol=symbol, quantity=round(qty, 6))
    fills = order.get("fills", [])
    fill_price = price
    fee = 0.0
    for f in fills:
        fill_price = float(f.get("price", fill_price))
        fee += float(f.get("commission", 0.0)) * (float(f.get("commissionAsset") == QUOTE))
    return {"price": fill_price, "qty": qty, "fee_quote": fee}

def place_market_sell(symbol, qty):
    if SIMULATE or not client:
        return simulate_fill_sell(symbol, qty)
    order = client.order_market_sell(symbol=symbol, quantity=round(qty, 6))
    fills = order.get("fills", [])
    fill_price = float(client.get_symbol_ticker(symbol=symbol)["price"])
    fee = 0.0
    receive_quote = fill_price * qty
    for f in fills:
        fill_price = float(f.get("price", fill_price))
        # commissionAsset may not be QUOTE; keeping simple
    receive_quote -= receive_quote * fees_rate
    base = symbol.replace(QUOTE, "")
    with lock:
        balances["quote"] += receive_quote
        asset_balances[base] -= qty
    return {"price": fill_price, "qty": qty, "fee_quote": receive_quote * fees_rate, "receive_quote": receive_quote}

def add_trade(rec):
    with lock:
        trades.append(rec)

def current_profit():
    # mark-to-market open positions + quote balance vs start
    with lock:
        quote_now = balances.get("quote", 0.0)
        mtm_assets = 0.0
        for sym, pos in open_positions.items():
            price = fetch_candles(sym)[-1]["close"]
            mtm_assets += price * pos["qty"]
        current = quote_now + mtm_assets
        starting = start_balances["quote"] + start_balances["assets"]
        p = current - starting
        pctp = p / starting if starting > 0 else 0.0
        return current, p, pctp

def start_card(symbol, spend_quote=DEFAULT_SPEND):
    global next_card_id
    with lock:
        cid = next_card_id
        next_card_id += 1
        active_cards[cid] = {
            "id": cid,
            "symbol": symbol,
            "spend": spend_quote,
            "created_at": utcnow_iso(),
            "state": "scanning",
            "mode": MODE
        }
    return cid

def try_open_position(card, worker_id):
    symbol = card["symbol"]
    policy = make_policy(card["mode"])
    check = evaluate_buy_checks(symbol, policy, worker_id)
    if not check["ok"]:
        return False
    # place buy
    fill = place_market_buy(symbol, card["spend"])
    entry = fill["price"]
    qty = fill["qty"]
    with lock:
        open_positions[symbol] = {
            "symbol": symbol,
            "entry": entry,
            "qty": qty,
            "opened_at": datetime.now(timezone.utc),
            "peak": entry,
            "mode": card["mode"]
        }
        card["state"] = "holding"
        card["entry"] = entry
        card["qty"] = qty
    add_trade({
        "type": "BUY",
        "time": utcnow_iso(),
        "symbol": symbol,
        "price": entry,
        "qty": qty,
        "mode": card["mode"],
        "note": "opened"
    })
    return True

def manage_position(symbol):
    with lock:
        pos = open_positions.get(symbol)
        if not pos:
            return
        entry = pos["entry"]
        qty = pos["qty"]
        mode = pos["mode"]
        opened_at = pos["opened_at"]
    price = fetch_candles(symbol)[-1]["close"]
    gain = pct(price, entry)

    # update peak for trailing
    with lock:
        if price > pos["peak"]:
            pos["peak"] = price
        peak = pos["peak"]

    # exits
    # hard TP
    if gain >= TP_GROSS:
        fill = place_market_sell(symbol, qty)
        with lock:
            open_positions.pop(symbol, None)
        add_trade({
            "type": "SELL",
            "time": utcnow_iso(),
            "symbol": symbol,
            "price": fill["price"],
            "qty": qty,
            "mode": mode,
            "note": "tp_hit"
        })
        return

    # trail (armed after TRAIL_ARM)
    if gain >= TRAIL_ARM:
        drawdown = pct(price, peak)
        if drawdown <= -TRAIL_GIVEBACK:
            fill = place_market_sell(symbol, qty)
            with lock:
                open_positions.pop(symbol, None)
            add_trade({
                "type": "SELL",
                "time": utcnow_iso(),
                "symbol": symbol,
                "price": fill["price"],
                "qty": qty,
                "mode": mode,
                "note": "trail_hit"
            })
            return

    # stop loss
    if gain <= -STOP_LOSS:
        fill = place_market_sell(symbol, qty)
        with lock:
            open_positions.pop(symbol, None)
        add_trade({
            "type": "SELL",
            "time": utcnow_iso(),
            "symbol": symbol,
            "price": fill["price"],
            "qty": qty,
            "mode": mode,
            "note": "sl_hit"
        })
        return

    # timeout
    age_min = (datetime.now(timezone.utc) - opened_at).total_seconds() / 60.0
    if age_min >= MAX_HOLD_MIN:
        fill = place_market_sell(symbol, qty)
        with lock:
            open_positions.pop(symbol, None)
        add_trade({
            "type": "SELL",
            "time": utcnow_iso(),
            "symbol": symbol,
            "price": fill["price"],
            "qty": qty,
            "mode": mode,
            "note": "timeout"
        })

def worker_loop(worker_id, my_symbols):
    while True:
        try:
            # scan open positions for exits
            with lock:
                symbols_open = list(open_positions.keys())
                cards_local = list(active_cards.values())
            for s in symbols_open:
                manage_position(s)

            # scan cards for entries
            for card in cards_local:
                if card["symbol"] not in my_symbols:
                    continue
                if card["state"] == "scanning":
                    try_open_position(card, worker_id)
                # don't buy same coin twice
                elif card["state"] == "holding":
                    pass
                time.sleep(SCAN_SLEEP)
        except Exception as e:
            add_debug(f"worker_error:{e}", "N/A", worker_id, False, False, False, False)
            time.sleep(1.0)

def rebalance_symbols():
    global watch_symbols
    while True:
        try:
            syms = get_symbols_from_binance()
            with lock:
                watch_symbols = syms
        except Exception as e:
            add_debug(f"symbol_refresh_error:{e}", "N/A", 0, False, False, False, False)
        time.sleep(REFRESH_SYMBOLS_EVERY)

def start_workers():
    # distribute symbols roughly evenly
    threads = []
    def chunk(lst, n):
        k = max(1, len(lst)//n)
        for i in range(n):
            yield lst[i*k:(i+1)*k] if i < n-1 else lst[i*k:]
    with lock:
        syms = watch_symbols[:]
    for wid, part in enumerate(chunk(syms, WORKERS), start=1):
        t = threading.Thread(target=worker_loop, args=(wid, part), daemon=True)
        t.start()
        threads.append(t)
    return threads

# ====== API ROUTES ======
@app.route("/")
def home():
    return jsonify({"ok": True, "message": "Bot backend running", "mode": MODE, "simulate": SIMULATE})

@app.route("/api/status")
def api_status():
    now = utcnow_iso()
    with lock:
        open_list = []
        for s, p in open_positions.items():
            age = (datetime.now(timezone.utc) - p["opened_at"]).total_seconds()
            open_list.append({
                "symbol": s,
                "entry": p["entry"],
                "qty": p["qty"],
                "mode": p["mode"],
                "opened_at": p["opened_at"].isoformat(),
                "age_sec": int(age),
                "peak": p["peak"]
            })
        cards = list(active_cards.values())
        ws = watch_symbols[:]
        quote_bal = balances.get("quote", 0.0)
    cur, prof, prof_pct = current_profit()
    return jsonify({
        "time": now,
        "mode": MODE,
        "simulate": SIMULATE,
        "quote": QUOTE,
        "quote_balance": round(quote_bal, 6),
        "portfolio_value": round(cur, 6),
        "profit_value": round(prof, 6),
        "profit_pct": round(prof_pct, 6),
        "open_positions": open_list,
        "cards": cards,
        "watch_count": len(ws),
        "workers": WORKERS
    })

@app.route("/api/trades")
def api_trades():
    with lock:
        return jsonify(trades)

@app.route("/api/debug")
def api_debug():
    limit = int(request.args.get("limit", "500"))
    with lock:
        data = list(debug_events)[-limit:]
    return jsonify(data)

@app.route("/api/debug/toggle", methods=["POST"])
def api_debug_toggle():
    global debug_enabled
    body = request.get_json(silent=True) or {}
    val = body.get("enabled")
    if isinstance(val, bool):
        debug_enabled = val
    return jsonify({"debug_enabled": debug_enabled})

@app.route("/api/debug/export")
def api_debug_export():
    # stream CSV
    def generate():
        header = ["time","worker_id","symbol","reason","day_ok","ema_ok","vol_ok","pattern_ok"]
        yield ",".join(header) + "\n"
        with lock:
            snapshot = list(debug_events)
        for r in snapshot:
            row = [
                r.get("time",""),
                str(r.get("worker_id","")),
                r.get("symbol",""),
                r.get("reason",""),
                str(r.get("day_ok","")),
                str(r.get("ema_ok","")),
                str(r.get("vol_ok","")),
                str(r.get("pattern_ok",""))
            ]
            yield ",".join(row) + "\n"
    return Response(generate(), mimetype="text/csv",
                    headers={"Content-Disposition":"attachment; filename=debug_events.csv"})

@app.route("/api/card", methods=["POST"])
def api_card():
    body = request.get_json(force=True)
    symbol = body.get("symbol")
    spend = float(body.get("spend", DEFAULT_SPEND))
    if not symbol or not symbol.endswith(QUOTE):
        return jsonify({"ok": False, "error": f"symbol must end with {QUOTE}"}), 400
    cid = start_card(symbol, spend)
    return jsonify({"ok": True, "card_id": cid})

@app.route("/api/card/<int:cid>/stop", methods=["POST"])
def api_card_stop(cid):
    with lock:
        card = active_cards.get(cid)
        if not card:
            return jsonify({"ok": False, "error": "card not found"}), 404
        # If holding, force close
        sym = card["symbol"]
        pos = open_positions.get(sym)
        if pos:
            fill = place_market_sell(sym, pos["qty"])
            open_positions.pop(sym, None)
            add_trade({
                "type": "SELL",
                "time": utcnow_iso(),
                "symbol": sym,
                "price": fill["price"],
                "qty": pos["qty"],
                "mode": pos["mode"],
                "note": "manual_close"
            })
        # remove card
        active_cards.pop(cid, None)
    return jsonify({"ok": True})

@app.route("/api/mode", methods=["POST"])
def api_mode():
    global MODE
    body = request.get_json(force=True)
    mode = body.get("mode","").lower()
    if mode not in ("fast","safe"):
        return jsonify({"ok": False, "error": "mode must be fast or safe"}), 400
    MODE = mode
    return jsonify({"ok": True, "mode": MODE})

# ====== BOOT ======
def boot_sim_balances_once():
    # set starting balances (simulation)
    with lock:
        if "INIT_DONE" in balances:
            return
        start_balances["quote"] = balances.get("quote", 1000.0)
        start_balances["assets"] = 0.0
        balances["INIT_DONE"] = 1

def main():
    global watch_symbols
    boot_sim_balances_once()
    watch_symbols = get_symbols_from_binance()
    # symbol refresher
    threading.Thread(target=rebalance_symbols, daemon=True).start()
    # workers
    start_workers()
    # web
    app.run(host="0.0.0.0", port=PORT, threaded=True)

if __name__ == "__main__":
    main()
