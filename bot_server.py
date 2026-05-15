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
from datetime import datetime
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
    except Exception:
        raise RuntimeError("Kein TELEGRAM_TOKEN / TELEGRAM_CHAT_ID gesetzt.")

ALARMS_FILE = os.environ.get("ALARMS_FILE", os.path.join(os.path.dirname(__file__), "alarms.json"))
INBOX_FILE  = os.environ.get("INBOX_FILE",  os.path.join(os.path.dirname(__file__), "alarm_inbox.json"))

CHAT_ID = str(CHAT_ID)  # normalise to str regardless of JSON int vs env string

SYMBOLS = ["BTCUSDT","ETHUSDT","SOLUSDT","BNBUSDT","LINKUSDT",
           "NEARUSDT","AVAXUSDT","MAGICUSDT","DOTUSDT","ADAUSDT","XRPUSDT",
           "SUIUSDT","INJUSDT","APTUSDT","ARBUSDT",
           "MATICUSDT","OPUSDT","DOGEUSDT","ATOMUSDT","LTCUSDT"]

active_alerts = {}  # { "SOLUSDT": {"entry":..,"sl":..,"tp":..,"thread":..} }
active_trades = {}  # { "SOLUSDT": {"entry":..,"sl":..,"tp":..,"thread":..} }
_lock = threading.Lock()
POSITION_SIZE = float(os.environ.get("POSITION_SIZE", "0"))
if not POSITION_SIZE:
    print("[Bot] Hinweis: POSITION_SIZE nicht gesetzt — PnL-Berechnung deaktiviert.", flush=True)
offset = 0

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
                send(f"Alarm gesetzt: <b>{coin}</b>\nEntry: ${entry} | SL: ${sl} | TP: ${tp}")
            else:
                send(f"Alarm gesetzt: <b>{coin}</b> bei ${entry}")
        last_price = None
        while symbol in active_alerts:
            try:
                price = get_price(symbol)
                triggered = price <= entry and (last_price is None or last_price > entry)
                if triggered:
                    if sl and tp:
                        send(
                            f"ENTRY ERREICHT: <b>{coin}</b>\n"
                            f"Preis: <b>${price}</b> | Entry: ${entry}\n"
                            f"Stop Loss: ${sl}\n"
                            f"Take Profit: ${tp}\n\n"
                            f"JETZT EINSTEIGEN!\n"
                            f"/trade {coin} {entry} {sl} {tp}"
                        )
                    else:
                        send(f"ALARM: <b>{coin}</b> hat ${entry} erreicht!\nAktuell: <b>${price}</b>")
                    for _ in range(3):
                        time.sleep(60)
                        if symbol not in active_alerts: break
                        send(f"Erinnerung: <b>{coin}</b> bei ${entry}!")
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
    except Exception: return {}

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
        "<b>GianniTGT Trading Bot</b>\n\n"
        "/scan — EMA20 Scan alle 15 Coins\n"
        "/price SOL — Aktueller Preis\n"
        "/alarm SOL 91.05 89.64 93.87 — Alarm bei Entry\n"
        "/alarme — Aktive Alarme anzeigen\n"
        "/stop SOL — Alarm stoppen\n\n"
        "/trade SOL 91.05 89.64 93.87 — Laufenden Trade ueberwachen\n"
        "/trades — Laufende Trades anzeigen\n"
        "/stoptrade SOL — Trade-Ueberwachung stoppen\n\n"
        "/hilfe — Diese Liste"
    )

def cmd_price(parts):
    if len(parts) < 2:
        send("Verwendung: /price SOL"); return
    coin   = parts[1].upper().replace("USDT","")
    symbol = coin + "USDT"
    if symbol not in SYMBOLS:
        send(f"Unbekannter Coin: {coin}."); return
    try:
        price = get_price(symbol)
        send(f"<b>{coin}/USDT</b>: ${price}")
    except Exception:
        send(f"Coin {coin} nicht gefunden.")

def cmd_alarm(parts):
    # Format A: /alarm BNB 674.50            (einfacher Preisalarm)
    # Format B: /alarm BNB 674.50 663.20 685 (mit SL + TP)
    if len(parts) < 3:
        send("Verwendung:\n/alarm BNB 674.50\n/alarm BNB 674.50 663.20 685.00"); return
    coin   = parts[1].upper().replace("USDT","")
    symbol = coin + "USDT"
    if symbol not in SYMBOLS:
        send(f"Unbekannter Coin: {coin}."); return
    try:
        entry = float(parts[2])
        sl    = float(parts[3]) if len(parts) > 3 else None
        tp    = float(parts[4]) if len(parts) > 4 else None
    except Exception:
        send("Ungültige Zahlen."); return

    with _lock:
        if symbol in active_alerts:
            send(f"Alarm für {coin} läuft bereits. /stop {coin} zum Beenden."); return

    start_alarm_thread(coin, symbol, entry, sl, tp)

