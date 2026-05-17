#!/usr/bin/env python3
"""
GianniTGTradingBot — Interaktiver Trading Bot
Befehle die du im Telegram schreiben kannst:
  /scan           — EMA Sniper Scan aller 20 Coins (15m + 1H Confluence)
  /price SOL      — Aktueller Preis eines Coins
  /alarm SOL 91.05 89.64 93.87 — Alarm setzen (wartet auf Entry)
  /alarme         — Alle aktiven Alarme anzeigen
  /stop SOL       — Alarm stoppen
  /trade SOL 91.05 89.64 93.87 — Laufenden Trade überwachen (SL/TP)
  /trades         — Alle laufenden Trades anzeigen
  /stoptrade SOL  — Trade-Überwachung stoppen
  /hilfe          — Diese Liste anzeigen
"""
import json, time, threading, os, io
from urllib.request import urlopen, Request
from urllib.parse import urlencode
from urllib.error import URLError
from datetime import datetime, timedelta
from strategy import (scan_all_symbols, get_binance_candles, get_binance_usdt_balance,
                       round_price as _round_price)

CEST = timedelta(hours=2)
def now_cest():
    return (datetime.utcnow() + CEST).strftime("%H:%M")
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import mplfinance as mpf
import pandas as pd
import requests as _req

# Env-Variablen (Railway) haben Vorrang vor lokalen Config-Dateien
TOKEN   = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

if not TOKEN or not CHAT_ID:
    try:
        _cfg_path = os.path.join(os.path.dirname(__file__), "telegram_config.json")
        with open(_cfg_path) as f:
            cfg = json.load(f)
        TOKEN   = cfg["bot_token"]
        CHAT_ID = cfg["chat_id"]
    except:
        raise RuntimeError("Kein TELEGRAM_TOKEN / TELEGRAM_CHAT_ID gesetzt.")

ALARMS_FILE = os.environ.get("ALARMS_FILE", os.path.join(os.path.dirname(__file__), "alarms.json"))
INBOX_FILE  = os.environ.get("INBOX_FILE",  os.path.join(os.path.dirname(__file__), "alarm_inbox.json"))

SYMBOLS = ["BTCUSDT","ETHUSDT","SOLUSDT","BNBUSDT","LINKUSDT",
           "NEARUSDT","AVAXUSDT","MAGICUSDT","DOTUSDT","ADAUSDT","XRPUSDT",
           "SUIUSDT","INJUSDT","APTUSDT","ARBUSDT",
           "MATICUSDT","OPUSDT","DOGEUSDT","ATOMUSDT","LTCUSDT"]

STOCK_SYMBOLS = ["AAPL", "TSLA", "NVDA", "MSFT", "GOOGL", "PG", "JNJ"]

active_alerts = {}  # { "SOLUSDT": {"entry":..,"sl":..,"tp":..,"thread":..} }
active_trades = {}  # { "SOLUSDT": {"entry":..,"sl":..,"tp":..,"thread":..} }
_lock = threading.Lock()
POSITION_SIZE    = float(os.environ.get("POSITION_SIZE", "0"))
BINANCE_API_KEY  = os.environ.get("BINANCE_API_KEY", "")
BINANCE_API_SECRET = os.environ.get("BINANCE_API_SECRET", "")

def get_equity():
    """Live USDT-Balance von Binance; fällt auf POSITION_SIZE-Env-Var zurück."""
    if BINANCE_API_KEY and BINANCE_API_SECRET:
        bal = get_binance_usdt_balance(BINANCE_API_KEY, BINANCE_API_SECRET)
        if bal > 0:
            return bal
    return POSITION_SIZE if POSITION_SIZE else 1000.0
CHAT_ID = str(CHAT_ID) if CHAT_ID else CHAT_ID
COINGLASS_KEY = os.environ.get("COINGLASS_API_KEY", "")
offset = 0

# ── BTC Boss Filter ───────────────────────────────────────────────────────────
def get_btc_status():
    """Kontrollon BTC mbi/nën EMA20 në 1h dhe 15m. Kthen (bullish, emoji, pershkrim)."""
    results = {}
    for tf in ["1h", "15m"]:
        try:
            url = f"https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval={tf}&limit=25"
            with urlopen(url, timeout=8) as r:
                data = json.loads(r.read())
            closes = [float(k[4]) for k in data]
            ema    = get_ema(closes)
            price  = closes[-1]
            results[tf] = price > ema
        except:
            results[tf] = True  # nëse API dështon, lejo skanimin

    bullish_1h  = results.get("1h",  True)
    bullish_15m = results.get("15m", True)

    if bullish_1h and bullish_15m:
        return True,  "🟢", "BTC Bullish (1h + 15m mbi EMA20)"
    elif bullish_1h and not bullish_15m:
        return False, "🟡", "BTC Kujdes (15m nën EMA20, 1h ok)"
    else:
        return False, "🔴", "BTC Bearish (nën EMA20) — nuk ka skanim"

# ── BTC Emergency Monitor ─────────────────────────────────────────────────────
_btc_price_last = None

