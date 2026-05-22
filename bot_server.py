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
                       execute_trade, round_price as _round_price)

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

SYMBOLS = [
    "ATOMUSDT", "LINKUSDT", "BNBUSDT", "DOTUSDT",
    "SUIUSDT",  "INJUSDT",  "APTUSDT", "ZECUSDT",
]  # Top 8 — Optimizer-Liste + ZEC (starke EMA-Moves)

STOCK_SYMBOLS = ["AAPL", "TSLA", "NVDA", "MSFT", "GOOGL", "PG", "JNJ"]

active_alerts = {}  # { "SOLUSDT": {"entry":..,"sl":..,"tp":..,"thread":..} }
active_trades = {}  # { "SOLUSDT": {"entry":..,"sl":..,"tp":..,"thread":..} }
_lock = threading.Lock()
POSITION_SIZE      = float(os.environ.get("POSITION_SIZE", "0"))
BINANCE_API_KEY    = os.environ.get("BINANCE_API_KEY", "")
BINANCE_API_SECRET = os.environ.get("BINANCE_API_SECRET", "")
# AUTO_TRADE=true  → sofort ausführen ohne Bestätigung
# AUTO_TRADE=false → 2-Minuten-Fenster, /trade COIN bestätigt den Trade
AUTO_TRADE = os.environ.get("AUTO_TRADE", "false").lower() == "true"
CONFIRM_WINDOW_SEC = 300  # Sekunden bis Buttons ablaufen (5 Minuten)

# Pending Setups: {symbol: {"entry", "sl", "tp", "equity", "expires", "coin"}}
_pending_setups: dict = {}

def get_equity():
    """Live USDT-Balance von Binance; fällt auf POSITION_SIZE-Env-Var zurück."""
    if BINANCE_API_KEY and BINANCE_API_SECRET:
        bal = get_binance_usdt_balance(BINANCE_API_KEY, BINANCE_API_SECRET)
        if bal > 0:
            return bal
    return POSITION_SIZE if POSITION_SIZE else 1000.0
CHAT_ID = str(CHAT_ID) if CHAT_ID else CHAT_ID
COINGLASS_KEY        = os.environ.get("COINGLASS_API_KEY", "")
LIQ_WARN_THRESHOLD_M = float(os.environ.get("LIQ_WARN_THRESHOLD_M", "50"))  # Warn-Schwelle $50M
TRADE_START_HOUR     = 10   # Trading-Verbot vor 10:30 CEST
TRADE_START_MIN      = 30
offset = 0

# ── BTC Boss Filter ───────────────────────────────────────────────────────────
def get_btc_status():
    """
    Prüft BTC 1H EMA20 — bestimmt Plan A vs Plan B.
    Rückgabe: (bullish: bool, emoji: str, beschreibung: str)
      True  → Plan A: Krypto-Scan aktiv
      False → Plan B: Aktien-Scan aktiv
    """
    try:
        url = "https://api.binance.com/api/v3/klines?" + urlencode(
            {"symbol": "BTCUSDT", "interval": "1h", "limit": 25})
        with urlopen(url, timeout=8) as r:
            data = json.loads(r.read())
        closes  = [float(k[4]) for k in data]
        ema     = get_ema(closes)
        price   = closes[-1]
        bullish = price > ema
        emoji   = "🟢" if bullish else "🔴"
        trend   = "über" if bullish else "unter"
        desc    = f"BTC ${round(price, 0):,.0f} — {trend} 1H EMA20 (${round(ema, 0):,.0f})"
        return bullish, emoji, desc
    except Exception:
        # API-Fehler → Plan A als sicherer Fallback
        return True, "🟡", "BTC Status nicht verfügbar — Plan A (Krypto) als Fallback"

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
    now_dt   = datetime.utcnow() + CEST
    weekday  = now_dt.weekday()
    hour_min = now_dt.hour * 60 + now_dt.minute
    days_de  = ["Montag","Dienstag","Mittwoch","Donnerstag","Freitag","Samstag","Sonntag"]

    # ── BTC 1H EMA20 — Plan A oder Plan B ────────────────────────────────────
    btc_bullish, btc_emoji, btc_desc = get_btc_status()
    if btc_bullish:
        mode_line = f"🟢 <b>Plan A — Krypto aktiv</b>\n{btc_desc}"
        coins_line = "📊 Coins: ATOM · LINK · BNB · DOT · SUI · INJ · APT · ZEC"
    else:
        mode_line = f"🔴 <b>Plan B — US-Aktien aktiv</b>\n{btc_desc}"
        coins_line = "📊 Aktien: AAPL · TSLA · NVDA · MSFT · GOOGL · PG · JNJ"

    # ── US-Markt Status ───────────────────────────────────────────────────────
    market_open = weekday < 5 and (15*60+30) <= hour_min <= (21*60+30)
    if weekday >= 5:
        market_line = f"📈 US-Markt: <b>GESCHLOSSEN</b> ({days_de[weekday]})"
    elif market_open:
        mins_left = (21*60+30) - hour_min
        market_line = f"📈 US-Markt: <b>OFFEN</b> 🟢  schliesst in {mins_left//60}h {mins_left%60}min"
    elif hour_min < 15*60+30:
        mins_to = (15*60+30) - hour_min
        market_line = f"📈 US-Markt: <b>GESCHLOSSEN</b> 🔴  öffnet in {mins_to//60}h {mins_to%60}min"
    else:
        market_line = f"📈 US-Markt: <b>GESCHLOSSEN</b> 🔴  öffnet morgen 15:30"

    # ── Nächste Scans ─────────────────────────────────────────────────────────
    scan_line = "🕐 Scans: 09:00 Briefing  |  16:00 Screener  |  16:45 Signal  |  alle 20 Min"

    # ── Offene Bestätigungen ──────────────────────────────────────────────────
    now_ts = time.time()
    pending_valid = {s: p for s, p in _pending_setups.items() if p["expires"] > now_ts}
    if pending_valid:
        pending_line = "⏳ <b>Offene Setups:</b> " + "  |  ".join(
            f"{p['coin']} (noch {int(p['expires']-now_ts)}s)" for p in pending_valid.values()
        )
    else:
        pending_line = ""

    # ── Alarme & Trades ───────────────────────────────────────────────────────
    alarm_line = f"🔔 Aktive Alarme: <b>{len(active_alerts)}</b>"
    if active_alerts:
        alarm_line += " — " + ", ".join(s.replace("USDT","") for s in active_alerts)
    trade_line = f"📊 Aktive Trades: <b>{len(active_trades)}</b>"
    if active_trades:
        trade_line += " — " + ", ".join(s.replace("USDT","") for s in active_trades)

    auto_line = f"🤖 AUTO_TRADE: <b>{'AN ⚡' if AUTO_TRADE else 'AUS — Button-Bestätigung'}</b>"

    msg = (
        f"📡 <b>STATUS — {now_dt.strftime('%H:%M')} CEST</b>\n{'─'*30}\n\n"
        f"{mode_line}\n"
        f"{coins_line}\n\n"
        f"{market_line}\n"
        f"{scan_line}\n\n"
        f"{alarm_line}\n"
        f"{trade_line}\n"
        f"{auto_line}"
    )
    if pending_line:
        msg += f"\n{pending_line}"
    send(msg)


