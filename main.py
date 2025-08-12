# -*- coding: utf-8 -*-
"""
Fast-cycle Binance bot + Flask dashboard (multi-worker)

SAFE mode (realistic + conservative):
- EMA50 strict, volume >= 1.2x avg10
- Pattern: previous closed bar dips >= 0.6% and next closed bar closes above previous HIGH

FAST mode (easier):
- EMA >= 99.2% of EMA50
- Volume >= 0.85x avg10 OR last volume is top-3 among last 10 closed bars
- Pattern: bounce-only (last close > previous close)

Exits for both:
- Hard TP +1.25% (to net >= ~1% after fees), then trailing arms at +1.6% with 0.4% giveback
- Stop-loss -1.5%, Max trade time 45m

Dashboard:
- Mobile friendly, worker cards, debug (toggle/copy/export), trade history
"""
import os, time, csv, math, threading, sys
from datetime import datetime, timedelta, timezone
from collections import defaultdict, deque
from typing import Dict, List
from flask import Flask, render_template, jsonify, request, Response
from dotenv import load_dotenv
from binance.client import Client

# ------------------ Watchlist ------------------
WATCHLIST: List[str] = [
    "BTCUSDT","ETHUSDT","BNBUSDT","SOLUSDT","XRPUSDT","DOGEUSDT","ADAUSDT","AVAXUSDT","TRXUSDT","LINKUSDT",
    "DOTUSDT","MATICUSDT","TONUSDT","OPUSDT","ARBUSDT","SUIUSDT","LTCUSDT","BCHUSDT","ATOMUSDT","NEARUSDT",
    "APTUSDT","FILUSDT","HBARUSDT","ICPUSDT","GALAUSDT","CFXUSDT","FETUSDT","RNDRUSDT","INJUSDT","FTMUSDT",
    "THETAUSDT","MANAUSDT","SANDUSDT","AXSUSDT","FLOWUSDT","KAVAUSDT","ROSEUSDT","C98USDT","GMTUSDT","ANKRUSDT",
    "CHZUSDT","CRVUSDT","DYDXUSDT","ENSUSDT","LRCUSDT","ONEUSDT","QTUMUSDT","STGUSDT","WAVESUSDT","ZILUSDT",
    "MINAUSDT","PEPEUSDT","JOEUSDT","HIGHUSDT","IDEXUSDT","ILVUSDT","MAGICUSDT","LINAUSDT","OCEANUSDT","IMXUSDT",
    "RLCUSDT","GLMRUSDT","CELOUSDT","COTIUSDT","ACHUSDT","API3USDT","ALGOUSDT","BADGERUSDT","BANDUSDT","BATUSDT",
    "BICOUSDT","BLZUSDT","COMPUSDT","CTKUSDT","DASHUSDT","DENTUSDT","DODOUSDT","ELFUSDT","ENJUSDT","EOSUSDT",
    "ETCUSDT","FLMUSDT","FXSUSDT","GRTUSDT","HOTUSDT","ICXUSDT","IOSTUSDT","IOTAUSDT","KLAYUSDT","KNCUSDT",
    "MASKUSDT","MKRUSDT","MTLUSDT","NKNUSDT","OGNUSDT","OMGUSDT","PHAUSDT","PYRUSDT","REIUSDT","RENUSDT",
    "SKLUSDT","SPELLUSDT","STMXUSDT","STORJUSDT","TLMUSDT","UMAUSDT","UNIUSDT","VETUSDT","XLMUSDT","XTZUSDT",
    "YFIUSDT","ZRXUSDT"
]

# ------------------ Config ------------------
INTERVAL = Client.KLINE_INTERVAL_1MINUTE
KLIMIT   = 120

# Exits (guarantee >= ~1% TP first, then trail)
TAKE_PROFIT_MIN_PCT   = 0.0125  # +1.25% hard TP to cover round-trip fees
TRAIL_ARM_PCT         = 0.0160  # arm trailing only if >= +1.6%
TRAIL_GIVEBACK_PCT    = 0.0040  # 0.4% giveback; worst trailing ~+1.2%
STOP_LOSS_PCT         = 0.015   # -1.5%
MAX_TRADE_MINUTES     = 45

# Entry filters
MIN_DAY_VOLATILITY_PCT = 0.5     # 24h range >= 0.5%
SAFE_DROP_PCT           = 0.006   # 0.6% single-bar dip for SAFE only
COOLDOWN_MINUTES        = 8

# Loop timing
POLL_SECONDS_IDLE   = 2
POLL_SECONDS_ACTIVE = 2