def monitor_btc_emergency():
    """Nëse BTC bie >1% brenda 15 minutave dhe ka trade aktive → alarm urgjent."""
    global _btc_price_last
    while True:
        try:
            price = get_price("BTCUSDT")
            if _btc_price_last is not None and active_trades:
                drop_pct = (_btc_price_last - price) / _btc_price_last * 100
                if drop_pct >= 1.0:
                    trades_list = ", ".join(s.replace("USDT","") for s in active_trades)
                    send(
                        f"🚨 <b>BTC po bie!</b>\n"
                        f"Rënie: <b>{round(drop_pct,2)}%</b> brenda 15 minutave\n"
                        f"Çmimi: ${round(price,2)}\n\n"
                        f"Trade aktive: <b>{trades_list}</b>\n"
                        f"Kontrollo pozicionet tua menjëherë!"
                    )
            _btc_price_last = price
        except: pass
        time.sleep(900)  # çdo 15 minuta

# ── Alarm Persistenz ──────────────────────────────────────────────────────────
def save_alarms():
    with _lock:
        data = {sym: {"entry": v["entry"], "sl": v["sl"], "tp": v["tp"]}
                for sym, v in active_alerts.items()}
    with open(ALARMS_FILE, "w") as f:
        json.dump(data, f)

def start_alarm_thread(coin, symbol, entry, sl, tp, notify=True):
    def monitor():
        if notify:
            if sl and tp:
                send(f"Alarmi vendosur: <b>{coin}</b>\nEntry: ${entry} | SL: ${sl} | TP: ${tp}")
            else:
                send(f"Alarmi vendosur: <b>{coin}</b> te ${entry}")
        last_price = None
        while symbol in active_alerts:
            try:
                price = get_price(symbol)
                triggered = price <= entry and (last_price is None or last_price > entry)
                if triggered:
                    if sl and tp:
                        send(
                            f"🚨 ENTRY ARRITUR: <b>{coin}</b>\n"
                            f"Çmimi: <b>${price}</b> | Entry: ${entry}\n"
                            f"Stop Loss: ${sl}\n"
                            f"Take Profit: ${tp}\n\n"
                            f"⚡ HAP BINANCE TANI!\n"
                            f"Pas blerjes: /trade {coin} {entry} {sl} {tp}"
                        )
                    else:
                        send(f"🔔 ALARM: <b>{coin}</b> arriti ${entry}!\nÇmimi tani: <b>${price}</b>")
                    for _ in range(3):
                        time.sleep(60)
                        if symbol not in active_alerts: break
                        send(f"⏰ Kujtesë: <b>{coin}</b> te ${entry} — ende aktiv!")
                    with _lock:
                        active_alerts.pop(symbol, None)
                    save_alarms()
                    break
                last_price = price
                time.sleep(20)
            except Exception: time.sleep(30)

    with _lock:
        if symbol in active_alerts:
            return
        t = threading.Thread(target=monitor, daemon=True)
        active_alerts[symbol] = {"entry": entry, "sl": sl, "tp": tp, "thread": t}
    save_alarms()
    t.start()

# ── Telegram Helfer ────────────────────────────────────────────────────────────
def tg(method, **kwargs):
    url  = f"https://api.telegram.org/bot{TOKEN}/{method}"
    data = urlencode(kwargs).encode()
    try:
        with urlopen(Request(url, data=data), timeout=10) as r:
            return json.loads(r.read())
    except: return {}

def send(text):
    tg("sendMessage", chat_id=CHAT_ID, text=text, parse_mode="HTML")

def get_updates(offset):
    r = tg("getUpdates", offset=offset, timeout=20)
    return r.get("result", [])

# ── Binance Helfer ────────────────────────────────────────────────────────────
def get_price(symbol):
    url = "https://api.binance.com/api/v3/ticker/price?" + urlencode({"symbol": symbol})
    with urlopen(url, timeout=5) as r:
        return float(json.loads(r.read())["price"])

def get_ema(closes, period=20):
    k, e = 2/(period+1), closes[0]
    for c in closes[1:]: e = c*k + e*(1-k)
    return e

def round_price(v):
    if v > 100: return round(v, 2)
    if v > 1:   return round(v, 4)
    return round(v, 5)