def cmd_hilfe():
    send(
        "<b>GianniTGT Trading Bot 🤖</b>\n\n"
        "📡 <b>Automatische Scans (CEST):</b>\n"
        "  09:00 — Morgenbriefing (Markt + Sentiment + Scan)\n"
        "  16:00 — Coin-Screener (Status vor heißer Phase)\n"
        "  16:45 — Post-NY Signal (nach NY-Eröffnungsvolatilität)\n"
        "  alle 20 Min — Stiller Hintergrund-Scan\n\n"
        "📊 <b>Coins:</b> ATOM · LINK · BNB · DOT · SUI · INJ · APT · ZEC\n\n"
        "/scan — Manueller EMA-Sniper-Scan (7/7 Filter)\n"
        "/briefing — Morgenbriefing manuell auslösen\n"
        "/price BNB — Aktueller Preis\n"
        "/status — Bot-Status\n\n"
        "/alarm BNB 674.50 663.20 685 — Preisalarm setzen\n"
        "/alarme — Aktive Alarme anzeigen\n"
        "/stop BNB — Alarm löschen\n\n"
        "/trade BNB 674.50 663.20 685 — Trade manuell überwachen\n"
        "/trades — Aktive Trades anzeigen\n"
        "/stoptrade BNB — Trade-Überwachung stoppen\n\n"
        "/hilfe — Diese Liste\n\n"
        "<i>Bei einem Signal erscheinen zwei Buttons:\n"
        "  [JA, TRADEN 🚀] → Order wird platziert\n"
        "  [NEIN, ABLEHNEN ❌] → Setup verworfen</i>"
    )

def cmd_zonen():
    """Zeigt die aktuell berechneten BTC Liquidations-Zonen aus Coinglass."""
    send("🔍 Berechne BTC Liq-Zonen aus Coinglass...")
    _liq_zones_cache["ts"] = 0  # Cache leeren → frische Daten erzwingen
    zones = fetch_dynamic_liq_zones()
    try:
        btc_p = get_price("BTCUSDT")
    except Exception:
        btc_p = 0

    if not zones["upper_zone"] and not zones["lower_zone"]:
        send(
            "⚠️ <b>Keine Zonen berechnet</b>\n\n"
            "Mögliche Ursachen:\n"
            "• Kein Coinglass API Key gesetzt\n"
            "• Kein Bucket hatte Volumen > $50M in den letzten 3 Tagen\n"
            "• API-Fehler (Logs in Railway prüfen)"
        )
        return

    lines = [f"🎯 <b>BTC Liq-Zonen — Live aus Coinglass</b>\n"
             f"BTC aktuell: ${round(btc_p, 0):,.0f}\n{'─'*28}"]

    if zones["upper_zone"]:
        dist = round((zones["upper_zone"] - btc_p) / btc_p * 100, 2) if btc_p else 0
        lines.append(
            f"\n↑ <b>Short-Liq Zone (obere):</b>\n"
            f"   Preis: ~${zones['upper_zone']:,.0f}  ({dist:+.2f}%)\n"
            f"   Volumen: ${zones['upper_vol_m']:.1f}M\n"
            f"   → Shorts clustern hier — Short-Squeeze Magnet"
        )
    if zones["lower_zone"]:
        dist = round((zones["lower_zone"] - btc_p) / btc_p * 100, 2) if btc_p else 0
        lines.append(
            f"\n↓ <b>Long-Liq Zone (untere):</b>\n"
            f"   Preis: ~${zones['lower_zone']:,.0f}  ({dist:+.2f}%)\n"
            f"   Volumen: ${zones['lower_vol_m']:.1f}M\n"
            f"   → Longs clustern hier — Stop-Hunt Magnet"
        )

    sweep = get_btc_sweep_status(zones)
    status_map = {
        "lower_swept_bullish": "🎯 STOP-HUNT + bullische Struktur → LONGs aktiv!",
        "lower_swept_neutral": "👀 Untere Zone berührt — warte auf Bestätigung",
        "upper_swept_weak":    "⚠️ Short-Squeeze + Schwäche → kein Long",
        "upper_swept_neutral": "🔓 Shorts geräumt — kein Drop-Signal",
        "neutral":             "⏳ Keine Zone aktiv — warte auf Sweep",
    }
    lines.append(f"\n<b>Sweep-Status:</b> {status_map.get(sweep['status'], sweep['status'])}")
    send("\n".join(lines))


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