# Logging / debug
LOG_FILE             = os.getenv("LOG_FILE", "fast_cycle_trades.csv")
RECENT_TRADES_LIMIT  = 500
DEBUG_BUFFER         = 600

# ------------------ Helpers ------------------
def ema(values, period):
    if len(values) < period or period <= 0: return None
    k = 2.0/(period+1.0); e = values[0]
    for v in values[1:]: e = v*k + e*(1-k)
    return e

def round_to(value, step):
    if step == 0: return value
    return math.floor(value/step)*step

def now_utc(): return datetime.now(timezone.utc)

def log_row(row):
    newfile = not os.path.isfile(LOG_FILE)
    with open(LOG_FILE, "a", newline="") as f:
        w = csv.writer(f)
        if newfile:
            w.writerow(["time","symbol","action","price","qty","pnl_pct","note","worker_id"])
        w.writerow(row)

def read_csv_tail(path, n=RECENT_TRADES_LIMIT):
    if not os.path.isfile(path): return []
    with open(path, newline="") as f:
        rows = list(csv.reader(f))
    if len(rows) <= 1: return []
    header, body = rows[0], rows[1:]
    body = body[-n:]
    return [dict(zip(header, r)) for r in body][::-1]

# ------------------ Binance ops ------------------
def build_client():
    load_dotenv()
    key = os.getenv("BINANCE_API_KEY", "")
    sec = os.getenv("BINANCE_API_SECRET", "")
    if not key or not sec:
        print("ERROR: Set BINANCE_API_KEY and BINANCE_API_SECRET"); sys.exit(1)
    client = Client(key, sec)
    if os.getenv("BINANCE_TESTNET", "true").lower() in ("1","true","yes","y"):
        client.API_URL = "https://testnet.binance.vision/api"
        print("[INFO] Using TESTNET")
    else:
        print("[INFO] Using LIVE")
    return client

def get_symbol_filters(client, symbol):
    info = client.get_symbol_info(symbol)
    if not info or info.get("status") != "TRADING":
        raise RuntimeError(f"{symbol} not tradable")
    tick = lot = 0.0; min_notional = 0.0
    for f in info["filters"]:
        if f["filterType"] == "PRICE_FILTER": tick = float(f["tickSize"])
        elif f["filterType"] == "LOT_SIZE":   lot = float(f["stepSize"])
        elif f["filterType"] == "NOTIONAL":   min_notional = float(f.get("minNotional", 0.0))
    return tick, lot, min_notional

def get_price(client, symbol): return float(client.get_symbol_ticker(symbol=symbol)["price"])

def get_24h_stats(client, symbol):
    t = client.get_ticker(symbol=symbol)
    last = float(t["lastPrice"]); high = float(t["highPrice"]); low = float(t["lowPrice"])
    move_pct = ((high-low)/low*100.0) if low>0 else 0.0
    return last, move_pct

def get_klines(client, symbol, interval=INTERVAL, limit=KLIMIT):
    raw = client.get_klines(symbol=symbol, interval=interval, limit=limit)
    return [{
        "open_time": int(k[0]),
        "open":  float(k[1]),
        "high":  float(k[2]),
        "low":   float(k[3]),
        "close": float(k[4]),
        "volume":float(k[5]),
    } for k in raw]

def filter_valid_symbols(client, watchlist):
    info = client.get_exchange_info()
    valid = {s["symbol"] for s in info["symbols"] if s.get("status") == "TRADING"}
    return [s for s in watchlist if s in valid], len(watchlist)

def get_all_prices(client) -> Dict[str, float]:
    return {t["symbol"]: float(t["price"]) for t in client.get_all_tickers()}

def get_net_usdt_value(client) -> float:
    acct = client.get_account(); prices = get_all_prices(client); total = 0.0
    for b in acct["balances"]:
        asset = b["asset"]; amt = float(b["free"]) + float(b["locked"])
        if amt == 0.0: continue
        if asset == "USDT": total += amt
        else:
            pair = asset + "USDT"; p = prices.get(pair)
            if p: total += amt*p
    return total