# ── Befehle ───────────────────────────────────────────────────────────────────
def cmd_status():
    now_dt  = datetime.utcnow() + CEST
    weekday = now_dt.weekday()
    hour_min = now_dt.hour * 60 + now_dt.minute
    market_open = weekday < 5 and (15*60) <= hour_min <= (21*60+30)
    days = ["E Hënë","E Martë","E Mërkurë","E Enjte","E Premte","E Shtunë","E Diel"]
    day_name = days[weekday]

    # BTC status
    btc_line = "BTC: duke ngarkuar..."
    mode_line = ""
    try:
        from urllib.request import urlopen
        url4 = "https://api.binance.com/api/v3/klines?" + urlencode({"symbol":"BTCUSDT","interval":"4h","limit":50})
        urld = "https://api.binance.com/api/v3/klines?" + urlencode({"symbol":"BTCUSDT","interval":"1d","limit":25})
        with urlopen(url4, timeout=8) as r: d4 = json.loads(r.read())
        with urlopen(urld, timeout=8) as r: dd = json.loads(r.read())
        c4 = [float(k[4]) for k in d4]
        cd = [float(k[4]) for k in dd]
        ema4h = get_ema(c4); emad = get_ema(cd)
        price = round(c4[-1], 2)
        bull4 = c4[-1] > ema4h; bulld = cd[-1] > emad
        t4 = "✅" if bull4 else "❌"; td = "✅" if bulld else "❌"
        btc_line = (f"BTC: <b>${price}</b>\n"
                    f"  Daily EMA20 {td}  ${round(emad,2)}\n"
                    f"  4h EMA20    {t4}  ${round(ema4h,2)}")
        if bull4 and bulld:
            mode_line = "🎯 Mode: <b>KRIPTO</b> — skanohet"
        else:
            mode_line = "🏦 Mode: <b>PLAN B</b> — aksione (nëse tregu hapur)"
    except:
        btc_line = "BTC: nuk u arrit"

    # Stock market
    if weekday >= 5:
        stock_line = f"📈 Bursa: <b>MBYLLUR</b> ({day_name})"
    elif market_open:
        close_h = 21; close_m = 30
        mins_left = (close_h*60+close_m) - hour_min
        stock_line = f"📈 Bursa: <b>HAPUR</b> 🟢  mbyllet pas {mins_left//60}h {mins_left%60}min"
    else:
        if hour_min < 15*60:
            mins_to = 15*60 - hour_min
            stock_line = f"📈 Bursa: <b>MBYLLUR</b> 🔴  hapet pas {mins_to//60}h {mins_to%60}min"
        else:
            stock_line = f"📈 Bursa: <b>MBYLLUR</b> 🔴  hapet nesër 15:00"

    # Alarms & Trades
    alarm_line = f"🔔 Alarme aktive: <b>{len(active_alerts)}</b>"
    if active_alerts:
        alarm_line += " — " + ", ".join(s.replace("USDT","") for s in active_alerts)
    trade_line = f"📊 Trades aktive: <b>{len(active_trades)}</b>"
    if active_trades:
        trade_line += " — " + ", ".join(s.replace("USDT","") for s in active_trades)

    send(
        f"📡 <b>STATUS — {now_dt.strftime('%H:%M')} CEST</b>\n{'─'*28}\n\n"
        f"{btc_line}\n\n"
        f"{mode_line}\n\n"
        f"{stock_line}\n\n"
        f"{alarm_line}\n"
        f"{trade_line}"
    )


def cmd_hilfe():
    send(
        "<b>GianniTGT Trading Bot 🤖</b>\n\n"
        "/status — Gjendja e plotë (BTC + Bursa + Alarme)\n"
        "/scan — Skano 20 coins (EMA20)\n"
        "/briefing — ETF flows + heatmap + news + setups\n"
        "/price BNB — Çmimi aktual\n"
        "/alarm BNB 674.50 663.20 685 — Vendos alarm\n"
        "/alarme — Shiko alarmet aktive\n"
        "/stop BNB — Fshij alarmin\n\n"
        "/trade BNB 674.50 663.20 685 — Monitoro trade aktiv\n"
        "/trades — Shiko të gjitha trades\n"
        "/stoptrade BNB — Ndalо monitorimin\n\n"
        "/hilfe — Kjo listë"
    )

def cmd_price(parts):
    if len(parts) < 2:
        send("Përdorimi: /price BNB"); return
    coin   = parts[1].upper().replace("USDT","")
    symbol = coin + "USDT"
    try:
        price = get_price(symbol)
        send(f"<b>{coin}/USDT</b>: ${price}")
    except:
        send(f"Coin {coin} nuk u gjet. Kontrollo emrin.")

def cmd_scan():
    """Manueller /scan — EMA Sniper 7-Filter (15m + 1H Confluence)."""
    do_scan(triggered_by_command=True, show_loading=True)

def cmd_alarm(parts):
    if len(parts) < 3:
        send("Përdorimi:\n/alarm BNB 674.50\n/alarm BNB 674.50 663.20 685.00"); return
    coin   = parts[1].upper().replace("USDT","")
    symbol = coin + "USDT"
    try:
        entry = float(parts[2])
        sl    = float(parts[3]) if len(parts) > 3 else None
        tp    = float(parts[4]) if len(parts) > 4 else None
    except:
        send("Numra të pavlefshëm."); return

    if symbol in active_alerts:
        send(f"Alarmi për {coin} është tashmë aktiv. /stop {coin} për ta fshirë."); return

    start_alarm_thread(coin, symbol, entry, sl, tp)