def cmd_trade_confirm(parts):
    """
    /trade BTC — bestätigt einen pending Setup innerhalb des 2-Minuten-Fensters.
    Führt Market Buy + OCO aus.
    """
    now = time.time()
    # Abgelaufene Setups aufräumen
    expired = [sym for sym, p in _pending_setups.items() if p["expires"] < now]
    for sym in expired:
        _pending_setups.pop(sym, None)

    if len(parts) < 2:
        # Kein Coin angegeben — zeige alle offenen Pending
        if not _pending_setups:
            send("Kein offenes Setup. Warte auf nächsten Scan.")
            return
        lines = []
        for sym, p in _pending_setups.items():
            sek = int(p["expires"] - now)
            lines.append(f"/trade {p['coin']}  (noch {sek}s)")
        send("Offene Setups:\n" + "\n".join(lines))
        return

    coin   = parts[1].upper().replace("USDT", "")
    symbol = coin + "USDT"

    if symbol not in _pending_setups:
        send(f"Kein offenes Setup für {coin}. Entweder abgelaufen oder kein Signal.")
        return

    p = _pending_setups.pop(symbol)
    if p["expires"] < now:
        send(f"⏰ Zeit abgelaufen für <b>{coin}</b>. Setup ist nicht mehr gültig.")
        return

    send(f"⚡ <b>{coin}</b> bestätigt — platziere Order...")
    threading.Thread(
        target=_fire_trade,
        args=(symbol, coin, p["entry"], p["sl"], p["tp"],
              p["sl_pct"], p["tp_pct"], p["equity"]),
        daemon=True
    ).start()


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
    """Schickt Foto per Telegram; Fallback Text wenn Fehler."""
    url = f"https://api.telegram.org/bot{TOKEN}/sendPhoto"
    try:
        _req.post(url,
                  data={"chat_id": CHAT_ID, "caption": caption, "parse_mode": "HTML"},
                  files={"photo": ("chart.png", buf, "image/png")}, timeout=30)
    except Exception:
        send(caption)

def send_photo_with_buttons(buf, caption, symbol):
    """Schickt Chart-Foto mit JA/NEIN Inline-Buttons zur Trade-Bestätigung."""
    url    = f"https://api.telegram.org/bot{TOKEN}/sendPhoto"
    markup = json.dumps({"inline_keyboard": [[
        {"text": "JA, TRADEN 🚀",    "callback_data": f"trade_yes_{symbol}"},
        {"text": "NEIN, ABLEHNEN ❌", "callback_data": f"trade_no_{symbol}"},
    ]]})
    try:
        _req.post(url,
                  data={"chat_id": CHAT_ID, "caption": caption,
                        "parse_mode": "HTML", "reply_markup": markup},
                  files={"photo": ("chart.png", buf, "image/png")},
                  timeout=30)
    except Exception:
        send(caption)