# ------------------ Signal Policy ----------------
def make_policy(mode:str):
    if mode == "fast":
        return {
            "ema_relax": 0.992,      # >= 99.2% of EMA50
            "vol_mult": 0.85,        # >= 0.85× avg10 OR top-3 volume
            "min_day_vol": MIN_DAY_VOLATILITY_PCT,
            "pattern": "bounce_only" # last close > prev close
        }
    else:  # SAFE (unchanged strict EMA/vol; realistic 0.6% dip + prev high recovery)
        return {
            "ema_relax": 1.0,        # strictly above EMA50
            "vol_mult": 1.2,         # require a spike
            "min_day_vol": MIN_DAY_VOLATILITY_PCT,
            "pattern": "prev_high"   # previous bar dips >= 0.6%, next close > prev high
        }

def evaluate_buy_checks(client, symbol, cache, policy):
    # 24h volatility
    _, day_move = get_24h_stats(client, symbol)
    day_ok = day_move >= policy["min_day_vol"]

    # klines (cached)
    candles = cache.get(symbol)
    if candles is None:
        candles = get_klines(client, symbol)
        cache[symbol] = candles
    if len(candles) < 60:
        return {"ok": False, "reason": "few_candles", "day_ok": day_ok,
                "ema_ok": False, "vol_ok": False, "pattern_ok": False}

    closes = [c["close"] for c in candles]
    vols   = [c["volume"] for c in candles]

    # EMA50 trend
    ema50  = ema(closes[-60:], 50)
    ema_ok = (ema50 is not None) and (closes[-1] >= ema50 * policy["ema_relax"])

    # Volume
    vol_ok = False
    if len(vols) >= 11:
        last_closed_vol = vols[-2]  # last CLOSED bar
        avg10 = sum(vols[-12:-2]) / 10.0
        if policy["vol_mult"] >= 1.0:
            vol_ok = (avg10 > 0) and (last_closed_vol >= policy["vol_mult"] * avg10)
        else:
            # fast mode: soft threshold OR top-3 rank
            cond_soft = (avg10 > 0) and (last_closed_vol >= policy["vol_mult"] * avg10)
            block = vols[-12:-2]
            top3 = sorted(block, reverse=True)[:3] if block else []
            cond_rank = bool(block) and (last_closed_vol >= (top3[-1] if len(top3)==3 else (top3[-1] if top3 else 0)))
            vol_ok = bool(cond_soft or cond_rank)

    # Pattern
    pattern_ok = False
    if policy["pattern"] == "prev_high":
        if len(candles) >= 2:
            c_prev = candles[-2]; c_last = candles[-1]
            dipped = (c_prev["close"] > 0) and ((c_prev["close"] - c_prev["low"]) / c_prev["close"] >= SAFE_DROP_PCT)
            recovered = c_last["close"] > c_prev["high"]
            pattern_ok = dipped and recovered
    else:  # bounce_only (FAST)
        if len(candles) >= 3:
            last = candles[-2]; prev = candles[-3]
            pattern_ok = last["close"] > prev["close"]

    # Reason
    if not day_ok:      reason = "low_24h_move"
    elif not ema_ok:    reason = "below_ema50"
    elif not vol_ok:    reason = "no_vol_spike"
    elif not pattern_ok:reason = "no_pullback_recovery"
    else:               reason = "ok"

    return {"ok": (reason == "ok"), "reason": reason,
            "day_ok": day_ok, "ema_ok": ema_ok, "vol_ok": vol_ok, "pattern_ok": pattern_ok}

# ------------------ Orders ----------------------
def market_buy_by_quote(client, symbol, quote_usdt):
    price = get_price(client, symbol)
    _, lot, min_notional = get_symbol_filters(client, symbol)
    qty = round_to(quote_usdt / price, lot)
    if qty <= 0: raise RuntimeError("Quantity rounded to 0; increase amount.")
    notional = price * qty
    min_req = max(10.0, min_notional)
    if notional < min_req:
        qty = round_to((min_req / price), lot)
    order = client.create_order(symbol=symbol, side="BUY", type="MARKET", quantity=qty)
    fills = order.get("fills", [])
    if fills:
        spent = sum(float(f["price"])*float(f["qty"]) for f in fills)
        got   = sum(float(f["qty"]) for f in fills)
        avg_price = spent/got; qty = got
    else:
        avg_price = price
    return avg_price, qty

def market_sell_qty(client, symbol, qty):
    _, lot, _ = get_symbol_filters(client, symbol)
    qty = round_to(qty, lot)
    order = client.create_order(symbol=symbol, side="SELL", type="MARKET", quantity=qty)
    fills = order.get("fills", [])
    if fills:
        earned = sum(float(f["price"])*float(f["qty"]) for f in fills)
        sold   = sum(float(f["qty"]) for f in fills)
        avg_price = earned/sold; qty = sold
    else:
        avg_price = get_price(client, symbol)
    return avg_price, qty