def cmd_alarme():
    if not active_alerts:
        send("Nuk ka alarme aktive."); return
    msg = "<b>Alarmet aktive:</b>\n\n"
    for sym, info in active_alerts.items():
        coin = sym.replace("USDT","")
        try:
            cur = get_price(sym)
            diff = round((cur - info["entry"]) / info["entry"] * 100, 2)
            dist = f"${cur} ({diff:+.2f}%)"
        except:
            dist = "?"
        sl_tp = f" | SL ${info['sl']} | TP ${info['tp']}" if info["sl"] else ""
        msg += f"• <b>{coin}</b> → Alarm te ${info['entry']}{sl_tp}\n  Tani: {dist}\n\n"
    msg += "/stop COIN — Fshij alarmin"
    send(msg)

def cmd_stop(parts):
    if len(parts) < 2:
        send("Përdorimi: /stop BNB"); return
    coin   = parts[1].upper().replace("USDT","")
    symbol = coin + "USDT"
    if symbol in active_alerts:
        active_alerts.pop(symbol)
        save_alarms()
        send(f"Alarmi për <b>{coin}</b> u fshi.")
    else:
        send(f"Kein aktiver Alarm für {coin}.")

# ── Trade Monitoring ──────────────────────────────────────────────────────────
def cmd_trade(parts):
    if len(parts) < 5:
        send("Verwendung: /trade BNB 668.06 663.20 677.78"); return
    coin   = parts[1].upper().replace("USDT","")
    symbol = coin + "USDT"
    try:
        entry, sl, tp = float(parts[2]), float(parts[3]), float(parts[4])
    except:
        send("Ungültige Zahlen."); return

    if symbol in active_trades:
        send(f"Monitorimi për {coin} është tashmë aktiv."); return

    def monitor_trade():
        rr = round((tp - entry) / (entry - sl), 1)
        send(
            f"✅ Trade aktiv: <b>{coin} LONG</b>\n"
            f"Entry: ${entry}\n"
            f"SL: ${sl} | TP: ${tp}\n"
            f"RR: {rr}:1\n"
            f"Do të njoftohesh kur të arrihet SL ose TP."
        )
        last_update = time.time()
        while symbol in active_trades:
            try:
                price = get_price(symbol)
                now   = time.time()

                # Update çdo 4 orë
                if now - last_update >= 14400:
                    pct = round((price - entry) / entry * 100, 2)
                    send(f"📊 Update <b>{coin}</b>: ${price} ({pct:+.2f}% nga entry)")
                    last_update = now

                if price <= sl:
                    send(
                        f"🔴 STOP LOSS U PREK: <b>{coin}</b>\n"
                        f"SL: ${sl} | Çmimi: ${price}\n\n"
                        f"Trade mbyllur. Mos u streso, setup tjetër vjen. 💪"
                    )
                    active_trades.pop(symbol, None)
                    break

                if price >= tp:
                    send(
                        f"🟢 TAKE PROFIT ARRITUR: <b>{coin}</b>\n"
                        f"TP: ${tp} | Çmimi: ${price}\n\n"
                        f"Masha'Allah! Mbyll trade-in. 🎯"
                    )
                    active_trades.pop(symbol, None)
                    break

                time.sleep(20)
            except: time.sleep(30)

    t = threading.Thread(target=monitor_trade, daemon=True)
    active_trades[symbol] = {"entry": entry, "sl": sl, "tp": tp, "thread": t}
    t.start()

def cmd_trades():
    if not active_trades:
        send("Nuk ka trade aktive."); return
    msg = "<b>Trades aktive:</b>\n\n"
    for sym, info in active_trades.items():
        try:
            price = get_price(sym)
            pct   = round((price - info["entry"]) / info["entry"] * 100, 2)
            msg  += f"<b>{sym.replace('USDT','')}</b>: ${price} ({pct:+.2f}%)\nEntry ${info['entry']} | SL ${info['sl']} | TP ${info['tp']}\n\n"
        except:
            msg += f"<b>{sym.replace('USDT','')}</b>: Entry ${info['entry']} | SL ${info['sl']} | TP ${info['tp']}\n\n"
    send(msg)

def cmd_briefing():
    """Dërgon briefing mëngjesi manualisht (force=True)."""
    threading.Thread(target=morning_briefing, kwargs={"force": True}, daemon=True).start()

def cmd_stoptrade(parts):
    if len(parts) < 2:
        send("Përdorimi: /stoptrade BNB"); return
    coin   = parts[1].upper().replace("USDT","")
    symbol = coin + "USDT"
    if symbol in active_trades:
        active_trades.pop(symbol)
        send(f"Monitorimi për <b>{coin}</b> u ndalua.")
    else:
        send(f"Nuk ka trade aktiv për {coin}.")

# ── Chart + Scan me Filtër Cilësie ────────────────────────────────────────────
_sl_alerted = {}