def scan_stocks(triggered_by_command=False, scan_label=""):
    """
    Plan B: Daily EMA20 Pullback auf US-Tech-Aktien.
    Wird aufgerufen wenn BTC unter der 1H EMA20 ist.
    Prüft automatisch ob US-Markt geöffnet ist (Mo-Fr, 15:30–21:30 CEST).
    """
    try:
        import yfinance as yf
    except ImportError:
        if triggered_by_command:
            send("⚠️ yfinance nicht installiert — Plan B nicht verfügbar.")
        return

    # US-Markt nur Mo-Fr, 15:30–21:30 CEST
    now_cest_dt = datetime.utcnow() + CEST
    weekday     = now_cest_dt.weekday()
    hour_min    = now_cest_dt.hour * 60 + now_cest_dt.minute
    market_open = weekday < 5 and (15 * 60 + 30) <= hour_min <= (21 * 60 + 30)

    if not market_open:
        if triggered_by_command:
            opens_in = ""
            if weekday < 5 and hour_min < 15 * 60 + 30:
                mins = (15 * 60 + 30) - hour_min
                opens_in = f" — öffnet in {mins // 60}h {mins % 60}min"
            send(
                f"📈 <b>Plan B — US-Aktien</b>\n"
                f"BTC unter 1H EMA20 → Aktien-Modus aktiv.\n"
                f"⏰ US-Markt aktuell geschlossen{opens_in}.\n"
                f"Scan läuft automatisch ab 15:30 CEST."
            )
        return

    now    = now_cest()
    header = f"{scan_label}  |  " if scan_label else ""
    setups, watch = [], []

    for ticker in STOCK_SYMBOLS:
        try:
            hist = yf.Ticker(ticker).history(period="60d", interval="1d", auto_adjust=True)
            if hist.empty or len(hist) < 22:
                continue

            closes  = [float(x) for x in hist["Close"].tolist()]
            opens   = [float(x) for x in hist["Open"].tolist()]
            highs   = [float(x) for x in hist["High"].tolist()]
            lows    = [float(x) for x in hist["Low"].tolist()]
            vols    = [float(x) for x in hist["Volume"].tolist()]

            ema      = get_ema(closes)
            ema_prev = get_ema(closes[:-3])
            trend    = ema > ema_prev
            cur, opn = closes[-1], opens[-1]
            lo, hi   = lows[-1], highs[-1]
            zone     = ema * 0.005
            in_zone  = lo <= ema + zone and hi >= ema - zone
            bounce   = in_zone and cur > ema and cur > opn
            dist     = round((cur - ema) / ema * 100, 2)
            vol_avg  = sum(vols[:-1]) / len(vols[:-1])
            vol_ok   = vols[-1] >= vol_avg * 0.6

            if trend and bounce and vol_ok:
                entry  = round(cur, 2)
                sl     = round(min(lows[-2] * 0.999, ema * 0.997), 2)
                risk   = entry - sl
                sl_pct = round(risk / entry * 100, 2)
                if sl_pct <= 2.0 and sl < ema:
                    tp     = round(entry + risk * 2, 2)
                    tp_pct = round(risk * 2 / entry * 100, 2)
                    setups.append(
                        f"<b>{ticker}</b> LONG (Daily EMA20 Pullback)\n"
                        f"Entry: ${entry}  |  SL: ${sl} (-{sl_pct}%)  |  TP: ${tp} (+{tp_pct}%)"
                    )
            elif trend and in_zone:
                watch.append(f"{ticker} ({dist:+.2f}%)")
        except Exception:
            continue

    if setups:
        msg = (
            f"📈 <b>PLAN B — US-AKTIEN  |  {header}{now}</b>\n"
            f"<i>BTC unter 1H EMA20 — Krypto pausiert</i>\n{'─'*30}\n\n"
            f"✅ <b>Setup gefunden:</b>\n\n" + "\n\n".join(setups)
        )
        if watch:
            msg += "\n\n👀 <b>Beobachten:</b> " + "  |  ".join(watch)
        send(msg)
    elif triggered_by_command:
        watch_str = "  |  ".join(watch) if watch else "—"
        send(
            f"📈 <b>Plan B — US-Aktien  |  {header}{now}</b>\n"
            f"<i>BTC unter 1H EMA20 — Krypto pausiert</i>\n"
            f"Kein Setup — kein Pullback auf Daily EMA20.\n"
            f"👀 Beobachten: {watch_str}"
        )


def _fire_trade(symbol: str, coin: str, entry: float, sl: float, tp: float,
                sl_pct: float, tp_pct: float, equity: float):
    """Führt Market Buy + OCO aus und schickt Bestätigung per Telegram."""
    if not (BINANCE_API_KEY and BINANCE_API_SECRET):
        send(f"⚠️ <b>{coin}</b>: Keine Binance API Keys — Trade nicht ausgeführt.")
        return
    result = execute_trade(symbol, entry, sl, tp, equity, BINANCE_API_KEY, BINANCE_API_SECRET)
    if result["ok"]:
        send(
            f"✅ <b>TRADE AUSGEFÜHRT: {coin} LONG</b>\n"
            f"Entry: ${entry}  |  Menge: {result['qty']} {coin}\n"
            f"SL: ${sl} (-{sl_pct}%)  |  TP: ${tp} (+{tp_pct}%)\n"
            f"Risiko: ${round(equity * 0.01, 2)}"
        )
    else:
        send(f"❌ <b>{coin} Order fehlgeschlagen:</b> {result['error']}")


def handle_callback_query(cq):
    """Verarbeitet Button-Klicks (JA 🚀 / NEIN ❌) aus Trade-Alerts."""
    cq_id  = cq["id"]
    data   = cq.get("data", "")
    # Telegram erwartet immer eine Antwort auf callback_query
    tg("answerCallbackQuery", callback_query_id=cq_id, text="✅")

    if data.startswith("trade_yes_"):
        symbol = data[len("trade_yes_"):]
        now    = time.time()
        p      = _pending_setups.pop(symbol, None)
        if p is None:
            send("⏰ Setup nicht mehr verfügbar — abgelaufen oder bereits ausgeführt.")
            return
        if p["expires"] < now:
            send(f"⏰ <b>{p['coin']}</b>: Fenster abgelaufen. Setup nicht mehr gültig.")
            return
        send(f"⚡ <b>{p['coin']}</b> bestätigt — platziere Order...")
        threading.Thread(
            target=_fire_trade,
            args=(symbol, p["coin"], p["entry"], p["sl"], p["tp"],
                  p["sl_pct"], p["tp_pct"], p["equity"]),
            daemon=True
        ).start()

    elif data.startswith("trade_no_"):
        symbol = data[len("trade_no_"):]
        p      = _pending_setups.pop(symbol, None)
        coin   = p["coin"] if p else symbol.replace("USDT", "")
        send(f"❌ <b>{coin}</b> abgelehnt — kein Trade.")