# ------------------ Multi-Worker ----------------
class WorkerState:
    def __init__(self, wid: int, quote: float):
        self.id = wid; self.quote = quote
        self.status = "scanning"
        self.symbol = None; self.last_pnl = None
        self.note = "Scanning watchlist…"; self.updated = now_utc().isoformat()

class FastCycleBot:
    def __init__(self):
        self._client = None
        self._workers: Dict[int, threading.Thread] = {}
        self._worker_state: Dict[int, WorkerState] = {}
        self._stop_flags: Dict[int, threading.Event] = {}
        self._lock = threading.Lock()
        self._active_symbols = set()
        self._candles_cache = {}
        self._last_sell_time = defaultdict(lambda: datetime.min.replace(tzinfo=timezone.utc))
        self._running = False

        self._watchlist_total = 0; self._watchlist_count = 0
        self.start_net_usdt = None; self.current_net_usdt = None

        self._metrics_thread = None; self._metrics_stop = threading.Event()

        self.debug_enabled = True
        self._debug_events = deque(maxlen=DEBUG_BUFFER)

        self.mode = "safe"  # "safe" or "fast"

    # ---- lifecycle ----
    def start_core(self):
        if self._running: return
        self._client = build_client()
        global WATCHLIST
        WATCHLIST, total = filter_valid_symbols(self._client, WATCHLIST)
        self._watchlist_total = total; self._watchlist_count = len(WATCHLIST)
        print(f"[INFO] Watchlist filtered: {self._watchlist_count} valid (from {total})")
        try:
            self.start_net_usdt = get_net_usdt_value(self._client)
        except Exception as e:
            print("[WARN] Could not fetch start net value:", e); self.start_net_usdt = None
        self._metrics_stop.clear()
        self._metrics_thread = threading.Thread(target=self._refresh_metrics_loop, daemon=True)
        self._metrics_thread.start()
        self._running = True

    def stop_core(self):
        for wid, ev in list(self._stop_flags.items()): ev.set()
        self._workers.clear(); self._stop_flags.clear(); self._worker_state.clear()
        self._active_symbols.clear(); self._running = False; self._metrics_stop.set()

    def _refresh_metrics_loop(self):
        while not self._metrics_stop.is_set():
            try:
                if self._client: self.current_net_usdt = get_net_usdt_value(self._client)
            except Exception: pass
            time.sleep(6)

    def _debug_push(self, symbol, wid, flags):
        if not self.debug_enabled or not flags: return
        self._debug_events.append({
            "time": now_utc().isoformat(),
            "symbol": symbol, "worker_id": wid, "reason": flags.get("reason"),
            "day_ok": flags.get("day_ok"), "ema_ok": flags.get("ema_ok"),
            "vol_ok": flags.get("vol_ok"), "pattern_ok": flags.get("pattern_ok"),
        })

    # ---- workers ----
    def add_worker(self, quote_amount: float) -> int:
        if not self._running: self.start_core()
        wid = 1
        with self._lock:
            while wid in self._workers: wid += 1
            state = WorkerState(wid, float(quote_amount))
            self._worker_state[wid] = state
            stop_ev = threading.Event(); self._stop_flags[wid] = stop_ev
            t = threading.Thread(target=self._worker_loop, args=(wid, stop_ev), daemon=True)
            self._workers[wid] = t; t.start()
        return wid

    def stop_worker(self, wid: int):
        ev = self._stop_flags.get(wid)
        if ev: ev.set()

    def _update_state(self, wid: int, **kwargs):
        st = self._worker_state.get(wid)
        if not st: return
        for k, v in kwargs.items(): setattr(st, k, v)
        st.updated = now_utc().isoformat()

    def _eligible_symbol(self, sym: str) -> bool:
        return sym not in self._active_symbols and (now_utc() - self._last_sell_time[sym]).total_seconds() >= COOLDOWN_MINUTES*60

    def _worker_loop(self, wid: int, stop_ev: threading.Event):
        st = self._worker_state[wid]; client = self._client
        while not stop_ev.is_set():
            try:
                policy = make_policy(self.mode)

                # SCAN
                self._update_state(wid, status="scanning", symbol=None, note="Scanning watchlist…")
                picked = None
                for sym in WATCHLIST:
                    if stop_ev.is_set(): break
                    if not self._eligible_symbol(sym): continue
                    flags = evaluate_buy_checks(client, sym, self._candles_cache, policy)
                    if flags["ok"]:
                        picked = (sym, flags["reason"]); break
                    else:
                        self._debug_push(sym, wid, flags)
                    time.sleep(0.05)
                if not picked:
                    time.sleep(POLL_SECONDS_IDLE); continue

                sym, reason = picked
                with self._lock:
                    if sym in self._active_symbols: continue
                    self._active_symbols.add(sym)

                # BUY
                self._update_state(wid, status="buying", symbol=sym, note=f"BUY signal ({reason})")
                entry, qty = market_buy_by_quote(client, sym, st.quote)
                start = now_utc()
                log_row([start.isoformat(), sym, "BUY", f"{entry:.8f}", f"{qty:.8f}", "", "worker", wid])

                hard_tp  = entry * (1 + TAKE_PROFIT_MIN_PCT)
                trail_arm= entry * (1 + TRAIL_ARM_PCT)
                stop_loss= entry * (1 - STOP_LOSS_PCT)
                peak = entry; trailing = False

                # IN POSITION
                self._update_state(wid, status="in_position", note=f"In trade {sym}")
                while not stop_ev.is_set():
                    price = get_price(client, sym); ts = now_utc()
                    if price > peak: peak = price

                    # Hard take-profit first (guarantee >= ~1% net)
                    if price >= hard_tp and not trailing:
                        self._update_state(wid, status="selling", note=f"Hard TP on {sym}")
                        exitp, sold = market_sell_qty(client, sym, qty)
                        pnl = (exitp/entry - 1)*100.0
                        log_row([ts.isoformat(), sym, "SELL_TP_HARD", f"{exitp:.8f}", f"{sold:.8f}", f"{pnl:.4f}", "hard-tp", wid])
                        self._last_sell_time[sym] = now_utc(); self._update_state(wid, last_pnl=pnl)
                        break

                    # Arm trailing at stronger profit
                    if not trailing and price >= trail_arm:
                        trailing = True; self._update_state(wid, note=f"Trailing armed on {sym}")

                    # Trailing exit
                    if trailing:
                        floor = peak * (1 - TRAIL_GIVEBACK_PCT)
                        if price <= floor:
                            self._update_state(wid, status="selling", note=f"Trailing exit on {sym}")
                            exitp, sold = market_sell_qty(client, sym, qty)
                            pnl = (exitp/entry - 1)*100.0
                            log_row([ts.isoformat(), sym, "SELL_TP_TRAIL", f"{exitp:.8f}", f"{sold:.8f}", f"{pnl:.4f}", "trailing", wid])
                            self._last_sell_time[sym] = now_utc(); self._update_state(wid, last_pnl=pnl)
                            break

                    # Stop-loss
                    if price <= stop_loss:
                        self._update_state(wid, status="selling", note=f"Stop-loss on {sym}")
                        exitp, sold = market_sell_qty(client, sym, qty)
                        pnl = (exitp/entry - 1)*100.0
                        log_row([ts.isoformat(), sym, "SELL_SL", f"{exitp:.8f}", f"{sold:.8f}", f"{pnl:.4f}", "stop-loss", wid])
                        self._last_sell_time[sym] = now_utc(); self._update_state(wid, last_pnl=pnl)
                        break

                    # Time exit
                    if ts - start >= timedelta(minutes=MAX_TRADE_MINUTES):
                        self._update_state(wid, status="selling", note=f"Time exit on {sym}")
                        exitp, sold = market_sell_qty(client, sym, qty)
                        pnl = (exitp/entry - 1)*100.0
                        log_row([ts.isoformat(), sym, "SELL_TIME", f"{exitp:.8f}", f"{sold:.8f}", f"{pnl:.4f}", f"time>{MAX_TRADE_MINUTES}m", wid])
                        self._last_sell_time[sym] = now_utc(); self._update_state(wid, last_pnl=pnl)
                        break

                    time.sleep(POLL_SECONDS_ACTIVE)

                with self._lock: self._active_symbols.discard(sym)
                self._update_state(wid, status="cooldown", symbol=None, note=f"Cooldown {COOLDOWN_MINUTES}m")
                time.sleep(2)

            except Exception as e:
                self._update_state(wid, status="error", note=f"{type(e).__name__}: {e}")
                time.sleep(3)

        self._update_state(wid, status="stopped", note="Stopped")

    # ---- status for UI ----
    def dashboard_state(self):
        start_val = self.start_net_usdt; cur_val = self.current_net_usdt
        profit_usd = profit_pct = None
        if start_val is not None and cur_val is not None:
            profit_usd = cur_val - start_val
            profit_pct = (profit_usd / start_val * 100.0) if start_val > 0 else None

        workers = []
        for wid, st in self._worker_state.items():
            workers.append({
                "id": st.id, "quote": st.quote, "status": st.status, "symbol": st.symbol,
                "last_pnl": st.last_pnl, "note": st.note, "updated": st.updated
            })
        workers.sort(key=lambda x: x["id"])

        return {
            "running": self._running,
            "watchlist_count": self._watchlist_count, "watchlist_total": self._watchlist_total,
            "start_net_usdt": start_val, "current_net_usdt": cur_val,
            "profit_usd": profit_usd, "profit_pct": profit_pct,
            "workers": workers, "mode": self.mode, "debug_enabled": self.debug_enabled
        }