def cmd_alarme():
    with _lock:
        snapshot = dict(active_alerts)
    if not snapshot:
        send("Keine aktiven Alarme."); return
    msg = "<b>Aktive Alarme:</b>\n\n"
    for sym, info in snapshot.items():
        coin = sym.replace("USDT","")
        try:
            cur = get_price(sym)
            diff = round((cur - info["entry"]) / info["entry"] * 100, 2)
            dist = f"${cur} ({diff:+.2f}%)"
        except Exception:
            dist = "?"
        sl_tp = f" | SL ${info['sl']} | TP ${info['tp']}" if info["sl"] else ""
        msg += f"• <b>{coin}</b> → Alarm bei ${info['entry']}{sl_tp}\n  Jetzt: {dist}\n\n"
    msg += "/stop COIN — Alarm beenden"
    send(msg)

def cmd_stop(parts):
    if len(parts) < 2:
        send("Verwendung: /stop SOL"); return
    coin   = parts[1].upper().replace("USDT","")
    symbol = coin + "USDT"
    if symbol not in SYMBOLS:
        send(f"Unbekannter Coin: {coin}."); return
    with _lock:
        found = active_alerts.pop(symbol, None) is not None
    if found:
        save_alarms()
        send(f"Alarm für <b>{coin}</b> gestoppt.")
    else:
        send(f"Kein aktiver Alarm für {coin}.")

# ── Trade Monitoring ──────────────────────────────────────────────────────────
def cmd_trade(parts):
    if len(parts) < 5:
        send("Verwendung: /trade BNB 668.06 663.20 677.78 [pos_size]"); return
    coin   = parts[1].upper().replace("USDT","")
    symbol = coin + "USDT"
    if symbol not in SYMBOLS:
        send(f"Unbekannter Coin: {coin}."); return
    try:
        entry, sl, tp = float(parts[2]), float(parts[3]), float(parts[4])
        pos_size = float(parts[5]) if len(parts) > 5 else POSITION_SIZE
    except Exception:
        send("Ungültige Zahlen."); return

    with _lock:
        if symbol in active_trades:
            send(f"Trade-Überwachung für {coin} läuft bereits."); return

    def monitor_trade():
        rr   = round((tp - entry) / (entry - sl), 1)
        send(
            f"Trade aktiv: <b>{coin} LONG</b>\n"
            f"Entry: ${entry}\n"
            f"SL: ${sl} | TP: ${tp}\n"
            f"RR: {rr}:1\n"
            f"Ich benachrichtige dich bei SL oder TP."
        )
        last_update = time.time()
        while symbol in active_trades:
            try:
                price = get_price(symbol)
                now   = time.time()

                if now - last_update >= 14400:
                    pct = round((price - entry) / entry * 100, 2)
                    send(f"Update <b>{coin}</b>: ${price} ({pct:+.2f}% seit Entry)")
                    last_update = now

                if price <= sl:
                    pnl_str = f"\nVerlust: ~${round((entry - price) * pos_size, 2)}" if pos_size else ""
                    send(
                        f"STOP LOSS GETROFFEN: <b>{coin}</b>\n"
                        f"SL: ${sl} | Preis: ${price}{pnl_str}\n\n"
                        f"Trade ist beendet. Kein Stress, naechstes Setup kommt."
                    )
                    with _lock:
                        active_trades.pop(symbol, None)
                    break

                if price >= tp:
                    pnl_str = f"\nGewinn: ~${round((price - entry) * pos_size, 2)}" if pos_size else ""
                    send(
                        f"TAKE PROFIT ERREICHT: <b>{coin}</b>\n"
                        f"TP: ${tp} | Preis: ${price}{pnl_str}\n\n"
                        f"Maschallah! Trade schliessen."
                    )
                    with _lock:
                        active_trades.pop(symbol, None)
                    break

                time.sleep(20)
            except Exception: time.sleep(30)

    t = threading.Thread(target=monitor_trade, daemon=True)
    with _lock:
        active_trades[symbol] = {"entry": entry, "sl": sl, "tp": tp, "thread": t}
    t.start()