def do_scan(triggered_by_command=False, show_loading=True, scan_label=""):
    """
    Haupt-Scan mit institutioneller Liquiditäts-Logik:
      1. Zeit-Filter  — kein Signal vor 10:30 CEST (Vorschau-Modus)
      2. BTC 1H EMA20 — Plan A (Krypto) oder Plan B (Aktien)
      3. Sweep-Check  — Untere Zone gesweept → PAUSE
                        Obere Zone gesweept + Struktur hält → LONGs scharf
    """
    now_cest_dt = datetime.utcnow() + CEST
    hour_min    = now_cest_dt.hour * 60 + now_cest_dt.minute
    too_early   = hour_min < TRADE_START_HOUR * 60 + TRADE_START_MIN

    if triggered_by_command and show_loading:
        if too_early:
            mins_left = (TRADE_START_HOUR * 60 + TRADE_START_MIN) - hour_min
            send(
                f"⏳ <b>Trading-Sperre bis 10:30 CEST</b> — noch {mins_left} Min.\n"
                f"🔍 Vorschau-Scan läuft..."
            )
        else:
            send("🔍 Scanne Markt... bitte warten.")

    # ── BTC 1H Entscheidung: Plan A oder Plan B ───────────────────────────────
    btc_bullish, btc_emoji, btc_desc = get_btc_status()

    if not btc_bullish:
        # Plan B: US-Aktien — ebenfalls erst ab 10:30 scharf
        if too_early:
            if triggered_by_command:
                send(
                    f"⏳ <b>Plan B (US-Aktien) — Trading-Sperre bis 10:30</b>\n"
                    f"{btc_desc}\nScan startet ab 10:30 CEST."
                )
            return
        scan_stocks(triggered_by_command=triggered_by_command, scan_label=scan_label)
        return

    # ── Plan A: BTC bullish — dynamische Liq-Zonen + Sweep-Filter ───────────
    liq_zones = fetch_dynamic_liq_zones()
    sweep     = get_btc_sweep_status(liq_zones)
    btc_p     = sweep["btc_price"]
    if btc_p == 0:
        try: btc_p = get_price("BTCUSDT")
        except Exception: pass

    upper_zone = liq_zones.get("upper_zone")
    lower_zone = liq_zones.get("lower_zone")

    # ── Obere Zone gesweept + Schwäche → VORSICHT, kein Long ─────────────────
    if sweep["status"] == "upper_swept_weak":
        if triggered_by_command:
            zone_ref = f"~${upper_zone:,.0f}" if upper_zone else "Short-Liq Zone"
            send(
                f"⚠️ <b>BTC OBERE ZONE GESWEEPT + SCHWÄCHE</b>\n"
                f"BTC ${round(btc_p, 0):,.0f} hat {zone_ref} (Short-Squeeze) berührt,\n"
                f"danach bearische Struktur auf 15m\n\n"
                f"⛔ <b>Kein LONG-Setup</b> — Drop-Risiko erhöht. Warte auf Stabilisierung."
            )
        return

    # ── Vor 10:30 CEST: Vorschau-Modus (kein Alert, keine Buttons) ───────────
    if too_early:
        if triggered_by_command:
            equity  = get_equity()
            setups, watch, _ = scan_all_symbols(SYMBOLS, equity=equity)
            now_str = now_cest()
            hdr     = f"{scan_label}  |  " if scan_label else ""
            zone_lines = []
            if upper_zone:
                d = f"{sweep['dist_upper_pct']:+.2f}%" if sweep["dist_upper_pct"] is not None else "?"
                zone_lines.append(f"  ↑ Short-Liq: ~${upper_zone:,.0f} ({d})")
            if lower_zone:
                d = f"{sweep['dist_lower_pct']:+.2f}%" if sweep["dist_lower_pct"] is not None else "?"
                zone_lines.append(f"  ↓ Long-Liq:  ~${lower_zone:,.0f} ({d})")
            zone_note = ("🎯 BTC Liq-Zonen (live):\n" + "\n".join(zone_lines)) if zone_lines else ""
            if setups:
                coins_ready = ", ".join(s["coin"] for s in setups)
                send(
                    f"⏳ <b>VORSCHAU — {hdr}{now_str}</b>\n"
                    f"<i>Signale erst ab 10:30 CEST</i>\n\n"
                    f"🎯 Setup-Kandidaten: <b>{coins_ready}</b>\n\n"
                    f"{zone_note}"
                )
            else:
                watch_str = "  |  ".join(
                    f"{w['coin']} ({w['dist_pct']:+.2f}%)" for w in watch
                ) or "—"
                send(
                    f"⏳ <b>VORSCHAU — {hdr}{now_str}</b>\n"
                    f"<i>Signale erst ab 10:30 CEST</i>\n\n"
                    f"Kein Setup — kein Coin erfüllt alle 7 Filter.\n"
                    f"👀 Beobachten: {watch_str}\n\n"
                    f"{zone_note}"
                )
        return

    # ── Lower Zone gesweept, aber noch keine bullische Bestätigung ────────────
    if sweep["status"] == "lower_swept_neutral":
        if triggered_by_command:
            zone_ref = f"~${lower_zone:,.0f}" if lower_zone else "Long-Liq Zone"
            send(
                f"👀 <b>STOP-HUNT ERKANNT — warte auf Bestätigung</b>\n"
                f"BTC ${round(btc_p, 0):,.0f} hat {zone_ref} (Long-Cluster) berührt\n\n"
                f"Noch keine bullische 15m-Struktur — nächste Candle abwarten.\n"
                f"⚡ Sobald 3/4 Candles bullisch: LONGs werden scharf gestellt."
            )
        return

    # ── Keine Zone aktiv — warten ─────────────────────────────────────────────
    if sweep["status"] == "neutral":
        if triggered_by_command:
            lines = []
            if upper_zone:
                d = f"{sweep['dist_upper_pct']:+.2f}%" if sweep["dist_upper_pct"] is not None else "?"
                lines.append(f"  ↑ Short-Liq: ~${upper_zone:,.0f} ({d})")
            if lower_zone:
                d = f"{sweep['dist_lower_pct']:+.2f}%" if sweep["dist_lower_pct"] is not None else "?"
                lines.append(f"  ↓ Long-Liq:  ~${lower_zone:,.0f} ({d})")
            zone_info = "\n".join(lines) if lines else "  Keine Zonen erkannt — Coinglass Key prüfen."
            send(
                f"⏳ <b>WARTE AUF LIQUIDATIONS-SWEEP</b>\n"
                f"BTC ${round(btc_p, 0):,.0f} — noch keine Zone aktiv\n\n"
                f"🎯 Live Zonen:\n{zone_info}\n\n"
                f"Strategie: LONGs nach Stop-Hunt (untere Zone) + bullischer 15m-Struktur."
            )
        return

    # ── LONG-Setups FREIGEGEBEN ───────────────────────────────────────────────
    # Fälle: lower_swept_bullish (PRIME LONG) oder upper_swept_neutral (Shorts geräumt, kein Drop)
    if sweep["status"] == "lower_swept_bullish":
        zone_ref  = f"~${lower_zone:,.0f}" if lower_zone else "Long-Liq Zone"
        sweep_tag = f"🎯 STOP-HUNT ✅ {zone_ref}"
    else:  # upper_swept_neutral
        sweep_tag = "🔓 SHORTS GERÄUMT ✅"

    equity    = get_equity()
    setups, watch, _ = scan_all_symbols(SYMBOLS, equity=equity)

    now    = now_cest()
    header = f"{scan_label}  |  " if scan_label else ""

    if setups:
        for s in setups:
            alert_key = f"{s['symbol']}_{s['entry']}"
            if _sl_alerted.get(s["symbol"]) == alert_key:
                continue
            _sl_alerted[s["symbol"]] = alert_key

            chart   = generate_chart(s["symbol"], s["candles_15m"], s["entry"], s["sl"], s["tp"])
            htf_tag = "1H ✅" if s["htf_bull"] else "1H ⚠️"

            if AUTO_TRADE:
                caption = (
                    f"🤖 <b>EMA SNIPER {s['coin']} 15m  |  {htf_tag}  |  {sweep_tag}  |  {header}{now}</b>\n"
                    f"Entry: ${s['entry']}  |  SL: ${s['sl']} (-{s['sl_pct']}%)"
                    f"  |  TP: ${s['tp']} (+{s['tp_pct']}%)\n"
                    f"RSI: {s['rsi']}  |  ADX: {s['adx']}  |  Filters: 7/7 ✅\n"
                    f"<i>AUTO_TRADE aktiv — Order wird platziert...</i>"
                )
                send_photo(chart, caption=caption)
                threading.Thread(
                    target=_fire_trade,
                    args=(s["symbol"], s["coin"], s["entry"], s["sl"], s["tp"],
                          s["sl_pct"], s["tp_pct"], equity),
                    daemon=True
                ).start()
            else:
                _pending_setups[s["symbol"]] = {
                    "coin":    s["coin"],
                    "entry":   s["entry"],
                    "sl":      s["sl"],
                    "tp":      s["tp"],
                    "sl_pct":  s["sl_pct"],
                    "tp_pct":  s["tp_pct"],
                    "equity":  equity,
                    "expires": time.time() + CONFIRM_WINDOW_SEC,
                }
                caption = (
                    f"🎯 <b>EMA SNIPER {s['coin']} 15m  |  {htf_tag}  |  {sweep_tag}  |  {header}{now}</b>\n"
                    f"Entry: ${s['entry']}  |  SL: ${s['sl']} (-{s['sl_pct']}%)"
                    f"  |  TP: ${s['tp']} (+{s['tp_pct']}%)\n"
                    f"RSI: {s['rsi']}  |  ADX: {s['adx']}  |  Filters: 7/7 ✅\n"
                    f"👇 <b>Möchtest du diesen Trade ausführen?</b>"
                )
                send_photo_with_buttons(chart, caption=caption, symbol=s["symbol"])

    elif triggered_by_command:
        watch_str = "  |  ".join(f"{w['coin']} ({w['dist_pct']:+.2f}%)" for w in watch)
        msg = (
            f"🎯 <b>EMA Sniper  |  {sweep_tag}  |  {header}{now}</b>\n"
            f"Kein Setup — alle 7 Filter von keinem Coin erfüllt."
        )
        if watch_str:
            msg += f"\n👀 <b>Beobachten:</b> {watch_str}"
        send(msg)

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