def generate_chart(sym, candles_15m_dicts, entry, sl, tp):
    """Generiert PNG 15m Candlestick-Chart mit EMA20/50/100/200 + Entry/SL/TP-Linien."""
    window = candles_15m_dicts[-80:]
    # Synthetische Timestamps (15m-Raster, rückwärts vom jetzigen Moment)
    end   = pd.Timestamp.utcnow().floor("15min")
    times = pd.date_range(end=end, periods=len(window), freq="15min")
    df = pd.DataFrame({
        "Open":   [c["open"]   for c in window],
        "High":   [c["high"]   for c in window],
        "Low":    [c["low"]    for c in window],
        "Close":  [c["close"]  for c in window],
        "Volume": [c["volume"] for c in window],
    }, index=times)

    all_closes = [c["close"] for c in candles_15m_dicts]

    def ema_series(n, color, width=1.2):
        k, e = 2 / (n + 1), all_closes[0]
        vals = []
        for v in all_closes:
            e = v * k + e * (1 - k)
            vals.append(e)
        s = pd.Series(vals[-80:], index=pd.DatetimeIndex(times))
        return mpf.make_addplot(s, color=color, width=width)

    ap = [
        ema_series(20,  "#2196F3", 1.8),   # EMA20  blau
        ema_series(50,  "#FF9800", 1.2),   # EMA50  orange
        ema_series(100, "#F44336", 1.0),   # EMA100 rot
        ema_series(200, "#9E9E9E", 1.0),   # EMA200 grau
    ]
    hl = dict(hlines=[entry, sl, tp],
              colors=["#3399ff", "#ff4444", "#00cc44"],
              linewidths=[1.2, 1.2, 1.2], linestyle="--")
    buf = io.BytesIO()
    fig, _ = mpf.plot(df, type="candle", style="nightclouds", addplot=ap, hlines=hl,
                      title=f"\n{sym} – 15m  |  Entry ${entry}  SL ${sl}  TP ${tp}",
                      figsize=(12, 7), returnfig=True)
    fig.savefig(buf, format="png", dpi=100, bbox_inches="tight", facecolor="#131722")
    plt.close(fig)
    buf.seek(0)
    return buf

def send_photo(buf, caption=""):
    """Dërgon foto në Telegram; fallback me tekst nëse dështon."""
    url = f"https://api.telegram.org/bot{TOKEN}/sendPhoto"
    try:
        _req.post(url,
                  data={"chat_id": CHAT_ID, "caption": caption, "parse_mode": "HTML"},
                  files={"photo": ("chart.png", buf, "image/png")}, timeout=30)
    except Exception:
        send(caption)

def scan_stocks():
    """Plan B: EMA20 Pullback Daily në aksione tech — thirret kur BTC është Bearish."""
    try:
        import yfinance as yf
    except ImportError:
        send("⚠️ yfinance nuk është instaluar ende. Railway po përditëson...")
        return

    # Kontrollo nëse është brenda orëve të tregut: Mon-Fri, 15:00-21:30 CEST
    now_cest_dt = datetime.utcnow() + CEST
    weekday     = now_cest_dt.weekday()
    hour_min    = now_cest_dt.hour * 60 + now_cest_dt.minute
    market_open = weekday < 5 and (15 * 60) <= hour_min <= (21 * 60 + 30)
    if not market_open:
        return  # heshtje jashtë orëve të tregut

    setups, watch, no = [], [], []

    for ticker in STOCK_SYMBOLS:
        try:
            hist = yf.Ticker(ticker).history(period="60d", interval="1d", auto_adjust=True)
            if hist.empty or len(hist) < 22:
                no.append(f"{ticker} (pa të dhëna)")
                continue

            closes = [float(x) for x in hist["Close"].tolist()]
            opens  = [float(x) for x in hist["Open"].tolist()]
            highs  = [float(x) for x in hist["High"].tolist()]
            lows   = [float(x) for x in hist["Low"].tolist()]
            vols   = [float(x) for x in hist["Volume"].tolist()]

            ema     = get_ema(closes)
            ema_prev = get_ema(closes[:-3])
            trend   = ema > ema_prev
            cur, opn, hi, lo = closes[-1], opens[-1], highs[-1], lows[-1]
            zone    = ema * 0.005
            in_zone = lo <= ema + zone and hi >= ema - zone
            bounce  = in_zone and cur > ema and cur > opn
            dist    = round((cur - ema) / ema * 100, 2)
            vol_avg = sum(vols[:-1]) / len(vols[:-1])
            vol_ok  = vols[-1] >= vol_avg * 0.6

            if trend and bounce and vol_ok:
                entry  = round(cur, 2)
                sl     = round(min(lows[-2] * 0.999, ema * 0.997), 2)
                rpt    = entry - sl
                sl_pct = round(rpt / entry * 100, 2)
                if sl_pct <= 2.0 and sl < ema:
                    tp     = round(entry + rpt * 2, 2)
                    tp_pct = round(rpt * 2 / entry * 100, 2)
                    setups.append(
                        f"<b>{ticker}</b> LONG (Daily EMA20)\n"
                        f"Entry: ${entry}  |  SL: ${sl} (-{sl_pct}%)  |  TP: ${tp} (+{tp_pct}%)"
                    )
            elif trend and in_zone:
                watch.append(f"{ticker} ({dist:+.2f}%)")
            else:
                reason = "trend down" if not trend else "nuk ka pullback"
                no.append(f"{ticker} ({reason})")
        except Exception as e:
            no.append(f"{ticker} (gabim)")

    now = now_cest()
    # Dërgo vetëm nëse ka setup — heshtje totale nëse jo
    if not setups:
        return
    now = now_cest()
    msg = (f"📈 <b>PLAN B — AKSIONE  |  {now}</b>\n"
           f"<i>Kripto në pritje (BTC Bearish)</i>\n{'─'*28}\n\n"
           f"✅ <b>SETUP:</b>\n" + "\n\n".join(setups))
    send(msg)


