#!/usr/bin/env python3
"""
GianniTGTradingBot — Interaktiver Trading Bot
Befehle die du im Telegram schreiben kannst:
  /scan           — RS + Stage2 + VCP Scan aller 25 Coins (Daily + 4H)
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
    # Large Caps
    "ETHUSDT",  "SOLUSDT",  "BNBUSDT",  "XRPUSDT",  "ADAUSDT",
    # Etablierte Mid-Caps
    "AVAXUSDT", "DOTUSDT",  "LINKUSDT", "MATICUSDT","ATOMUSDT",
    # Neue Liquid-Coins
    "NEARUSDT", "APTUSDT",  "ARBUSDT",  "INJUSDT",  "SUIUSDT",
    # DeFi / Layer2
    "OPUSDT",   "LDOUSDT",  "STXUSDT",  "RUNEUSDT", "SEIUSDT",
    # Weitere liquide Paare
    "TIAUSDT",  "LTCUSDT",  "AAVEUSDT", "DOGEUSDT", "FTMUSDT",
]  # 25 liquideste Binance-Paare — RS Leader Strategie

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

# Pending Setups — auf Disk gespeichert damit Railway-Neustarts den Button nicht brechen
_PENDING_FILE = os.path.join(os.path.dirname(__file__), "pending_setups.json")

def _load_pending() -> dict:
    try:
        with open(_PENDING_FILE) as f:
            data = json.load(f)
        # Abgelaufene entfernen
        now = time.time()
        return {k: v for k, v in data.items() if v.get("expires", 0) > now}
    except Exception:
        return {}

def _save_pending(d: dict) -> None:
    try:
        with open(_PENDING_FILE, "w") as f:
            json.dump(d, f)
    except Exception:
        pass

_pending_setups: dict = _load_pending()

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
    """Wenn BTC >1% innerhalb von 15 Minuten fällt und aktive Trades hat → Notfall-Alarm."""
    global _btc_price_last
    while True:
        try:
            price = get_price("BTCUSDT")
            if _btc_price_last is not None and active_trades:
                drop_pct = (_btc_price_last - price) / _btc_price_last * 100
                if drop_pct >= 1.0:
                    trades_list = ", ".join(s.replace("USDT","") for s in active_trades)
                    send(
                        f"🚨 <b>BTC fällt!</b>\n"
                        f"Rückgang: <b>{round(drop_pct,2)}%</b> in 15 Minuten\n"
                        f"Preis: ${round(price,2)}\n\n"
                        f"Aktive Trades: <b>{trades_list}</b>\n"
                        f"Überprüfe deine Positionen sofort!"
                    )
            _btc_price_last = price
        except: pass
        time.sleep(900)  # alle 15 Minuten

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
                            f"🚨 ENTRY ERREICHT: <b>{coin}</b>\n"
                            f"Preis: <b>${price}</b> | Entry: ${entry}\n"
                            f"Stop Loss: ${sl}\n"
                            f"Take Profit: ${tp}\n\n"
                            f"⚡ JETZT BINANCE ÖFFNEN!\n"
                            f"Nach dem Kauf: /trade {coin} {entry} {sl} {tp}"
                        )
                    else:
                        send(f"🔔 ALARM: <b>{coin}</b> hat ${entry} erreicht!\nPreis jetzt: <b>${price}</b>")
                    for _ in range(3):
                        time.sleep(60)
                        if symbol not in active_alerts: break
                        send(f"⏰ Erinnerung: <b>{coin}</b> bei ${entry} — noch aktiv!")
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
    _log_message(text)

# ── Message Log ───────────────────────────────────────────────────────────────
import logging as _logging
from logging.handlers import RotatingFileHandler as _RotHandler

_LOG_PATH = os.path.join(os.path.dirname(__file__), "signals.log")
_msg_logger = _logging.getLogger("signals")
_msg_logger.setLevel(_logging.INFO)
if not _msg_logger.handlers:
    _h = _RotHandler(_LOG_PATH, maxBytes=5*1024*1024, backupCount=5)  # 5 MB, 5 Backups
    _h.setFormatter(_logging.Formatter("%(asctime)s UTC | %(message)s",
                                        datefmt="%Y-%m-%d %H:%M:%S"))
    _logging.Formatter.converter = __import__("time").gmtime  # UTC erzwingen
    _msg_logger.addHandler(_h)

def _log_message(text: str) -> None:
    """Logt jede Bot-Nachricht in signals.log (automatisch über send() aufgerufen)."""
    clean = text.replace("\n", " | ")
    _msg_logger.info(clean)

def get_log(lines: int = 50) -> str:
    """Gibt die letzten N Zeilen aus signals.log zurück."""
    try:
        with open(_LOG_PATH, encoding="utf-8") as f:
            all_lines = f.readlines()
        return "".join(all_lines[-lines:]) or "Log ist leer."
    except FileNotFoundError:
        return "Noch keine Nachrichten geloggt."

def get_updates(offset):
    r = tg("getUpdates", offset=offset, timeout=20,
            allowed_updates='["message","callback_query"]')
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
        coins_line = "📊 25 Coins: ETH · SOL · BNB · XRP · ADA · AVAX · LINK · AAVE · LTC · ..."
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
    scan_line = "🕐 Scans: 09:00 Briefing  |  16:00 Screener  |  16:45 Signal  |  stündlich"

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
        "  stündlich — Stiller Hintergrund-Scan\n\n"
        "📊 <b>25 Coins:</b> ETH · SOL · BNB · XRP · ADA · AVAX · LINK · AAVE · LTC · ...\n\n"
        "/scan — Manueller Scan (RS + Stage2 + 4H VCP)\n"
        "/strategie — Aktive Strategie + alle Filter anzeigen\n"
        "/briefing — Morgenbriefing manuell auslösen\n"
        "/zonen — BTC Liquidation Zonen (Coinglass)\n"
        "/price BNB — Aktueller Preis\n"
        "/status — Bot-Status\n\n"
        "/alarm BNB 674.50 663.20 685 — Preisalarm setzen\n"
        "/alarme — Aktive Alarme anzeigen\n"
        "/stop BNB — Alarm löschen\n\n"
        "/trade BNB 674.50 663.20 685 — Trade manuell überwachen\n"
        "/trades — Aktive Trades anzeigen\n"
        "/stoptrade BNB — Trade-Überwachung stoppen\n\n"
        "/log — Letzte 50 Bot-Nachrichten abrufen\n"
        "/log 100 — Letzte 100 Nachrichten\n\n"
        "/testsetup BNB — Test-Setup mit Buttons senden\n\n"
        "/hilfe — Diese Liste\n\n"
        "<i>Bei einem Signal erscheinen zwei Buttons:\n"
        "  [JA, TRADEN 🚀] → Order wird platziert\n"
        "  [NEIN, ABLEHNEN ❌] → Setup verworfen</i>"
    )

def cmd_strategie():
    """Zeigt die aktive Strategie mit allen Filtern und Parametern."""
    from strategy import DEFAULT_CONFIG
    cfg       = DEFAULT_CONFIG
    coins_str = " · ".join(s.replace("USDT", "") for s in SYMBOLS)
    send(
        "📋 <b>AKTIVE STRATEGIE</b>\n"
        "<i>RS Leader + Weinstein Stage2 + 4H VCP Pullback</i>\n"
        "Long Only | Spot | Halal\n\n"
        f"📊 <b>{len(SYMBOLS)} Coins</b> werden gescannt\n"
        "🕐 Scan: stündlich + 09:00 / 16:00 / 16:45 CEST\n\n"
        "── <b>Filter-Kaskade</b> ──────────────────\n"
        "F1  <b>Daily Golden Cross</b>\n"
        "    EMA50 &gt; EMA200 + EMA200 steigt\n\n"
        f"F2  <b>Nicht zu extended</b>\n"
        f"    Preis max. {cfg['extension_max']}% über Daily EMA50\n\n"
        "F3  <b>Relative Stärke (RS)</b>\n"
        "    Coin/BTC Ratio-EMA steigt auf Daily\n\n"
        f"F4  <b>RS Resilienz</b>\n"
        f"    Wenn BTC {cfg['btc_drop_min']}%, Coin verliert &lt; {abs(cfg['coin_max_drop'])}%\n\n"
        f"F5  <b>4H EMA20 Pullback</b>\n"
        f"    Preis max. {cfg['prox_pct']}% von 4H EMA20 entfernt\n\n"
        "F6  <b>VCP Kompression</b>\n"
        "    Kerzen + Volumen trocknen auf 4H aus\n\n"
        "── <b>SL / TP</b> ─────────────────────────\n"
        f"SL  Swing-Low (letzte {cfg['swing_len']} × 4H-Kerzen) − {cfg['atr_mult']}×ATR\n"
        f"TP  Entry + {int(cfg['crv'])}× Risiko  (CRV {int(cfg['crv'])}:1)\n"
        "💰 Risiko  1% des Kapitals pro Trade\n\n"
        "── <b>Coins</b> ───────────────────────────\n"
        f"<code>{coins_str}</code>"
    )

def cmd_price(parts):
    if len(parts) < 2:
        send("Verwendung: /price BNB"); return
    coin   = parts[1].upper().replace("USDT","")
    symbol = coin + "USDT"
    try:
        price = get_price(symbol)
        send(f"<b>{coin}/USDT</b>: ${price}")
    except:
        send(f"Coin {coin} nicht gefunden. Bitte Namen prüfen.")

def cmd_scan():
    """Manueller /scan — RS Leader + Stage2 + 4H VCP Pullback."""
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
        send(f"Alarm für {coin} ist bereits aktiv. /stop {coin} zum Löschen."); return

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
    msg += "/stop COIN — Alarm löschen"
    send(msg)

def cmd_stop(parts):
    if len(parts) < 2:
        send("Verwendung: /stop BNB"); return
    coin   = parts[1].upper().replace("USDT","")
    symbol = coin + "USDT"
    if symbol in active_alerts:
        active_alerts.pop(symbol)
        save_alarms()
        send(f"🔕 Alarm für <b>{coin}</b> gelöscht.")
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
        send(f"Überwachung für {coin} ist bereits aktiv."); return

    def monitor_trade():
        rr = round((tp - entry) / (entry - sl), 1)
        send(
            f"✅ Trade aktiv: <b>{coin} LONG</b>\n"
            f"Entry: ${entry}\n"
            f"SL: ${sl} | TP: ${tp}\n"
            f"RR: {rr}:1\n"
            f"Du wirst benachrichtigt wenn SL oder TP erreicht wird."
        )
        last_update = time.time()
        while symbol in active_trades:
            try:
                price = get_price(symbol)
                now   = time.time()

                # Update alle 4 Stunden
                if now - last_update >= 14400:
                    pct = round((price - entry) / entry * 100, 2)
                    send(f"📊 Update <b>{coin}</b>: ${price} ({pct:+.2f}% vom Entry)")
                    last_update = now

                if price <= sl:
                    send(
                        f"🔴 STOP LOSS ERREICHT: <b>{coin}</b>\n"
                        f"SL: ${sl} | Preis: ${price}\n\n"
                        f"Trade geschlossen. Nicht stressen, nächstes Setup kommt. 💪"
                    )
                    active_trades.pop(symbol, None)
                    break

                if price >= tp:
                    send(
                        f"🟢 TAKE PROFIT ERREICHT: <b>{coin}</b>\n"
                        f"TP: ${tp} | Preis: ${price}\n\n"
                        f"Perfekt! Trade schließen. 🎯"
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
        send("Keine aktiven Trades."); return
    msg = "<b>Aktive Trades:</b>\n\n"
    for sym, info in active_trades.items():
        try:
            price = get_price(sym)
            pct   = round((price - info["entry"]) / info["entry"] * 100, 2)
            msg  += f"<b>{sym.replace('USDT','')}</b>: ${price} ({pct:+.2f}%)\nEntry ${info['entry']} | SL ${info['sl']} | TP ${info['tp']}\n\n"
        except:
            msg += f"<b>{sym.replace('USDT','')}</b>: Entry ${info['entry']} | SL ${info['sl']} | TP ${info['tp']}\n\n"
    send(msg)

def cmd_briefing():
    """Sendet das Morgen-Briefing manuell (force=True)."""
    threading.Thread(target=morning_briefing, kwargs={"force": True}, daemon=True).start()

def cmd_stoptrade(parts):
    if len(parts) < 2:
        send("Verwendung: /stoptrade BNB"); return
    coin   = parts[1].upper().replace("USDT","")
    symbol = coin + "USDT"
    if symbol in active_trades:
        active_trades.pop(symbol)
        send(f"Überwachung für <b>{coin}</b> gestoppt.")
    else:
        send(f"Kein aktiver Trade für {coin}.")

# ── Chart + Scan me Filtër Cilësie ────────────────────────────────────────────
_sl_alerted = {}

def generate_chart(sym, candles_15m_dicts, entry, sl, tp):
    """Generiert PNG 4H Candlestick-Chart mit EMA20/50/200 + Entry/SL/TP-Linien."""
    window = candles_15m_dicts[-60:]
    # Synthetische Timestamps (4H-Raster, rückwärts vom jetzigen Moment)
    end   = pd.Timestamp.utcnow().floor("4h")
    times = pd.date_range(end=end, periods=len(window), freq="4h")
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
                      title=f"\n{sym} – 4H  |  Entry ${entry}  SL ${sl}  TP ${tp}",
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


_ACCEPTED_LOG = os.path.join(os.path.dirname(__file__), "accepted_trades.txt")

def _edit_message(chat_id, message_id, text):
    """Ersetzt den Text einer bestehenden Nachricht (entfernt auch Buttons)."""
    tg("editMessageText",
       chat_id=chat_id, message_id=message_id,
       text=text, parse_mode="HTML")

def handle_callback_query(cq):
    """Verarbeitet alle Button-Klicks aus Trade-Alerts."""
    cq_id      = cq["id"]
    data       = cq.get("data", "")
    msg        = cq.get("message", {})
    chat_id    = str(msg.get("chat", {}).get("id", CHAT_ID))
    message_id = msg.get("message_id")

    # 1. Immer sofort antworten — verhindert endlos-drehen
    tg("answerCallbackQuery", callback_query_id=cq_id, text="✅")

    # ── accept_SYMBOL ────────────────────────────────────────────────────────
    if data.startswith("accept_"):
        symbol = data[len("accept_"):]
        coin   = symbol.replace("USDT", "")
        _edit_message(chat_id, message_id, f"✅ <b>{coin}</b> angenommen — Trade aktiv.")
        # In accepted_trades.txt loggen
        try:
            os.makedirs(os.path.dirname(_ACCEPTED_LOG) or ".", exist_ok=True)
            with open(_ACCEPTED_LOG, "a") as f:
                f.write(f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC | {symbol} | ACCEPTED\n")
        except Exception as e:
            print(f"[AcceptLog] Fehler: {e}", flush=True)

    # ── reject_SYMBOL ────────────────────────────────────────────────────────
    elif data.startswith("reject_"):
        symbol = data[len("reject_"):]
        coin   = symbol.replace("USDT", "")
        _edit_message(chat_id, message_id, f"❌ <b>{coin}</b> abgelehnt.")

    # ── Legacy: trade_yes_ / trade_no_ (alter Code) ──────────────────────────

    if data.startswith("trade_yes_"):
        symbol = data[len("trade_yes_"):]
        now    = time.time()
        # Immer frisch von Disk laden — überlebt Railway-Neustarts
        current = _load_pending()
        p = current.pop(symbol, None)
        _pending_setups.pop(symbol, None)
        _save_pending(current)
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
        current = _load_pending()
        p = current.pop(symbol, None)
        _pending_setups.pop(symbol, None)
        _save_pending(current)
        coin   = p["coin"] if p else symbol.replace("USDT", "")
        send(f"❌ <b>{coin}</b> abgelehnt — kein Trade.")


def do_scan(triggered_by_command=False, show_loading=True, scan_label=""):
    """
    Haupt-Scan-Funktion — entscheidet BTC 1H EMA20:
      BTC bullish (über EMA20) → Plan A: 7 Krypto-Coins
      BTC bearish (unter EMA20) → Plan B: US-Aktien
    """
    if triggered_by_command and show_loading:
        send("🔍 Scanne Markt... bitte warten.")

    # ── BTC 1H Entscheidung: Plan A oder Plan B ───────────────────────────────
    btc_bullish, btc_emoji, btc_desc = get_btc_status()

    if not btc_bullish:
        # Plan B: US-Aktien — Krypto pausiert
        scan_stocks(triggered_by_command=triggered_by_command, scan_label=scan_label)
        return

    # ── Plan A: Krypto ────────────────────────────────────────────────────────
    equity  = get_equity()
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
            ext_tag = f"+{s.get('extension_pct', 0)}% D-EMA50"
            rs_tag  = "RS ✅" if s.get("rs_ok", True) else "RS ⚠️"

            # ── BTC Zone-Filter: Setup blockieren wenn BTC < 1% von Liq-Zone ──
            near, warn = btc_near_zone(threshold_pct=1.0)
            if near:
                send(
                    f"🚫 <b>{s['coin']} Setup blockiert</b>\n"
                    f"{warn}\n"
                    f"<i>Warte bis BTC die Zone sweept oder dreht.</i>"
                )
                continue

            if AUTO_TRADE:
                # Vollautomatisch (AUTO_TRADE=true in Railway) — sofort ausführen
                caption = (
                    f"🤖 <b>VCP SETUP {s['coin']} 4H  |  {rs_tag}  |  {header}{now}</b>\n"
                    f"Entry: ${s['entry']}  |  SL: ${s['sl']} (-{s['sl_pct']}%)"
                    f"  |  TP: ${s['tp']} (+{s['tp_pct']}%)\n"
                    f"RSI: {s['rsi']}  |  Ext: {ext_tag}  |  Stage2 ✅ VCP ✅\n"
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
                # Phase 1: Button-Bestätigung — JA = Trade, NEIN = Ablehnen
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
                _save_pending(_pending_setups)   # ← auf Disk speichern
                caption = (
                    f"🎯 <b>VCP SETUP {s['coin']} 4H  |  {rs_tag}  |  {header}{now}</b>\n"
                    f"Entry: ${s['entry']}  |  SL: ${s['sl']} (-{s['sl_pct']}%)"
                    f"  |  TP: ${s['tp']} (+{s['tp_pct']}%)\n"
                    f"RSI: {s['rsi']}  |  Ext: {ext_tag}  |  Stage2 ✅ VCP ✅\n"
                    f"👇 <b>Möchtest du diesen Trade ausführen?</b>"
                )
                send_photo_with_buttons(chart, caption=caption, symbol=s["symbol"])

    elif triggered_by_command:
        watch_str = "  |  ".join(f"{w['coin']} ({w['dist_pct']:+.2f}%)" for w in watch)
        msg = f"🔍 <b>VCP Scan — {header}{now}</b>\nKein Setup — Markt erfüllt Stage2 / RS / VCP Kriterien noch nicht."
        if watch_str:
            msg += f"\n👀 <b>Beobachten:</b> {watch_str}"
        send(msg)
    # Kein Setup + Hintergrund-Scan → totale Stille

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


def format_dyn_zones(zones: dict) -> str:
    """Formatiert fetch_dyn_zones() Ergebnis als Telegram-Text."""
    if not zones:
        return "🔲 <b>BTC Zonen:</b> Daten nicht verfügbar\n"
    lines = ["⚡ <b>BTC Liq. Zonen (Leverage-Schätzung):</b>"]
    if "upper" in zones:
        p, _ = zones["upper"]
        lines.append(f"  📈 Widerstand / Short-Liq: <b>${p:,.0f}</b>")
    if "lower" in zones:
        p, _ = zones["lower"]
        lines.append(f"  📉 Support / Long-Liq:     <b>${p:,.0f}</b>")
    lines.append("<i>  (10x/25x/50x Leverage + 24h Range)</i>")
    return "\n".join(lines) + "\n"


def cmd_zones():
    """Zeigt BTC Liquidation Zonen on-demand."""
    zones = fetch_dyn_zones()
    send(format_dyn_zones(zones))


def btc_near_zone(threshold_pct: float = 1.0) -> tuple:
    """
    Prüft ob BTC weniger als threshold_pct% von einer Liq-Zone entfernt ist.
    Gibt (True, warnung_str) oder (False, "") zurück.
    """
    try:
        zones = fetch_dyn_zones()
        if not zones:
            return False, ""
        resp = _req.get("https://api.mexc.com/api/v3/ticker/price",
                        params={"symbol": "BTCUSDT"}, timeout=5)
        btc = float(resp.json().get("price", 0))
        if btc == 0:
            return False, ""
        for side, (zone_price, _) in zones.items():
            dist_pct = abs(btc - zone_price) / btc * 100
            if dist_pct <= threshold_pct:
                label = "Widerstand" if side == "upper" else "Support"
                msg = (f"⚠️ BTC nur {dist_pct:.2f}% von {label}-Zone "
                       f"(${zone_price:,.0f}) — Sweep-Gefahr!")
                print(f"[ZoneFilter] {msg}", flush=True)
                return True, msg
        return False, ""
    except Exception as e:
        print(f"[ZoneFilter] Fehler: {e}", flush=True)
        return False, ""


def cmd_testsetup(parts):
    """Sendet eine gefälschte Setup-Nachricht mit Annehmen/Ablehnen Buttons zum Testen."""
    coin   = parts[1].upper().replace("USDT", "") if len(parts) > 1 else "BNB"
    symbol = coin + "USDT"
    try:
        price = get_price(symbol)
    except Exception:
        price = 100.0
    entry  = round(price, 2)
    sl     = round(price * 0.985, 2)
    tp     = round(price * 1.03,  2)
    sl_pct = round((entry - sl) / entry * 100, 2)
    tp_pct = round((tp - entry) / entry * 100, 2)

    caption = (
        f"🎯 <b>EMA SNIPER — TEST SETUP ⚠️</b>\n"
        f"<b>{coin}/USDT</b> LONG\n"
        f"{'─'*24}\n"
        f"Entry: <b>${entry}</b>\n"
        f"SL:    <b>${sl}</b>  (-{sl_pct}%)\n"
        f"TP:    <b>${tp}</b>  (+{tp_pct}%)\n"
        f"RSI: 48  |  Ext: +3.2% D-EMA50  |  Stage2 ✅ VCP ✅\n"
        f"👇 <b>Möchtest du diesen Trade ausführen?</b>\n"
        f"<i>(Das ist ein Test — kein echter Trade)</i>"
    )
    markup = json.dumps({"inline_keyboard": [[
        {"text": "✅ Annehmen", "callback_data": f"accept_{symbol}"},
        {"text": "❌ Ablehnen", "callback_data": f"reject_{symbol}"},
    ]]})
    tg("sendMessage", chat_id=CHAT_ID, text=caption,
       parse_mode="HTML", reply_markup=markup)


def fetch_dyn_zones() -> dict:
    """
    BTC Liquidation Zonen — kostenlose Schätzung via MEXC.
    Berechnet aus typischen Leverage-Levels (10x/25x/50x) + 24h High/Low.
    Coinglass Heatmap (exakte Cluster) benötigt bezahlten Plan.
    """
    try:
        resp = _req.get(
            "https://api.mexc.com/api/v3/ticker/24hr",
            params={"symbol": "BTCUSDT"}, timeout=6)
        t      = resp.json()
        price  = float(t["lastPrice"])
        high24 = float(t["highPrice"])
        low24  = float(t["lowPrice"])

        # Leverage-basierte Liq-Schwellen:
        # 10x Short → liquidiert bei +9.1%  | 10x Long → bei -9.1%
        # 25x Short → liquidiert bei +3.8%  | 25x Long → bei -3.8%
        # 50x Short → liquidiert bei +2.0%  | 50x Long → bei -2.0%
        upper_candidates = [price * 1.091, price * 1.038, price * 1.020, high24]
        lower_candidates = [price * 0.909, price * 0.962, price * 0.980, low24]

        # Nächste Zone direkt über / unter aktuellem Preis
        upper = min(c for c in upper_candidates if c > price)
        lower = max(c for c in lower_candidates if c < price)

        print(f"[DynZone] Preis=${price:,.0f} | Upper=${upper:,.0f} | Lower=${lower:,.0f} (Leverage-Schätzung)", flush=True)
        return {"upper": (upper, 0), "lower": (lower, 0)}
    except Exception as e:
        print(f"[DynZone] Fehler: {e}", flush=True)
        return {}


def generate_liquidation_chart():
    """BTC liquidation chart — nutzt v4 DynZone Daten + historische Bars."""
    if not COINGLASS_KEY:
        print("[DynZone] Kein API Key — Chart übersprungen.", flush=True)
        return None
    try:
        # Historische Bars — v4 aggregated-history endpoint
        resp = _req.get(
            "https://open-api-v4.coinglass.com/api/futures/liquidation/aggregated-history",
            headers={"CG-API-KEY": COINGLASS_KEY},
            params={"symbol": "BTC", "interval": "4h",
                    "exchange_list": "Binance,OKX,Bybit"},
            timeout=10,
        )
        print(f"[Coinglass LIQ] status={resp.status_code} raw={resp.text[:200]}", flush=True)
        resp.raise_for_status()
        body = resp.json()
        rows = body.get("data", [])
        if isinstance(rows, dict):
            rows = [rows]

        if not rows:
            print("[Coinglass LIQ] Keine Datenpunkte erhalten.", flush=True)
            return None

        times, longs, shorts = [], [], []
        for r in rows[-18:]:  # letzte 18 × 4h = 3 Tage
            ts = int(r.get("time", 0))
            dt = datetime.utcfromtimestamp(ts / 1000 if ts > 1e10 else ts) + CEST
            times.append(dt.strftime("%d/%m\n%H:%M"))
            longs.append(float(r.get("aggregated_long_liquidation_usd",  0)) / 1_000_000)
            shorts.append(float(r.get("aggregated_short_liquidation_usd", 0)) / 1_000_000)

        x = range(len(times))
        fig, ax = plt.subplots(figsize=(12, 5), facecolor='#131722')
        ax.set_facecolor('#131722')
        ax.bar([i - 0.2 for i in x], longs,  width=0.38, color='#ff4444', label='Longs liquidiert')
        ax.bar([i + 0.2 for i in x], shorts, width=0.38, color='#00cc66', label='Shorts liquidiert')
        ax.set_xticks(list(x))
        ax.set_xticklabels(times, color='white', fontsize=7)
        ax.tick_params(colors='white')
        ax.yaxis.set_tick_params(labelcolor='white')
        ax.set_ylabel('Mio USD', color='white')
        day_str = (datetime.utcnow() + CEST).strftime("%Y-%m-%d")
        ax.set_title(f'BTC Liquidations — 3 Tage 4h  ({day_str})', color='white', fontsize=13)
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
    """Holt High-Impact USD News von ForexFactory für heute (CEST)."""
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
            return "📅 <b>News heute (USD High):</b> Keine wichtigen Ereignisse. ✅\n"
        lines = ["⚠️ <b>News heute (USD High):</b>"]
        for ev in high:
            try:
                t = (datetime.fromisoformat(ev["date"]) + CEST).strftime("%H:%M")
            except Exception:
                t = "?"
            title = _html.escape(ev.get("title", "?"))
            lines.append(f"  {t} — {title}")
        return "\n".join(lines) + "\n"
    except Exception:
        return "📅 Kalender offline.\n"


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
    zones_text     = format_dyn_zones(fetch_dyn_zones())

    # Scan-Ergebnis für die gemeinsame Nachricht vorbereiten
    equity = get_equity()
    setups, watch, _ = scan_all_symbols(SYMBOLS, equity=equity)
    if setups:
        scan_text = ""
        for s in setups:
            rs_tag  = "RS ✅" if s.get("rs_ok", True) else "RS ⚠️"
            ext_tag = f"+{s.get('extension_pct', 0)}% D-EMA50"
            scan_text += (
                f"\n🎯 <b>{s['coin']} LONG — 4H VCP</b>\n"
                f"Entry: ${s['entry']}  SL: ${s['sl']} (-{s['sl_pct']}%)  TP: ${s['tp']} (+{s['tp_pct']}%)\n"
                f"RSI: {s['rsi']}  |  {ext_tag}  |  {rs_tag}  Stage2 ✅"
            )
    elif watch:
        watch_str = "  |  ".join(f"{w['coin']} ({w['dist_pct']:+.2f}%)" for w in watch)
        scan_text = f"\n👀 <b>Beobachten:</b> {watch_str}"
    else:
        scan_text = "\n🔍 <b>Scan:</b> Kein Setup — Markt noch kein Stage2 / RS / VCP Signal."

    text_part = (
        f"☀️ <b>MORGEN-BRIEFING — {day}  09:00 CEST</b>\n\n"
        f"{sentiment_text}\n"
        f"{zones_text}\n"
        f"{news_text}"
        f"{scan_text}"
    )

    liq_chart = generate_liquidation_chart()
    if liq_chart:
        send_photo(liq_chart, caption=text_part)
    else:
        send(text_part)


_last_auto_scan = 0.0

def monitor_sl_width():
    """Alle 60 Min automatischen do_scan ausführen (4H Strategie)."""
    global _last_auto_scan
    while True:
        try:
            if time.time() - _last_auto_scan >= 3600:
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
                send(f"🤖 Bot neugestartet. Alarme wiederhergestellt: <b>{names}</b>")
        except: pass

    threading.Thread(target=run_auto_scan_loop, daemon=True).start()
    threading.Thread(target=monitor_sl_width, daemon=True).start()
    threading.Thread(target=monitor_btc_emergency, daemon=True).start()

    send("🤖 Bot gestartet! Schreib /hilfe für alle Befehle.")
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
                elif cmd == "/strategie":    cmd_strategie()
                elif cmd == "/status":       cmd_status()
                elif cmd == "/scan":         threading.Thread(target=do_scan, args=(True,), daemon=True).start()
                elif cmd == "/briefing":     cmd_briefing()
                elif cmd in ("/zonen", "/zones"):  cmd_zones()
                elif cmd == "/testsetup":          cmd_testsetup(parts)
                elif cmd == "/price":        cmd_price(parts)
                elif cmd == "/alarm":        cmd_alarm(parts)
                elif cmd == "/alarme":       cmd_alarme()
                elif cmd == "/stop":         cmd_stop(parts)
                elif cmd == "/trade":
                    if len(parts) >= 5:  cmd_trade(parts)          # /trade BNB entry sl tp
                    else:                cmd_trade_confirm(parts)   # /trade BNB — Bestätigung
                elif cmd == "/trades":       cmd_trades()
                elif cmd == "/stoptrade":    cmd_stoptrade(parts)
                elif cmd == "/log":
                    n = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 50
                    send(f"<pre>{get_log(n)}</pre>")
                else: send("Unbekannter Befehl. Schreib /hilfe")

        except KeyboardInterrupt:
            send("Bot gestoppt.")
            print("[Bot] Beendet.", flush=True)
            break
        except: time.sleep(5)

if __name__ == "__main__":
    main()