_liq_zones_cache: dict = {"data": None, "ts": 0.0}

def fetch_dynamic_liq_zones():
    """
    Findet die stärksten BTC Liquidations-Zonen LIVE aus Coinglass — vollautomatisch.

    Algorithmus (kein statischer Preis):
      1. Coinglass: 4h Liquidationsvolumen der letzten 3 Tage (18 Buckets)
      2. Binance:   passende 4h OHLCV-Candles für Preis-Zuordnung
      3. Pro Bucket: Long-dominiert → Candle-LOW = Long-Liq-Zone (Cluster unten)
                     Short-dominiert → Candle-HIGH = Short-Liq-Zone (Cluster oben)
      4. Beste Zone je Typ nach höchstem Volumen → RAM-Variable (kein Env-Var!)
      5. 15-Min-Cache verhindert redundante API-Calls im 20-Min-Hintergrund-Scan

    Returns dict:
      upper_zone / lower_zone : float | None  — dynamischer Preislevel
      upper_vol_m / lower_vol_m: float        — Volumen in Mio. USD
      warning_text             : str          — fertige Warn-Nachricht fürs Briefing
    """
    global _liq_zones_cache
    if time.time() - _liq_zones_cache["ts"] < 900 and _liq_zones_cache["data"]:
        return _liq_zones_cache["data"]

    empty = {"upper_zone": None, "lower_zone": None,
             "upper_vol_m": 0.0, "lower_vol_m": 0.0, "warning_text": ""}

    if not COINGLASS_KEY:
        return empty

    try:
        # 1. Coinglass: 4h-Buckets (letzte 3 Tage)
        resp = _req.get(
            "https://open-api.coinglass.com/public/v2/liquidation/chart",
            headers={"coinglassSecret": COINGLASS_KEY},
            params={"symbol": "BTC", "time_type": "h4", "limit": "18"},
            timeout=10,
        )
        data = resp.json()
        ok   = (data.get("code") == "0") or (data.get("success") is True)
        rows = data.get("data") or []
        if not ok or not rows:
            return empty

        # 2. Binance: 4h OHLCV für Preis-Zuordnung
        url = "https://api.binance.com/api/v3/klines?" + urlencode(
            {"symbol": "BTCUSDT", "interval": "4h", "limit": 18})
        with urlopen(url, timeout=8) as r:
            candles = json.loads(r.read())
        candle_map = {int(k[0]): {"high": float(k[2]), "low": float(k[3])} for k in candles}

        # 3. Enrichment: Liq-Volumen + Preis zusammenführen
        upper_candidates, lower_candidates = [], []
        for row in rows:
            ts        = int(row.get("time", row.get("t", 0)))
            ts_ms     = ts * 1000 if ts < 1e12 else ts
            long_vol  = float(row.get("longLiquidationUsd",  row.get("long",  0))) / 1e6
            short_vol = float(row.get("shortLiquidationUsd", row.get("short", 0))) / 1e6
            total_vol = long_vol + short_vol
            if total_vol == 0:
                continue
            # Nächste 4h-Candle suchen (Toleranz ±4h)
            best = min(candle_map.keys(), key=lambda t: abs(t - ts_ms), default=None)
            if not best or abs(best - ts_ms) > 4 * 3600 * 1000:
                continue
            c = candle_map[best]
            if long_vol >= short_vol:
                lower_candidates.append({"price": c["low"],  "long_vol": long_vol,
                                          "short_vol": short_vol, "total_vol": total_vol})
            else:
                upper_candidates.append({"price": c["high"], "long_vol": long_vol,
                                          "short_vol": short_vol, "total_vol": total_vol})

        # 4. Stärksten Kandidaten je Zone wählen
        upper = max(upper_candidates, key=lambda x: x["total_vol"]) if upper_candidates else None
        lower = max(lower_candidates, key=lambda x: x["total_vol"]) if lower_candidates else None

        # 5. Warn-Text + Zonenübersicht
        warnings = []
        if upper:
            warnings.append(
                f"⚠️ <b>SHORT-LIQ ZONE:</b> ~${upper['price']:,.0f} — "
                f"${upper['short_vol']:.1f}M Shorts geclustert (Short-Squeeze Risiko!)"
            )
        if lower:
            warnings.append(
                f"⚠️ <b>LONG-LIQ ZONE:</b> ~${lower['price']:,.0f} — "
                f"${lower['long_vol']:.1f}M Longs geclustert (Stop-Hunt Magnet!)"
            )
        try:
            btc_now    = get_price("BTCUSDT")
            zone_lines = []
            if upper:
                d = round((upper["price"] - btc_now) / btc_now * 100, 2)
                zone_lines.append(f"  ↑ Short-Liq: ~${upper['price']:,.0f} ({d:+.2f}%) — ${upper['short_vol']:.1f}M")
            if lower:
                d = round((lower["price"] - btc_now) / btc_now * 100, 2)
                zone_lines.append(f"  ↓ Long-Liq:  ~${lower['price']:,.0f} ({d:+.2f}%) — ${lower['long_vol']:.1f}M")
            if zone_lines:
                warnings.append("🎯 <b>Dynamische BTC Liq-Zonen:</b>\n" + "\n".join(zone_lines))
        except Exception:
            pass

        result = {
            "upper_zone":  upper["price"]     if upper else None,
            "lower_zone":  lower["price"]     if lower else None,
            "upper_vol_m": upper["total_vol"] if upper else 0.0,
            "lower_vol_m": lower["total_vol"] if lower else 0.0,
            "warning_text": ("\n".join(warnings) + "\n") if warnings else "",
        }
        _liq_zones_cache = {"data": result, "ts": time.time()}
        return result

    except Exception as e:
        print(f"[DynZone] error: {e}", flush=True)
        return empty