def do_scan(triggered_by_command=False, show_loading=True):
    """EMA Sniper — 7 Filter (15m + 1H Confluence) → alle 20 Coins → Chart."""
    if triggered_by_command and show_loading:
        send("Duke skanuar... prit.")

    equity  = get_equity()
    setups, watch, _ = scan_all_symbols(SYMBOLS, equity=equity)

    now = now_cest()
    if setups:
        for s in setups:
            alert_key = f"{s['symbol']}_{s['entry']}"
            if _sl_alerted.get(s["symbol"]) == alert_key:
                continue
            _sl_alerted[s["symbol"]] = alert_key

            chart   = generate_chart(s["symbol"], s["candles_15m"], s["entry"], s["sl"], s["tp"])
            htf_tag = "1H ✅" if s["htf_bull"] else "1H ⚠️"
            caption = (
                f"<b>{s['coin']} LONG  |  {htf_tag}  |  {now}</b>\n"
                f"Entry: ${s['entry']}  |  SL: ${s['sl']} (-{s['sl_pct']}%)"
                f"  |  TP: ${s['tp']} (+{s['tp_pct']}%)\n"
                f"RSI: {s['rsi']}  |  ADX: {s['adx']}"
                f"  |  Risiko: ${s['risk_usd']}\n"
                f"/alarm {s['coin']} {s['entry']} {s['sl']} {s['tp']}"
            )
            send_photo(chart, caption=caption)
    elif triggered_by_command:
        watch_str = " | ".join(f"{w['coin']} ({w['dist_pct']:+.2f}%)" for w in watch)
        msg = f"🎯 <b>EMA Sniper — {now}</b>\nKein Setup (alle 7 Filter bestanden von keinem Coin)."
        if watch_str:
            msg += f"\n👀 Beobachten: {watch_str}"
        send(msg)
    # Kein Setup + Auto-Scan → totale Stille

# ── Morning Briefing (09:00 CEST) ────────────────────────────────────────────
_briefing_done = set()  # dedup per day: {"2026-05-15"}

def fetch_market_sentiment():
    """Fear & Greed Index + BTC Dominance — falas, pa API key."""
    lines = []
    # Fear & Greed
    try:
        d = _req.get("https://api.alternative.me/fng/?limit=1", timeout=8).json()
        fng = d["data"][0]
        val   = int(fng["value"])
        label = fng["value_classification"]
        if val >= 75:   emoji = "🟢"
        elif val >= 55: emoji = "🟡"
        elif val >= 30: emoji = "🟠"
        else:           emoji = "🔴"
        lines.append(f"<b>Fear &amp; Greed:</b> {emoji} {val}/100 — <i>{label}</i>")
    except Exception:
        pass
    # BTC Dominance
    try:
        d = _req.get("https://api.coingecko.com/api/v3/global", timeout=8).json()
        dom = round(d["data"]["market_cap_percentage"]["btc"], 1)
        lines.append(f"<b>BTC Dominance:</b> {dom}%")
    except Exception:
        pass
    return "\n".join(lines) + "\n" if lines else ""


def generate_liquidation_chart():
    """BTC liquidation bar chart (3 ditë, 4h buckets) nga Coinglass. Kthen PNG buffer ose None."""
    if not COINGLASS_KEY:
        return None
    try:
        resp = _req.get(
            "https://open-api.coinglass.com/public/v2/liquidation/chart",
            headers={"coinglassSecret": COINGLASS_KEY},
            params={"symbol": "BTC", "time_type": "h4", "limit": "18"},
            timeout=10
        )
        data = resp.json()
        print(f"[Coinglass LIQ] status={resp.status_code} code={data.get('code')} raw={resp.text[:200]}", flush=True)
        ok   = (data.get("code") == "0") or (data.get("success") is True)
        rows = data.get("data") or []
        if not ok or not rows:
            return None

        # rows: [{time, longLiquidationUsd, shortLiquidationUsd}, ...]
        times  = []
        longs  = []
        shorts = []
        for r in rows:
            ts = int(r.get("time", r.get("t", 0)))
            dt = datetime.utcfromtimestamp(ts / 1000 if ts > 1e10 else ts) + CEST
            times.append(dt.strftime("%d/%m\n%H:%M"))
            longs.append(float(r.get("longLiquidationUsd",  r.get("long",  0))) / 1_000_000)
            shorts.append(float(r.get("shortLiquidationUsd", r.get("short", 0))) / 1_000_000)

        x = range(len(times))
        fig, ax = plt.subplots(figsize=(12, 5), facecolor='#131722')
        ax.set_facecolor('#131722')
        ax.bar([i - 0.2 for i in x], longs,  width=0.38, color='#ff4444', label='Longs likuiduar')
        ax.bar([i + 0.2 for i in x], shorts, width=0.38, color='#00cc66', label='Shorts likuiduar')
        ax.set_xticks(list(x))
        ax.set_xticklabels(times, color='white', fontsize=7)
        ax.tick_params(colors='white')
        ax.yaxis.set_tick_params(labelcolor='white')
        ax.set_ylabel('Milion USD', color='white')
        day_str = (datetime.utcnow() + CEST).strftime("%Y-%m-%d")
        ax.set_title(f'BTC Liquidime — 3 Ditë 4h  ({day_str})', color='white', fontsize=13)
        ax.legend(facecolor='#1e2030', labelcolor='white', fontsize=9)
        for spine in ax.spines.values():
            spine.set_edgecolor('#333344')
        plt.tight_layout()
        buf = io.BytesIO()
        fig.savefig(buf, format='png', dpi=100, bbox_inches='tight', facecolor='#131722')
        plt.close(fig)
        buf.seek(0)
        return buf
    except Exception as e:
        print(f"[Coinglass LIQ] error: {e}", flush=True)
        return None