# ------------------ Flask ------------------
app = Flask(__name__, template_folder="templates")
bot = FastCycleBot()

@app.route("/")
def dashboard():
    recent = read_csv_tail(LOG_FILE, RECENT_TRADES_LIMIT)
    state = bot.dashboard_state()
    return render_template(
        "dashboard.html",
        state=state,
        recent_trades=recent,
        watchlist_list=WATCHLIST,
        tp_trigger_pct=(TAKE_PROFIT_MIN_PCT*100),
        trail_pct=(TRAIL_GIVEBACK_PCT*100),
        trail_arm=(TRAIL_ARM_PCT*100),
        sl_pct=(STOP_LOSS_PCT*100),
        time_limit=MAX_TRADE_MINUTES,
        min_day_vol=MIN_DAY_VOLATILITY_PCT,
        safe_drop_pct=(SAFE_DROP_PCT*100),
        recent_limit=RECENT_TRADES_LIMIT
    )

@app.get("/api/status")
def api_status(): return jsonify(bot.dashboard_state())

@app.get("/api/trades")
def api_trades(): return jsonify({"rows": read_csv_tail(LOG_FILE, RECENT_TRADES_LIMIT)})

# ---- Debug API ----
@app.get("/api/debug")
def api_debug():
    events = list(bot._debug_events)[-200:]
    counts = defaultdict(int)
    for e in events: counts[e["reason"]] += 1
    return jsonify({
        "enabled": bot.debug_enabled,
        "events": events[::-1],
        "counts": dict(sorted(counts.items(), key=lambda kv: kv[1], reverse=True))
    })