def get_btc_sweep_status(liq_zones):
    """
    Prüft BTC 15m-Candles (letzte 8h) auf Sweep der dynamischen Liq-Zonen.

    KORREKTE Marktpsychologie:
      lower_swept_bullish → Stop-Hunt auf Long-Cluster, dann Erholung
                            = PRIME LONG SETUP (Liquidität abgeholt + Käufer zurück!)
      lower_swept_neutral → Sweep, aber noch keine bullische Bestätigung
                            = Warten auf Kerzen-Bestätigung
      upper_swept_weak    → Short-Squeeze, danach Schwäche / Rejection
                            = VORSICHT — potenzieller Drop, kein Long
      upper_swept_neutral → Shorts geräumt, kein klares Drop-Signal
                            = Normal weiter scannen
      neutral             → keine Zone aktiv, warten
    """
    upper_zone = liq_zones.get("upper_zone")
    lower_zone = liq_zones.get("lower_zone")

    try:
        url = "https://api.binance.com/api/v3/klines?" + urlencode(
            {"symbol": "BTCUSDT", "interval": "15m", "limit": 32})
        with urlopen(url, timeout=8) as r:
            candles = json.loads(r.read())
        highs  = [float(k[2]) for k in candles]
        lows   = [float(k[3]) for k in candles]
        opens  = [float(k[1]) for k in candles]
        closes = [float(k[4]) for k in candles]
        last_close = closes[-1]

        # Struktur-Check auf den letzten 4 Candles (≈ 1 Stunde)
        rc = closes[-4:]; ro = opens[-4:]
        bullish_count = sum(1 for c, o in zip(rc, ro) if c > o)
        bearish_count = sum(1 for c, o in zip(rc, ro) if c < o)
        is_bullish = bullish_count >= 3 and last_close > closes[-5]
        is_weak    = bearish_count >= 3 and last_close < closes[-5]

        lower_touched = lower_zone and any(lo <= lower_zone for lo in lows)
        upper_touched = upper_zone and any(h  >= upper_zone for h  in highs)

        dist_upper = round((upper_zone - last_close) / last_close * 100, 2) if upper_zone else None
        dist_lower = round((last_close - lower_zone) / last_close * 100, 2) if lower_zone else None

        if lower_touched and is_bullish:
            status = "lower_swept_bullish"
        elif lower_touched:
            status = "lower_swept_neutral"
        elif upper_touched and is_weak:
            status = "upper_swept_weak"
        elif upper_touched:
            status = "upper_swept_neutral"
        else:
            status = "neutral"

        return {
            "status":         status,
            "btc_price":      last_close,
            "dist_upper_pct": dist_upper,
            "dist_lower_pct": dist_lower,
        }
    except Exception as e:
        print(f"[Sweep] error: {e}", flush=True)
        return {"status": "neutral", "btc_price": 0.0,
                "dist_upper_pct": None, "dist_lower_pct": None}


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
    liq_zones      = fetch_dynamic_liq_zones()  # dynamische Zonen live aus Coinglass

    text_part = (
        f"☀️ <b>MORGENBRIEFING — {day}  09:00 CEST</b>\n\n"
        f"{sentiment_text}\n"
        f"{liq_zones['warning_text']}"
        f"{news_text}"
    )

    liq_chart = generate_liquidation_chart()
    if liq_chart:
        send_photo(liq_chart, caption=text_part)
    else:
        send(text_part)

    # Vorschau-Scan (09:00 → vor 10:30, kein echtes Signal — nur Kandidaten + Zonen)
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
    (9,  0,  "morgen"),    # 09:00 — Morgenbriefing
    (16, 0,  "screener"),  # 16:00 — Coin-Screener (vor heißer Phase)
    (16, 45, "signal"),    # 16:45 — Post-NY-Signal (nach NY-Eröffnungsvolatilität)
]