def fetch_news_today():
    """Merr lajmet High-Impact USD nga ForexFactory për sot (CEST)."""
    try:
        import html as _html
        resp  = _req.get(
            "https://nfs.faireconomy.media/ff_calendar_thisweek.json", timeout=8)
        cal   = resp.json()
        today = (datetime.utcnow() + CEST).strftime("%Y-%m-%d")
        high  = [e for e in cal
                 if e.get("impact") == "High"
                 and e.get("country") == "USD"
                 and e.get("date", "").startswith(today)]
        if not high:
            return "📅 <b>Lajmet sot (USD High):</b> Asnjë lajm i rëndësishëm. ✅\n"
        lines = ["⚠️ <b>Lajmet sot (USD High):</b>"]
        for ev in high:
            try:
                t = (datetime.fromisoformat(ev["date"]) + CEST).strftime("%H:%M")
            except Exception:
                t = "?"
            title = _html.escape(ev.get("title", "?"))
            lines.append(f"  {t} — {title}")
        return "\n".join(lines) + "\n"
    except Exception:
        return "📅 Kalendarit offline.\n"


def morning_briefing(force=False):
    """09:00 CEST: ETF flows + Liquidation Heatmap + News + Setups."""
    day = (datetime.utcnow() + CEST).strftime("%Y-%m-%d")
    if not force:
        if day in _briefing_done:
            return
        _briefing_done.add(day)

    print(f"[Briefing] Starting morning briefing {day}", flush=True)

    sentiment_text = fetch_market_sentiment()
    news_text      = fetch_news_today()

    # Bubble 1: briefing i pastër pa "Duke skanuar..."
    text_part = (
        f"☀️ <b>BRIEFING MËNGJESI — {day}  09:00 CEST</b>\n{'─'*28}\n\n"
        f"{sentiment_text}\n"
        f"{news_text}"
    )

    liq_chart = generate_liquidation_chart()
    if liq_chart:
        send_photo(liq_chart, caption=text_part)
    else:
        send(text_part)

    # Bubble 2: rezultati i skanimit (setup chart OSE status — 1 bubble, pa "Duke skanuar...")
    do_scan(triggered_by_command=True, show_loading=False)


_last_auto_scan = 0.0

def monitor_sl_width():
    """Çdo 20 min ekzekuton do_scan automatikisht."""
    global _last_auto_scan
    while True:
        try:
            if time.time() - _last_auto_scan >= 1200:
                _last_auto_scan = time.time()
                do_scan()
        except Exception: pass
        time.sleep(30)

# ── Haupt-Loop ────────────────────────────────────────────────────────────────
def check_inbox():
    """Liest alarm_inbox.json und startet neue Alarm-Threads."""
    if not os.path.exists(INBOX_FILE):
        return
    try:
        with open(INBOX_FILE) as f:
            inbox = json.load(f)
        os.remove(INBOX_FILE)
        for sym, info in inbox.items():
            coin = sym.replace("USDT","")
            if sym not in active_alerts:
                start_alarm_thread(coin, sym, info["entry"], info.get("sl"), info.get("tp"))
    except: pass

# ── Automatische Scans (09:00 / 16:00 / 17:30 UTC+2 CEST) ───────────────────
_scans_done = set()  # z.B. {"2026-05-15_09", "2026-05-15_16", "2026-05-15_17"}

SCAN_SCHEDULE = [
    (9,  0,  "morgen"),
    (16, 0,  "fruehwarnung"),
    (17, 30, "signal"),
]

