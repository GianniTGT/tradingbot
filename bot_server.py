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
    try:
        price = get_price(symbol)
        send(f"<b>{coin}/USDT</b>: ${price}")
    except:
        send(f"Coin {coin} nicht gefunden.")

def cmd_scan():
    send("Scanne 15 Coins... bitte warten.")
    results = {"setup": [], "watch": [], "no": []}

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
                results["watch"].append(f"{coin} ({dist:+.2f}% zu EMA20)")
            else:
                r = "Daily bear" if not trendd else "4h bear" if not trend4 else "kein PB"
                results["no"].append(f"{coin} ({r})")
        except:
            results["no"].append(f"{coin} (Fehler)")

    msg = f"<b>EMA20 Scan 4h — {datetime.now().strftime('%H:%M')}</b>\n\n"
    if results["setup"]:
        msg += "SETUPS:\n" + "\n\n".join(results["setup"]) + "\n\n"
    if results["watch"]:
        msg += "BEOBACHTEN:\n" + " | ".join(results["watch"]) + "\n\n"
    msg += "KEIN SETUP:\n" + " | ".join(results["no"])
    send(msg)

def cmd_alarm(parts):
    # Format A: /alarm BNB 674.50            (einfacher Preisalarm)
    # Format B: /alarm BNB 674.50 663.20 685 (mit SL + TP)
    if len(parts) < 3:
        send("Verwendung:\n/alarm BNB 674.50\n/alarm BNB 674.50 663.20 685.00"); return
    coin   = parts[1].upper().replace("USDT","")
    symbol = coin + "USDT"
    try:
        entry = float(parts[2])
        sl    = float(parts[3]) if len(parts) > 3 else None
        tp    = float(parts[4]) if len(parts) > 4 else None
    except:
        send("Ungültige Zahlen."); return

    if symbol in active_alerts:
        send(f"Alarm für {coin} läuft bereits. /stop {coin} zum Beenden."); return

    start_alarm_thread(coin, symbol, entry, sl, tp)

def cmd_alarme():
    if not active_alerts:
        send("Keine aktiven Alarme."); return
    msg = "<b>Aktive Alarme:</b>\n\n"
    for sym, info in active_alerts.items():
        coin = sym.replace("USDT","")
        try:
            cur = get_price(sym)
            diff = round((cur - info["entry"]) / info["entry"] * 100, 2)
            dist = f"${cur} ({diff:+.2f}%)"
        except:
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
    if symbol in active_alerts:
        active_alerts.pop(symbol)
        save_alarms()
        send(f"Alarm für <b>{coin}</b> gestoppt.")
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

                # Preis-Update alle 4 Stunden
                if now - last_update >= 14400:
                    pct = round((price - entry) / entry * 100, 2)
                    send(f"Update <b>{coin}</b>: ${price} ({pct:+.2f}% seit Entry)")
                    last_update = now

                if price <= sl:
                    loss = round((entry - price) * 8.9, 2)
                    send(
                        f"STOP LOSS GETROFFEN: <b>{coin}</b>\n"
                        f"SL: ${sl} | Preis: ${price}\n"
                        f"Verlust: ~${loss}\n\n"
                        f"Trade ist beendet. Kein Stress, naechstes Setup kommt."
                    )
                    active_trades.pop(symbol, None)
                    break

                if price >= tp:
                    gain = round((price - entry) * 8.9, 2)
                    send(
                        f"TAKE PROFIT ERREICHT: <b>{coin}</b>\n"
                        f"TP: ${tp} | Preis: ${price}\n"
                        f"Gewinn: ~${gain}\n\n"
                        f"Maschallah! Trade schliessen."
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
        send("Keine laufenden Trades."); return
    msg = "<b>Laufende Trades:</b>\n\n"
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
        send("Verwendung: /stoptrade SOL"); return
    coin   = parts[1].upper().replace("USDT","")
    symbol = coin + "USDT"
    if symbol in active_trades:
        active_trades.pop(symbol)
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
    """Scan mit unterschiedlichem Prefix je nach Tageszeit."""
    send("Scanne 15 Coins... bitte warten.")

    if scan_type == "fruehwarnung":
        prefix = "FRÜHWARNUNG 16:00 — noch nicht einsteigen!\nBeobachte diese Coins für 17:30:"
        hint   = "Nächster Check: 17:30 für finale Signale."
    elif scan_type == "signal":
        prefix = "SIGNAL 17:30 — Setup bestätigt:"
        hint   = "Alarm setzen wenn Setup passt: /alarm COIN entry sl tp"
    else:
        prefix = "MORGEN-SCAN 09:00:"
        hint   = "Nächste Scans: 16:00 (Frühwarnung) & 17:30 (Signal)"

    results  = {"setup": [], "watch": [], "no": []}
    vol_rank = []  # für "Bester Kandidat" beim 17:30 Scan

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

    msg = f"<b>{prefix}</b>\n{'─'*28}\n\n"
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
        flag    = "✅ Setup aktiv" if typ_b == "setup" else "👀 In der Zone"
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
                send(f"Bot neugestartet. Alarme wiederhergestellt: <b>{names}</b>")
        except: pass

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
                elif cmd == "/scan":         threading.Thread(target=cmd_scan_typed, args=("morgen",), daemon=True).start()
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
        except: time.sleep(5)

if __name__ == "__main__":
    main()
