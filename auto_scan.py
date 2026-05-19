"""
Auto-Scan: läuft täglich via Windows Task Scheduler
EMA Sniper — 7-Filter Confluence (15m + 1H HTF)
Sendet Ergebnis direkt per Telegram.
"""
import json
import requests
from datetime import datetime
import sys, os

sys.path.insert(0, os.path.dirname(__file__))
from strategy import scan_all_symbols, get_binance_usdt_balance

with open("C:/Users/Gianni/TradingBot/telegram_config.json") as f:
    cfg = json.load(f)
TOKEN   = cfg["bot_token"]
CHAT_ID = cfg["chat_id"]

# Binance API Keys für Live-Equity
try:
    with open("C:/Users/Gianni/TradingBot/binance_config.json") as f:
        bcfg = json.load(f)
    BINANCE_KEY    = bcfg.get("api_key", "")
    BINANCE_SECRET = bcfg.get("api_secret", "")
except Exception:
    BINANCE_KEY = BINANCE_SECRET = ""

SYMBOLS = [
    "ATOMUSDT", "LINKUSDT", "BNBUSDT", "DOTUSDT",
    "SUIUSDT",  "INJUSDT",  "APTUSDT",
]  # Top 7 — Optimizer: profitabelste Coins (PF > 1.0 über 6 Monate)

# Live USDT-Balance von Binance holen (Fallback: 1000 USD)
EQUITY = get_binance_usdt_balance(BINANCE_KEY, BINANCE_SECRET, fallback=1000.0)

def send(text):
    requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        data={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"},
        timeout=10,
    )

# ── Makro-Kalender ─────────────────────────────────────────────────────────────
skip_today = False
cal_msg    = ""
try:
    cal   = requests.get("https://nfs.faireconomy.media/ff_calendar_thisweek.json", timeout=6).json()
    today = datetime.now().strftime("%Y-%m-%d")
    high  = [e for e in cal
             if e.get("impact") == "High"
             and e.get("country") == "USD"
             and e.get("date", "").startswith(today)]
    if high:
        skip_today = True
        cal_msg    = "NEWS-TAG — kein Trade empfohlen!\n"
        for ev in high:
            try:
                t = datetime.fromisoformat(ev["date"]).strftime("%H:%M")
            except Exception:
                t = "?"
            cal_msg += f"  {t} {ev['title']}\n"
    else:
        cal_msg = "Keine High-Impact News heute. Grünes Licht.\n"
except Exception:
    cal_msg = "Kalender offline.\n"

# ── Funding Rate BTC ───────────────────────────────────────────────────────────
funding_factor = 1.0
fund_msg       = ""
try:
    fr = requests.get(
        "https://fapi.binance.com/fapi/v1/premiumIndex?symbol=BTCUSDT", timeout=6
    ).json()
    fp = round(float(fr["lastFundingRate"]) * 100, 4)
    if fp > 0.05:
        funding_factor = 0.5
        fund_msg = f"BTC Funding: +{fp}% — HOCH! Risiko halbiert.\n"
    elif fp < -0.01:
        fund_msg = f"BTC Funding: {fp}% — Negativ, gut für Longs.\n"
    else:
        fund_msg = f"BTC Funding: {fp}% — Neutral.\n"
except Exception:
    fund_msg = "Funding offline.\n"

# ── Scan ───────────────────────────────────────────────────────────────────────
now = datetime.now().strftime("%Y-%m-%d %H:%M")
msg = f"<b>EMA SNIPER SCAN — {now}</b>\n{'─' * 28}\n\n"
msg += f"<b>Makro:</b> {cal_msg}"
msg += f"<b>Funding:</b> {fund_msg}\n"

if skip_today:
    msg += "Kein Trade heute — NEWS-TAG!"
    send(msg)
    raise SystemExit(0)

effective_equity = EQUITY * funding_factor
setups, watch, errors = scan_all_symbols(SYMBOLS, equity=effective_equity)

# ── Nachricht zusammenbauen ────────────────────────────────────────────────────
if setups:
    msg += f"<b>SETUPS ({len(setups)})</b>\n{'─' * 28}\n"
    for s in setups:
        risk_usd = round(effective_equity * 0.01, 2)
        msg += (
            f"\n<b>{s['coin']} — EMA Sniper Long</b>\n"
            f"Entry:  ${s['entry']}\n"
            f"SL:     ${s['sl']}  (-{s['sl_pct']}%)\n"
            f"TP:     ${s['tp']}  (+{s['tp_pct']}%)  [2:1 CRV]\n"
            f"Größe:  {s['pos_size']} Coins  ≈ ${s['pos_val']}"
            f"  |  Risiko: ${risk_usd}\n"
            f"RSI: {s['rsi']}  |  ADX: {s['adx']}"
            f"  |  1H Confluence: {'✅' if s['htf_bull'] else '⚠️'}\n"
            f"/alarm {s['coin']} {s['entry']} {s['sl']} {s['tp']}\n"
        )
else:
    msg += "<b>Keine Setups</b> — alle 7 Filter von keinem Coin erfüllt.\n"

if watch:
    watch_str = " | ".join(
        f"{w['coin']} ({w['dist_pct']:+.2f}%, RSI {w['rsi']})" for w in watch
    )
    msg += f"\n<b>Beobachten:</b> {watch_str}\n"

send(msg)
print(f"Scan gesendet: {now} | Setups: {len(setups)} | Watch: {len(watch)}")
