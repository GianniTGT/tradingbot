#!/usr/bin/env python3
"""
GianniTGTradingBot — Interaktiver Trading Bot
Befehle die du im Telegram schreiben kannst:
  /scan           — EMA20 Scan aller 15 Coins (4h)
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

active_alerts = {}  # { "SOLUSDT": {"entry":..,"sl":..,"tp":..,"thread":..} }
active_trades = {}  # { "SOLUSDT": {"entry":..,"sl":..,"tp":..,"thread":..} }
_lock = threading.Lock()
POSITION_SIZE = float(os.environ.get("POSITION_SIZE", "0"))
if not POSITION_SIZE:
    print("[Bot] Hint: POSITION_SIZE not set — PnL calculation disabled.", flush=True)
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
def cmd_hilfe():
    send(
        "<b>GianniTGT Trading Bot 🤖</b>\n\n"
        "/scan — Skano 20 coins (EMA20)\n"
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
    """Skan manual — me BTC filtër."""
    btc_ok, btc_emoji, btc_desc = get_btc_status()
    send(f"Duke skanuar 20 coins... prit.\n{btc_emoji} {btc_desc}")
    results = {"setup": [], "watch": [], "no": []}

    if not btc_ok:
        send(f"🔴 <b>{btc_desc}</b>\nNuk skanohet kur BTC është bearish.")
        return

    for sym in SYMBOLS:
        coin = sym.replace("USDT","")
        try:
            url4 = f"https://api.binance.com/api/v3/klines?symbol={sym}&interval=4h&limit=50"
            urld = f"https://api.binance.com/api/v3/klines?symbol={sym}&interval=1d&limit=25"
            with urlopen(url4, timeout=8) as r: d4 = json.loads(r.read())
            with urlopen(urld, timeout=8) as r: dd = json.loads(r.read())

            c4 = [float(k[4]) for k in d4]
            o4 = [float(k[1]) for k in d4]
            l4 = [float(k[3]) for k in d4]
            h4 = [float(k[2]) for k in d4]
            cd = [float(k[4]) for k in dd]

            ema4h = get_ema(c4); ema4h_prev = get_ema(c4[:-3])
            emad  = get_ema(cd)

            trend4 = ema4h > ema4h_prev
            trendd = cd[-1] > emad
            zone   = ema4h * 0.005
            inZone = l4[-1] <= ema4h+zone and h4[-1] >= ema4h-zone
            bounce = inZone and c4[-1] > ema4h and c4[-1] > o4[-1]
            dist   = round((c4[-1]-ema4h)/ema4h*100, 2)

            if trend4 and trendd and bounce:
                entry  = round_price(c4[-1])
                sl     = round_price(min(l4[-2]*0.999, ema4h*0.997))
                rpt    = entry - sl
                slpct  = round(rpt/entry*100, 2)
                if slpct <= 1.5:
                    tp = round_price(entry + rpt*2)
                    results["setup"].append(f"<b>{coin}</b> LONG\nEntry: ${entry} | SL: ${sl} (-{slpct}%) | TP: ${tp}\n/alarm {coin} {entry} {sl} {tp}")
            elif trend4 and trendd and inZone:
                results["watch"].append(f"{coin} ({dist:+.2f}% nga EMA20)")
            else:
                r = "Daily bearish" if not trendd else "4h bearish" if not trend4 else "nuk ka pullback"
                results["no"].append(f"{coin} ({r})")
        except:
            results["no"].append(f"{coin} (gabim)")

    msg = f"{btc_emoji} <b>{btc_desc}</b>\n<b>SKAN EMA20 — {now_cest()}</b>\n\n"
    if results["setup"]:
        msg += "✅ SETUP:\n" + "\n\n".join(results["setup"]) + "\n\n"
    if results["watch"]:
        msg += "👀 SHIQO KËTA:\n" + " | ".join(results["watch"]) + "\n\n"
    if not results["setup"] and not results["watch"]:
        msg += "Nuk ka setup. Prit konsolidim.\n\n"
    msg += "❌ PA SETUP:\n" + " | ".join(results["no"])
    send(msg)

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

def generate_chart(sym, raw_candles, entry, sl, tp):
    """Gjeneron PNG 4h candlestick me EMA20 + nivelet entry/SL/TP."""
    candles = raw_candles[-60:]
    times   = [pd.Timestamp(int(k[0]), unit='ms') for k in candles]
    df = pd.DataFrame({
        'Open':   [float(k[1]) for k in candles],
        'High':   [float(k[2]) for k in candles],
        'Low':    [float(k[3]) for k in candles],
        'Close':  [float(k[4]) for k in candles],
        'Volume': [float(k[5]) for k in candles],
    }, index=pd.DatetimeIndex(times))

    all_closes = [float(k[4]) for k in raw_candles]
    k_m, e = 2 / 21, all_closes[0]
    all_emas = []
    for c in all_closes:
        e = c * k_m + e * (1 - k_m)
        all_emas.append(e)
    ema_s = pd.Series(all_emas[-60:], index=pd.DatetimeIndex(times))

    ap = [mpf.make_addplot(ema_s, color='cyan', width=1.5)]
    hl = dict(hlines=[entry, sl, tp],
              colors=['#3399ff', '#ff4444', '#00cc44'],
              linewidths=[1.2, 1.2, 1.2], linestyle='--')
    buf = io.BytesIO()
    fig, _ = mpf.plot(df, type='candle', style='nightclouds', addplot=ap, hlines=hl,
                      title=f'\n{sym} – 4h  |  Entry ${entry}  SL ${sl}  TP ${tp}',
                      figsize=(12, 7), returnfig=True)
    fig.savefig(buf, format='png', dpi=100, bbox_inches='tight', facecolor='#131722')
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

def do_scan(triggered_by_command=False):
    """BTC 4h EMA20 gatekeeper → skanoj coins me filtër cilësie → chart."""
    if triggered_by_command:
        send("Duke skanuar... prit.")

    # BTC 4h EMA20 kontrollo
    try:
        url4 = "https://api.binance.com/api/v3/klines?" + urlencode(
            {"symbol": "BTCUSDT", "interval": "4h", "limit": 50})
        with urlopen(url4, timeout=8) as r:
            d4_btc = json.loads(r.read())
        c4_btc    = [float(k[4]) for k in d4_btc]
        ema_btc   = get_ema(c4_btc)
        btc_price = round(c4_btc[-1], 2)
        btc_ema   = round(ema_btc, 2)
        btc_bull  = c4_btc[-1] > ema_btc
    except Exception:
        if triggered_by_command:
            send("Gabim: nuk arrita të marr të dhënat e BTC.")
        return

    if not btc_bull:
        send(
            "Për momentin nuk ka setup-e të mira.\n"
            f"BTC është nën EMA20 — ${btc_price} (EMA: ${btc_ema}) — Bearish.\n\n"
            "Presim një ambient më të sigurt tregtar."
        )
        return

    setups, watch = [], []

    for sym in SYMBOLS:
        if sym == "BTCUSDT":
            continue
        coin = sym.replace("USDT", "")
        try:
            url4 = "https://api.binance.com/api/v3/klines?" + urlencode(
                {"symbol": sym, "interval": "4h", "limit": 60})
            urld = "https://api.binance.com/api/v3/klines?" + urlencode(
                {"symbol": sym, "interval": "1d", "limit": 25})
            with urlopen(url4, timeout=8) as r: d4 = json.loads(r.read())
            with urlopen(urld, timeout=8) as r: dd = json.loads(r.read())

            c4 = [float(k[4]) for k in d4]
            o4 = [float(k[1]) for k in d4]
            l4 = [float(k[3]) for k in d4]
            h4 = [float(k[2]) for k in d4]
            v4 = [float(k[5]) for k in d4]
            cd = [float(k[4]) for k in dd]

            ema4h      = get_ema(c4)
            ema4h_prev = get_ema(c4[:-3])
            emad       = get_ema(cd)
            candle_ts  = str(d4[-1][0])

            trend4 = ema4h > ema4h_prev
            trendd = cd[-1] > emad
            zone   = ema4h * 0.005
            inZone = l4[-1] <= ema4h + zone and h4[-1] >= ema4h - zone
            bounce = inZone and c4[-1] > ema4h and c4[-1] > o4[-1]
            dist   = round((c4[-1] - ema4h) / ema4h * 100, 2)

            if trend4 and trendd and bounce:
                entry = round_price(c4[-1])
                sl    = round_price(min(l4[-2] * 0.999, ema4h * 0.997))
                rpt   = entry - sl

                # Filtrat e cilësisë
                if sl >= ema4h: continue  # SL mbi EMA — setup i keq
                candle_range = h4[-1] - l4[-1]
                body_ratio   = (c4[-1] - o4[-1]) / candle_range if candle_range > 0 else 0
                if body_ratio < 0.3: continue  # kandelë indecisive
                vol_avg = sum(v4[:-1]) / len(v4[:-1])
                if v4[-1] < vol_avg * 0.6: continue  # volum shumë i dobët

                slpct     = round(rpt / entry * 100, 2)
                alert_key = f"{sym}_{candle_ts}"
                if slpct <= 1.5 and _sl_alerted.get(sym) != alert_key:
                    _sl_alerted[sym] = alert_key
                    tp    = round_price(entry + rpt * 2)
                    tppct = round(rpt * 2 / entry * 100, 2)
                    chart = generate_chart(sym, d4, entry, sl, tp)
                    setups.append({"coin": coin, "entry": entry, "sl": sl, "tp": tp,
                                   "slpct": slpct, "tppct": tppct, "chart": chart})

            elif trend4 and trendd and inZone:
                watch.append(f"{coin} ({dist:+.2f}%)")

        except Exception:
            pass

    now = now_cest()
    if setups:
        for s in setups:
            caption = (f"<b>{s['coin']} LONG  |  BTC ✅  |  {now}</b>\n"
                       f"Entry: ${s['entry']}  |  SL: ${s['sl']} (-{s['slpct']}%)  "
                       f"|  TP: ${s['tp']} (+{s['tppct']}%)\n"
                       f"/alarm {s['coin']} {s['entry']} {s['sl']} {s['tp']}")
            send_photo(s["chart"], caption=caption)
        if watch:
            send(f"👀 <i>Afër EMA20: {' | '.join(watch)}</i>")
    else:
        msg = f"<b>Skan — {now}  |  BTC ✅</b>\nAktualisht asnjë setup i mirë."
        if watch:
            msg += f"\n👀 Afër EMA20: {' | '.join(watch)}"
        send(msg)

# ── Morning Briefing (09:00 CEST) ────────────────────────────────────────────
_briefing_done = set()  # dedup per day: {"2026-05-15"}

def fetch_etf_flows():
    """Merr BTC ETF net flows nga Coinglass. Kthen tekst të formatuar."""
    if not COINGLASS_KEY:
        return "ETF flows: COINGLASS_API_KEY nuk është vendosur.\n"
    try:
        resp = _req.get(
            "https://open-api.coinglass.com/public/v2/etf/bitcoin_etf_flow_all_list",
            headers={"coinglassSecret": COINGLASS_KEY},
            timeout=10
        )
        data = resp.json()
        if data.get("code") != "0" or not data.get("data"):
            return "ETF flows: të dhënat nuk janë të disponueshme.\n"
        rows   = data["data"]
        recent = rows[:3]  # 3 ditët e fundit
        lines  = ["<b>BTC ETF Flows (mln USD):</b>"]
        for row in recent:
            date  = row.get("date", "?")
            total = float(row.get("total", 0))
            sign  = "🟢 +" if total > 0 else ("🔴 " if total < 0 else "⚪ ")
            lines.append(f"  {date}: {sign}{total:.1f}M")
        total_3d = sum(float(r.get("total", 0)) for r in recent)
        sign_3d  = "🟢 +" if total_3d > 0 else "🔴 "
        lines.append(f"  3-ditore: {sign_3d}{total_3d:.1f}M")
        return "\n".join(lines) + "\n"
    except Exception as e:
        return f"ETF flows: gabim ({e})\n"


def generate_liquidation_heatmap():
    """Gjeneron BTC liquidation heatmap 3-ditore si PNG buffer."""
    try:
        import numpy as np
        import html as _html
        now_ms   = int(time.time() * 1000)
        start_ms = now_ms - 3 * 24 * 3600 * 1000

        url  = ("https://fapi.binance.com/fapi/v1/allForceOrders?"
                f"symbol=BTCUSDT&startTime={start_ms}&limit=1000")
        resp = _req.get(url, timeout=12)
        orders = resp.json()
        if not orders or not isinstance(orders, list):
            return None

        liq_data = []
        for o in orders:
            ts     = int(o.get("time", 0))
            price  = float(o.get("avgPrice") or o.get("price") or 0)
            qty    = float(o.get("origQty", 0))
            if price > 0 and qty > 0:
                liq_data.append((ts, price, price * qty))

        if not liq_data:
            return None

        times   = [d[0] for d in liq_data]
        prices  = [d[1] for d in liq_data]

        t_min, t_max = min(times),  max(times)
        p_min, p_max = min(prices), max(prices)
        p_pad  = (p_max - p_min) * 0.03
        p_min -= p_pad; p_max += p_pad

        N_TIME, N_PRICE = 24, 30
        grid = np.zeros((N_PRICE, N_TIME))
        for ts, price, vol in liq_data:
            ti = min(int((ts - t_min) / max(t_max - t_min, 1) * N_TIME), N_TIME - 1)
            pi = min(int((price - p_min) / max(p_max - p_min, 1) * N_PRICE), N_PRICE - 1)
            grid[pi, ti] += vol / 1_000_000  # in millions USD

        fig, ax = plt.subplots(figsize=(12, 7), facecolor='#131722')
        ax.set_facecolor('#131722')

        im = ax.imshow(grid, aspect='auto', origin='lower',
                       cmap='hot', interpolation='nearest')

        x_ticks = list(range(0, N_TIME, 4))
        x_labels = []
        for i in x_ticks:
            ts_mid = t_min + (i + 0.5) * (t_max - t_min) / N_TIME
            dt = datetime.utcfromtimestamp(ts_mid / 1000) + CEST
            x_labels.append(dt.strftime("%d/%m\n%H:%M"))
        ax.set_xticks(x_ticks)
        ax.set_xticklabels(x_labels, color='white', fontsize=8)

        y_ticks = list(range(0, N_PRICE, 5))
        y_labels = [f"${p_min + j * (p_max - p_min) / N_PRICE:,.0f}" for j in y_ticks]
        ax.set_yticks(y_ticks)
        ax.set_yticklabels(y_labels, color='white', fontsize=8)

        cbar = fig.colorbar(im, ax=ax, pad=0.02)
        cbar.set_label('Liquidim (M USD)', color='white')
        plt.setp(cbar.ax.yaxis.get_ticklabels(), color='white')

        day_str = (datetime.utcnow() + CEST).strftime("%Y-%m-%d")
        ax.set_title(f'BTC Liquidation Heatmap — 3 Ditë  ({day_str})',
                     color='white', fontsize=13, pad=10)
        ax.set_xlabel('Koha (CEST)', color='white')
        ax.set_ylabel('Çmimi BTC (USD)', color='white')
        ax.tick_params(colors='white')
        for spine in ax.spines.values():
            spine.set_edgecolor('#333344')

        plt.tight_layout()
        buf = io.BytesIO()
        fig.savefig(buf, format='png', dpi=100, bbox_inches='tight', facecolor='#131722')
        plt.close(fig)
        buf.seek(0)
        return buf
    except Exception as e:
        print(f"[Briefing] Heatmap error: {e}", flush=True)
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


def morning_briefing():
    """09:00 CEST: ETF flows + Liquidation Heatmap + News + Setups."""
    day = (datetime.utcnow() + CEST).strftime("%Y-%m-%d")
    if day in _briefing_done:
        return
    _briefing_done.add(day)

    print(f"[Briefing] Starting morning briefing {day}", flush=True)
    send(f"☕ <b>BRIEFING MËNGJESI — {day}  09:00 CEST</b>\nDuke mbledhur të dhënat...")

    etf_text  = fetch_etf_flows()
    news_text = fetch_news_today()

    heatmap_buf = generate_liquidation_heatmap()
    caption = (f"☀️ <b>BRIEFING {day}</b>\n{'─'*28}\n\n"
               f"{etf_text}\n{news_text}")
    if heatmap_buf:
        send_photo(heatmap_buf, caption=caption)
    else:
        send(caption + "\n⚠️ Heatmap nuk u gjenerua.")

    send("🔍 Duke skanuar setups për sot...")
    do_scan()


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
    """Scan me filtër BTC Boss dhe prefix sipas orës."""
    send("Duke skanuar... prit.")

    # ── BTC Boss Filtër ───────────────────────────────────────────────────────
    btc_ok, btc_emoji, btc_desc = get_btc_status()

    if scan_type == "fruehwarnung":
        prefix = "⚠️ PARALAJMËRIM 16:00 — mos hyr ende!\nVëzhgo këta coins për 17:30:"
        hint   = "Kontrolli tjetër: 17:30 për sinjal final."
    elif scan_type == "signal":
        prefix = "✅ SINJAL 17:30 — Setup i konfirmuar:"
        hint   = "Vendos alarmin: /alarm COIN entry sl tp"
    else:
        prefix = "🌅 SKAN MËNGJESIT 09:00:"
        hint   = "Skanet tjera: 16:00 (paralajmërim) & 17:30 (sinjal)"

    # Nëse BTC Bearish → nuk skanojmë altcoins
    if not btc_ok:
        send(
            f"{btc_emoji} <b>{btc_desc}</b>\n"
            f"{'─'*28}\n"
            f"Boti nuk skanon altcoins kur BTC është bearish.\n"
            f"Prit që BTC të kthehet mbi EMA20 dhe provo sërish."
        )
        return

    results  = {"setup": [], "watch": [], "no": []}
    vol_rank = []  # për "Bester Kandidat" 17:30

    for sym in SYMBOLS:
        coin = sym.replace("USDT","")
        try:
            url4 = f"https://api.binance.com/api/v3/klines?symbol={sym}&interval=4h&limit=50"
            urld = f"https://api.binance.com/api/v3/klines?symbol={sym}&interval=1d&limit=25"
            with urlopen(url4, timeout=8) as r: d4 = json.loads(r.read())
            with urlopen(urld, timeout=8) as r: dd = json.loads(r.read())

            c4 = [float(k[4]) for k in d4]
            o4 = [float(k[1]) for k in d4]
            l4 = [float(k[3]) for k in d4]
            h4 = [float(k[2]) for k in d4]
            v4 = [float(k[5]) for k in d4]
            cd = [float(k[4]) for k in dd]

            ema4h = get_ema(c4); ema4h_prev = get_ema(c4[:-3])
            emad  = get_ema(cd)

            trend4  = ema4h > ema4h_prev
            trendd  = cd[-1] > emad
            zone    = ema4h * 0.005
            inZone  = l4[-1] <= ema4h+zone and h4[-1] >= ema4h-zone
            bounce  = inZone and c4[-1] > ema4h and c4[-1] > o4[-1]
            dist    = round((c4[-1]-ema4h)/ema4h*100, 2)
            vol_avg = sum(v4[:-1]) / len(v4[:-1])
            vol_rel = round(v4[-1] / vol_avg, 2)  # >1 = überdurchschnittlich

            if trend4 and trendd and bounce:
                entry  = round_price(c4[-1])
                sl     = round_price(min(l4[-2]*0.999, ema4h*0.997))
                rpt    = entry - sl
                slpct  = round(rpt/entry*100, 2)
                if slpct <= 1.5:
                    tp = round_price(entry + rpt*2)
                    results["setup"].append(
                        f"<b>{coin}</b> LONG\nEntry: ${entry} | SL: ${sl} (-{slpct}%) | TP: ${tp}\n"
                        f"/alarm {coin} {entry} {sl} {tp}"
                    )
                    vol_rank.append((coin, vol_rel, "setup"))
            elif trend4 and trendd and inZone:
                results["watch"].append(f"{coin} ({dist:+.2f}%)")
                vol_rank.append((coin, vol_rel, "watch"))
            else:
                r = "Daily bear" if not trendd else "4h bear" if not trend4 else "kein PB"
                results["no"].append(f"{coin} ({r})")
        except:
            results["no"].append(f"{coin} (Fehler)")

    msg = f"{btc_emoji} <b>{btc_desc}</b>\n<b>{prefix}</b>\n{'─'*28}\n\n"
    if results["setup"]:
        msg += "SETUPS:\n" + "\n\n".join(results["setup"]) + "\n\n"
    if results["watch"]:
        msg += "BEOBACHTEN:\n" + " | ".join(results["watch"]) + "\n\n"
    if not results["setup"] and not results["watch"]:
        msg += "Keine Setups. Markt abwarten.\n\n"

    # Bester Kandidat nur beim 17:30 Signal-Scan
    if scan_type == "signal" and vol_rank:
        best = max(vol_rank, key=lambda x: x[1])
        coin_b, vol_b, typ_b = best
        vol_str = f"{vol_b}x Durchschnitt"
        flag    = "✅ Setup aktiv" if typ_b == "setup" else "👀 Afër EMA20, pret bounce"
        msg += f"{'─'*28}\n🏆 <b>Bester Kandidat Abend-Trade: {coin_b}</b>\nVolumen letzte 4h: <b>{vol_str}</b> — {flag}\n{'─'*28}\n\n"

    msg += f"<i>{hint}</i>"
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
                elif cmd == "/scan":         threading.Thread(target=do_scan, args=(True,), daemon=True).start()
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