def cmd_trades():
    with _lock:
        snapshot = dict(active_trades)
    if not snapshot:
        send("Keine laufenden Trades."); return
    msg = "<b>Laufende Trades:</b>\n\n"
    for sym, info in snapshot.items():
        try:
            price = get_price(sym)
            pct   = round((price - info["entry"]) / info["entry"] * 100, 2)
            msg  += f"<b>{sym.replace('USDT','')}</b>: ${price} ({pct:+.2f}%)\nEntry ${info['entry']} | SL ${info['sl']} | TP ${info['tp']}\n\n"
        except Exception:
            msg += f"<b>{sym.replace('USDT','')}</b>: Entry ${info['entry']} | SL ${info['sl']} | TP ${info['tp']}\n\n"
    send(msg)

def cmd_stoptrade(parts):
    if len(parts) < 2:
        send("Verwendung: /stoptrade SOL"); return
    coin   = parts[1].upper().replace("USDT","")
    symbol = coin + "USDT"
    if symbol not in SYMBOLS:
        send(f"Unbekannter Coin: {coin}."); return
    with _lock:
        found = active_trades.pop(symbol, None) is not None
    if found:
        send(f"Trade-Überwachung für <b>{coin}</b> gestoppt.")
    else:
        send(f"Kein laufender Trade für {coin}.")

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
            if sym not in SYMBOLS:
                continue
            try:
                entry = float(info["entry"])
                sl    = float(info["sl"]) if info.get("sl") is not None else None
                tp    = float(info["tp"]) if info.get("tp") is not None else None
            except (ValueError, TypeError, KeyError):
                continue
            coin = sym.replace("USDT","")
            start_alarm_thread(coin, sym, entry, sl, tp)
    except Exception: pass

# ── Chart Generation ─────────────────────────────────────────────────────────
def generate_chart(sym, raw_candles, entry, sl, tp):
    """Gjeneron PNG 4h candlestick me EMA20 + nivelet entry/SL/TP."""
    candles = raw_candles[-60:]
    times = [pd.Timestamp(int(k[0]), unit='ms') for k in candles]
    df = pd.DataFrame({
        'Open':   [float(k[1]) for k in candles],
        'High':   [float(k[2]) for k in candles],
        'Low':    [float(k[3]) for k in candles],
        'Close':  [float(k[4]) for k in candles],
        'Volume': [float(k[5]) for k in candles],
    }, index=pd.DatetimeIndex(times))

    # EMA20 për çdo kandelë
    all_closes = [float(k[4]) for k in raw_candles]
    k_m, e = 2 / 21, all_closes[0]
    all_emas = []
    for c in all_closes:
        e = c * k_m + e * (1 - k_m)
        all_emas.append(e)
    ema_series = pd.Series(all_emas[-60:], index=pd.DatetimeIndex(times))

    ap = [mpf.make_addplot(ema_series, color='cyan', width=1.5)]
    hl = dict(
        hlines=[entry, sl, tp],
        colors=['#3399ff', '#ff4444', '#00cc44'],
        linewidths=[1.2, 1.2, 1.2],
        linestyle='--'
    )
    buf = io.BytesIO()
    fig, _ = mpf.plot(
        df, type='candle', style='nightclouds',
        addplot=ap, hlines=hl,
        title=f'\n{sym} – 4h  |  Entry ${entry}  SL ${sl}  TP ${tp}',
        figsize=(12, 7), returnfig=True
    )
    fig.savefig(buf, format='png', dpi=100, bbox_inches='tight', facecolor='#131722')
    plt.close(fig)
    buf.seek(0)
    return buf

def send_photo(buf, caption=""):
    """Dërgon foto në Telegram."""
    url = f"https://api.telegram.org/bot{TOKEN}/sendPhoto"
    try:
        _req.post(
            url,
            data={"chat_id": CHAT_ID, "caption": caption, "parse_mode": "HTML"},
            files={"photo": ("chart.png", buf, "image/png")},
            timeout=30
        )
    except Exception:
        send(caption)  # fallback: dërgo vetëm tekstin

# ── BTC Gatekeeper + Unified Scan ────────────────────────────────────────────
_last_auto_scan = 0.0