def cmd_scan_typed(scan_type):
    """Geplanter Scan (09:00 / 16:00 / 17:30 CEST) mit EMA Sniper 7-Filter."""
    send("Duke skanuar... prit.")

    if scan_type == "fruehwarnung":
        prefix = "⚠️ FRÜHWARNUNG 16:00 — noch nicht einsteigen!\nBeobachte für 17:30:"
        hint   = "Nächster Check: 17:30 für finales Signal."
    elif scan_type == "signal":
        prefix = "✅ SIGNAL 17:30 — Setup bestätigt:"
        hint   = "Alarm setzen: /alarm COIN entry sl tp"
    else:
        prefix = "🌅 MORGEN-SCAN 09:00:"
        hint   = "Weitere Scans: 16:00 (Frühwarnung) & 17:30 (Signal)"

    equity = get_equity()
    setups, watch, _ = scan_all_symbols(SYMBOLS, equity=equity)

    if not setups:
        return  # Totale Stille wenn kein Setup

    now = now_cest()
    lines = []
    for s in setups:
        lines.append(
            f"<b>{s['coin']}</b> LONG\n"
            f"Entry: ${s['entry']} | SL: ${s['sl']} (-{s['sl_pct']}%) | TP: ${s['tp']} (+{s['tp_pct']}%)\n"
            f"RSI: {s['rsi']} | ADX: {s['adx']} | 1H: {'✅' if s['htf_bull'] else '⚠️'}\n"
            f"/alarm {s['coin']} {s['entry']} {s['sl']} {s['tp']}"
        )

    msg = f"<b>{prefix}</b>\n{'─'*28}\n\n" + "\n\n".join(lines)

    # Bester Kandidat (höchster ADX) beim 17:30 Signal-Scan
    if scan_type == "signal":
        best = max(setups, key=lambda x: x["adx"])
        msg += (
            f"\n\n{'─'*28}\n"
            f"🏆 <b>Bester Kandidat: {best['coin']}</b>  "
            f"ADX: <b>{best['adx']}</b>  RSI: {best['rsi']}\n"
            f"{'─'*28}"
        )

    msg += f"\n\n<i>{hint}</i>"
    send(msg)

def maybe_run_scheduled_scans():
    global _scans_done
    now = datetime.utcnow()
    # CEST = UTC+2
    cest_hour   = (now.hour + 2) % 24
    cest_minute = now.minute
    day_key     = now.strftime("%Y-%m-%d")

    for h, m, scan_type in SCAN_SCHEDULE:
        key = f"{day_key}_{h}"
        if cest_hour == h and cest_minute < 3 and key not in _scans_done:
            _scans_done.add(key)
            if scan_type == "morgen":
                threading.Thread(target=morning_briefing, daemon=True).start()
            else:
                threading.Thread(target=cmd_scan_typed, args=(scan_type,), daemon=True).start()

def run_auto_scan_loop():
    while True:
        try:
            maybe_run_scheduled_scans()
        except: pass
        time.sleep(60)

def main():
    global offset

    # Gespeicherte Alarme beim Start wiederherstellen
    if os.path.exists(ALARMS_FILE):
        try:
            with open(ALARMS_FILE) as f:
                saved = json.load(f)
            for sym, info in saved.items():
                coin = sym.replace("USDT","")
                start_alarm_thread(coin, sym, info["entry"], info.get("sl"), info.get("tp"), notify=False)
            if saved:
                names = ", ".join(s.replace("USDT","") for s in saved)
                send(f"Boti u rinis. Alarmet u rikthyen: <b>{names}</b>")
        except: pass

    threading.Thread(target=run_auto_scan_loop, daemon=True).start()
    threading.Thread(target=monitor_sl_width, daemon=True).start()
    threading.Thread(target=monitor_btc_emergency, daemon=True).start()

    send("🤖 Boti startoi! Shkruaj /hilfe për të parë të gjitha komandat.")
    print("[Bot] Läuft. Strg+C zum Beenden.", flush=True)

    while True:
        try:
            check_inbox()
            updates = get_updates(offset)
            for u in updates:
                offset = u["update_id"] + 1
                msg    = u.get("message", {})
                text   = msg.get("text", "").strip()
                cid    = str(msg.get("chat", {}).get("id", ""))

                if cid != CHAT_ID: continue
                if not text.startswith("/"): continue

                parts = text.split()
                cmd   = parts[0].lower()
                print(f"[Bot] Befehl: {text}", flush=True)

                if cmd == "/hilfe":          cmd_hilfe()
                elif cmd == "/status":       cmd_status()
                elif cmd == "/scan":         threading.Thread(target=do_scan, args=(True,), daemon=True).start()
                elif cmd == "/briefing":     cmd_briefing()
                elif cmd == "/price":        cmd_price(parts)
                elif cmd == "/alarm":        cmd_alarm(parts)
                elif cmd == "/alarme":       cmd_alarme()
                elif cmd == "/stop":         cmd_stop(parts)
                elif cmd == "/trade":        cmd_trade(parts)
                elif cmd == "/trades":       cmd_trades()
                elif cmd == "/stoptrade":    cmd_stoptrade(parts)
                else: send("Komandë e panjohur. Shkruaj /hilfe")

        except KeyboardInterrupt:
            send("Bot gestoppt.")
            print("[Bot] Beendet.", flush=True)
            break
        except: time.sleep(5)

if __name__ == "__main__":
    main()