def maybe_run_scheduled_scans():
    """Prüft jede Minute ob ein geplanter Scan fällig ist (09:00 / 16:00 / 16:45 CEST)."""
    global _scans_done
    now_dt      = datetime.utcnow()
    cest_hour   = (now_dt.hour + 2) % 24
    cest_minute = now_dt.minute
    day_key     = now_dt.strftime("%Y-%m-%d")

    for h, m, scan_type in SCAN_SCHEDULE:
        key = f"{day_key}_{h}_{m}"
        # 3-Minuten-Fenster — falls Loop den exakten Tick verpasst
        in_window = (cest_hour == h) and (m <= cest_minute < m + 3)
        if in_window and key not in _scans_done:
            _scans_done.add(key)
            if scan_type == "morgen":
                threading.Thread(target=morning_briefing, daemon=True).start()
            elif scan_type == "screener":
                threading.Thread(
                    target=do_scan,
                    args=(True, True, "📊 16:00 COIN-SCREENER"),
                    daemon=True
                ).start()
            elif scan_type == "signal":
                threading.Thread(
                    target=do_scan,
                    args=(True, True, "🎯 16:45 POST-NY SIGNAL"),
                    daemon=True
                ).start()

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

                # ── Button-Klick (JA / NEIN) ──────────────────────────────────
                if "callback_query" in u:
                    cq  = u["callback_query"]
                    cid = str(cq.get("message", {}).get("chat", {}).get("id", ""))
                    if cid == CHAT_ID:
                        handle_callback_query(cq)
                    continue

                if cid != CHAT_ID: continue
                if not text.startswith("/"): continue

                parts = text.split()
                cmd   = parts[0].lower()
                print(f"[Bot] Befehl: {text}", flush=True)

                if cmd == "/hilfe":          cmd_hilfe()
                elif cmd == "/status":       cmd_status()
                elif cmd == "/zonen":        threading.Thread(target=cmd_zonen, daemon=True).start()
                elif cmd == "/scan":         threading.Thread(target=do_scan, args=(True,), daemon=True).start()
                elif cmd == "/briefing":     cmd_briefing()
                elif cmd == "/price":        cmd_price(parts)
                elif cmd == "/alarm":        cmd_alarm(parts)
                elif cmd == "/alarme":       cmd_alarme()
                elif cmd == "/stop":         cmd_stop(parts)
                elif cmd == "/trade":
                    if len(parts) >= 5:  cmd_trade(parts)          # /trade BNB entry sl tp
                    else:                cmd_trade_confirm(parts)   # /trade BNB — Bestätigung
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