def do_scan(triggered_by_command=False):
    """BTC është kusht i parë. Nëse bullish → skanoj të gjithë coinat."""
    if triggered_by_command:
        send("Skanoj coinat... pak durim.")

    # Hapi 1: Kontrollo BTC 4h EMA20
    try:
        url4 = "https://api.binance.com/api/v3/klines?" + urlencode(
            {"symbol": "BTCUSDT", "interval": "4h", "limit": 50})
        with urlopen(url4, timeout=8) as r:
            d4 = json.loads(r.read())
        c4        = [float(k[4]) for k in d4]
        ema4h_btc = get_ema(c4)
        btc_price = round(c4[-1], 2)
        btc_ema   = round(ema4h_btc, 2)
        btc_bull  = c4[-1] > ema4h_btc
    except Exception:
        if triggered_by_command:
            send("Gabim: nuk arrita të marr të dhënat e BTC.")
        return

    # BTC Bearish → mesazh i shkurtër, stop
    if not btc_bull:
        send(
            "Për momentin nuk ka setup-e të mira.\n"
            f"BTC është nën EMA20 — ${btc_price} (EMA: ${btc_ema}) — Bearish.\n\n"
            "Presim një ambient më të sigurt tregtar."
        )
        return

    # Hapi 2: BTC Bullish → skanoj coinat e tjera
    setups = []
    watch  = []

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

                # ── Filtrat e cilësisë (boti "sheh" chartin) ──────────────
                # 1. SL duhet të jetë nën EMA20 — jo mbi të
                if sl >= ema4h:
                    continue
                # 2. Trupi i kandelës bounce duhet të jetë bullish i qartë
                #    (trupi ≥ 30% e rangut të kandelës)
                candle_range = h4[-1] - l4[-1]
                body_ratio   = (c4[-1] - o4[-1]) / candle_range if candle_range > 0 else 0
                if body_ratio < 0.3:
                    continue
                # 3. Volumi i bounce-it të mos jetë shumë i dobët
                vol_avg = sum(v4[:-1]) / len(v4[:-1])
                if v4[-1] < vol_avg * 0.6:
                    continue
                # ─────────────────────────────────────────────────────────

                slpct = round(rpt / entry * 100, 2)
                if slpct <= 1.5:
                    tp    = round_price(entry + rpt * 2)
                    tppct = round(rpt * 2 / entry * 100, 2)
                    chart = generate_chart(sym, d4, entry, sl, tp)
                    setups.append({
                        "coin": coin, "entry": entry, "sl": sl, "tp": tp,
                        "slpct": slpct, "tppct": tppct, "chart": chart
                    })

            elif trend4 and trendd and inZone:
                watch.append(f"{coin} ({dist:+.2f}%)")

        except Exception:
            pass

    now = datetime.now().strftime("%H:%M")
    if setups:
        for s in setups:
            caption = (
                f"<b>{s['coin']} LONG  |  BTC ✅  |  {now}</b>\n"
                f"Entry: ${s['entry']}  |  SL: ${s['sl']} (-{s['slpct']}%)  |  TP: ${s['tp']} (+{s['tppct']}%)\n"
                f"/alarm {s['coin']} {s['entry']} {s['sl']} {s['tp']}"
            )
            send_photo(s["chart"], caption=caption)
        if watch:
            send(f"👀 <i>Afër EMA20: {' | '.join(watch)}</i>")
    else:
        msg = f"<b>EMA20 Scan — {now}  |  BTC ✅</b>\nAktualisht asnjë setup i mirë."
        if watch:
            msg += f"\n👀 Afër EMA20: {' | '.join(watch)}"
        send(msg)

def run_auto_scan_loop():
    global _last_auto_scan
    while True:
        try:
            if time.time() - _last_auto_scan >= 1200:  # çdo 20 minuta
                _last_auto_scan = time.time()
                threading.Thread(target=do_scan, daemon=True).start()
        except Exception: pass
        time.sleep(30)

def main():
    global offset

    # Gespeicherte Alarme beim Start wiederherstellen
    if os.path.exists(ALARMS_FILE):
        try:
            with open(ALARMS_FILE) as f:
                saved = json.load(f)
            restored = []
            for sym, info in saved.items():
                if sym not in SYMBOLS:
                    continue
                try:
                    entry = float(info["entry"])
                    sl    = float(info["sl"]) if info.get("sl") is not None else None
                    tp    = float(info["tp"]) if info.get("tp") is not None else None
                except (ValueError, TypeError, KeyError):
                    continue
                coin = sym.replace("USDT","")
                start_alarm_thread(coin, sym, entry, sl, tp, notify=False)
                restored.append(coin)
            if restored:
                send(f"Bot neugestartet. Alarme wiederhergestellt: <b>{', '.join(restored)}</b>")
        except Exception: pass

    threading.Thread(target=run_auto_scan_loop, daemon=True).start()

    send("Bot gestartet! Schreib /hilfe um alle Befehle zu sehen.")
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
                else: send("Unbekannter Befehl. Schreib /hilfe")

        except KeyboardInterrupt:
            send("Bot gestoppt.")
            print("[Bot] Beendet.", flush=True)
            break
        except Exception: time.sleep(5)

if __name__ == "__main__":
    main()
