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
import json, time, threading, os
from urllib.request import urlopen, Request
from urllib.parse import urlencode
from urllib.error import URLError
from datetime import datetime

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
    data = {sym: {"entry": v["entry"], "sl": v["sl"], "tp": v["tp"]}
            for sym, v in active_alerts.items()}
    with open(ALARMS_FILE, "w") as f:
        json.dump(data, f)

def start_alarm_thread(coin, symbol, entry, sl, tp, notify=True):
    if symbol in active_alerts:
        return
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
                    active_alerts.pop(symbol, None)
                    save_alarms()
                    break
                last_price = price
                time.sleep(20)
            except: time.sleep(30)

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
    url = f"https://api.binance.com/api/v3/ticker/price?symbol={symbol}"
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

    msg = f"{btc_emoji} <b>{btc_desc}</b>\n<b>SKAN EMA20 — {datetime.now().strftime('%H:%M')}</b>\n\n"
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

# ── SL Monitor: njofton kur SL < 1.5% pranë EMA20 ───────────────────────────
_sl_alerted = {}  # { "BNBUSDT": "2026-05-15_candle_timestamp" }

def monitor_sl_width():
    """Çdo 20 min kontrollon nëse ndonjë coin ka ngadalësuar pranë EMA20."""
    while True:
        try:
            # BTC Boss Filtër — nëse bearish, nuk kontrollon altcoins
            btc_ok, btc_emoji, btc_desc = get_btc_status()
            if not btc_ok:
                time.sleep(1200)
                continue

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

                    candle_ts = str(d4[-1][0])  # timestamp kandela aktuale

                    ema4h      = get_ema(c4)
                    ema4h_prev = get_ema(c4[:-3])
                    emad       = get_ema(cd)

                    trend4  = ema4h > ema4h_prev
                    trendd  = cd[-1] > emad
                    zone    = ema4h * 0.005
                    inZone  = l4[-1] <= ema4h + zone and h4[-1] >= ema4h - zone
                    bounce  = inZone and c4[-1] > ema4h and c4[-1] > o4[-1]

                    if not (trend4 and trendd and (inZone or bounce)):
                        continue

                    entry   = round_price(c4[-1])
                    sl      = round_price(min(l4[-2] * 0.999, ema4h * 0.997))
                    risk_pt = entry - sl
                    sl_pct  = round(risk_pt / entry * 100, 2)

                    # Vetëm nëse SL < 1.5% dhe nuk kemi njoftuar tashmë për këtë kandelë
                    alert_key = f"{sym}_{candle_ts}"
                    if sl_pct <= 1.5 and _sl_alerted.get(sym) != alert_key:
                        _sl_alerted[sym] = alert_key
                        tp     = round_price(entry + risk_pt * 2)
                        tp_pct = round(risk_pt * 2 / entry * 100, 2)
                        status = "✅ Bounce konfirmuar" if bounce else "👀 Në zonë, pret bounce"
                        send(
                            f"📉➡️📈 <b>{coin} ka ngadalësuar pranë EMA20!</b>\n"
                            f"{'─'*28}\n"
                            f"SL: <b>{sl_pct}%</b> — brenda kufirit 1.5% ✅\n"
                            f"Status: {status}\n\n"
                            f"Entry: ${entry}\n"
                            f"Stop Loss: ${sl}  (-{sl_pct}%)\n"
                            f"Take Profit: ${tp}  (+{tp_pct}%)  [2:1]\n\n"
                            f"/alarm {coin} {entry} {sl} {tp}"
                        )
                except:
                    continue
        except: pass
        time.sleep(1200)  # kontrollo çdo 20 minuta

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
                elif cmd == "/scan":         threading.Thread(target=cmd_scan_typed, args=("morgen",), daemon=True).start()
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