@app.post("/api/debug/toggle")
def api_debug_toggle():
    data = request.get_json(force=True, silent=True) or {}
    bot.debug_enabled = bool(data.get("enabled", True))
    return jsonify({"ok": True, "enabled": bot.debug_enabled})

@app.get("/api/debug/export")
def api_debug_export():
    fmt = request.args.get("format", "csv").lower()
    events = list(bot._debug_events)
    if fmt == "json":
        from json import dumps
        return Response(dumps(events, ensure_ascii=False, indent=2), mimetype="application/json")
    headers = ["time","worker_id","symbol","reason","day_ok","ema_ok","vol_ok","pattern_ok"]
    def gen():
        yield ",".join(headers) + "\n"
        for e in events:
            row = [str(e.get(h,"")) for h in headers]
            yield ",".join(row) + "\n"
    return Response(gen(), mimetype="text/csv",
                    headers={"Content-Disposition":"attachment; filename=debug_events.csv"})

# ---- Mode & Core/Workers ----
@app.post("/api/mode")
def api_mode():
    data = request.get_json(force=True, silent=True) or {}
    mode = str(data.get("mode","safe")).lower()
    if mode not in ("safe","fast"):
        return jsonify({"ok":False,"error":"mode must be 'safe' or 'fast'"}), 400
    bot.mode = mode
    return jsonify({"ok":True,"mode":bot.mode})

@app.post("/api/start-core")
def api_start_core(): bot.start_core(); return jsonify({"ok": True})

@app.post("/api/stop-core")
def api_stop_core(): bot.stop_core(); return jsonify({"ok": True})

@app.post("/api/add-worker")
def api_add_worker():
    data = request.get_json(force=True, silent=True) or {}
    wid = bot.add_worker(float(data.get("quote", 20.0)))
    return jsonify({"ok": True, "worker_id": wid})

@app.post("/api/stop-worker")
def api_stop_worker():
    data = request.get_json(force=True, silent=True) or {}
    bot.stop_worker(int(data.get("worker_id"))); return jsonify({"ok": True})

if __name__ == "__main__":
    load_dotenv(); port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port, debug=False)