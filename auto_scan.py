"""
Auto-Scan: läuft täglich via Windows Task Scheduler
Sendet EMA20 Scan-Ergebnis direkt per Telegram
"""
import json, time, hmac, hashlib, requests
from datetime import datetime

with open("C:/Users/Gianni/TradingBot/telegram_config.json") as f:
    cfg = json.load(f)
TOKEN   = cfg["bot_token"]
CHAT_ID = cfg["chat_id"]

SYMBOLS = [
    "BTCUSDT","ETHUSDT","SOLUSDT","BNBUSDT","LINKUSDT",
    "NEARUSDT","AVAXUSDT","MAGICUSDT","DOTUSDT","ADAUSDT",
    "XRPUSDT","SUIUSDT","INJUSDT","APTUSDT","ARBUSDT"
]

def send(text):
    requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        data={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"},
        timeout=10
    )

def get_ema(closes, period=20):
    k, e = 2/(period+1), closes[0]
    for c in closes[1:]: e = c*k + e*(1-k)
    return e

def round_price(v):
    if v > 100: return round(v, 2)
    if v > 1:   return round(v, 4)
    return round(v, 5)

# ── Makro-Kalender ────────────────────────────────────────────────────────────
skip_today = False
cal_msg = ""
try:
    cal   = requests.get("https://nfs.faireconomy.media/ff_calendar_thisweek.json", timeout=6).json()
    today = datetime.now().strftime("%Y-%m-%d")
    high  = [e for e in cal if e.get("impact") == "High" and e.get("country") == "USD" and e.get("date","").startswith(today)]
    if high:
        skip_today = True
        cal_msg = "NEWS-TAG — kein Trade empfohlen!\n"
        for ev in high:
            try: t = datetime.fromisoformat(ev["date"]).strftime("%H:%M")
            except: t = "?"
            cal_msg += f"  {t} {ev['title']}\n"
    else:
        cal_msg = "Keine High-Impact News heute. Grünes Licht.\n"
except:
    cal_msg = "Kalender offline.\n"

# ── Funding Rate BTC ──────────────────────────────────────────────────────────
funding_warn = False
fund_msg = ""
try:
    fr   = requests.get("https://fapi.binance.com/fapi/v1/premiumIndex?symbol=BTCUSDT", timeout=6).json()
    fp   = round(float(fr["lastFundingRate"]) * 100, 4)
    if fp > 0.05:
        funding_warn = True
        fund_msg = f"BTC Funding: +{fp}% — HOCH! Risiko halbiert.\n"
    elif fp < -0.01:
        fund_msg = f"BTC Funding: {fp}% — Negativ, gut für Longs.\n"
    else:
        fund_msg = f"BTC Funding: {fp}% — Neutral.\n"
except:
    fund_msg = "Funding offline.\n"

# ── EMA20 Scan ────────────────────────────────────────────────────────────────
setups = []
watch  = []
no     = []

if not skip_today:
    risk_usd = 100
    if funding_warn:
        risk_usd = 50

    for sym in SYMBOLS:
        coin = sym.replace("USDT","")
        try:
            r4 = requests.get(f"https://api.binance.com/api/v3/klines?symbol={sym}&interval=4h&limit=50", timeout=8).json()
            rd = requests.get(f"https://api.binance.com/api/v3/klines?symbol={sym}&interval=1d&limit=25", timeout=8).json()

            c4 = [float(k[4]) for k in r4]
            o4 = [float(k[1]) for k in r4]
            h4 = [float(k[2]) for k in r4]
            l4 = [float(k[3]) for k in r4]
            v4 = [float(k[5]) for k in r4]
            cd = [float(k[4]) for k in rd]

            ema4h      = get_ema(c4)
            ema4h_prev = get_ema(c4[:-3])
            emad       = get_ema(cd)
            vol_avg    = sum(v4) / len(v4)

            cur = c4[-1]; opn = o4[-1]; hi = h4[-1]; lo = l4[-1]; vol = v4[-1]
            pl  = l4[-2]; ph  = h4[-2]; pc = c4[-2]; po = o4[-2]

            trend4  = ema4h > ema4h_prev
            trendd  = cd[-1] > emad
            zone    = ema4h * 0.005
            inZone  = lo <= ema4h+zone and hi >= ema4h-zone
            pInZone = pl <= ema4h+zone and ph >= ema4h-zone
            bounce  = inZone  and cur > ema4h and cur > opn
            pBounce = pInZone and pc  > ema4h and pc  > po and cur > ema4h
            vol_ok  = vol >= vol_avg * 0.7
            dist    = round((cur - ema4h) / ema4h * 100, 2)

            if trend4 and trendd and (bounce or pBounce):
                entry   = round_price(cur)
                sl      = round_price(min(pl*0.999, ema4h*0.997))
                risk_pt = entry - sl
                sl_pct  = round(risk_pt / entry * 100, 2)
                if sl_pct > 1.5:
                    no.append(f"{coin} (SL {sl_pct}% > 1.5%)")
                    continue
                tp      = round_price(entry + risk_pt * 2)
                tp_pct  = round(risk_pt * 2 / entry * 100, 2)
                pos_val = round(risk_usd / risk_pt * entry, 2)
                vol_str = "OK" if vol_ok else "schwach"
                setups.append({
                    "coin": coin, "entry": entry, "sl": sl, "tp": tp,
                    "sl_pct": sl_pct, "tp_pct": tp_pct,
                    "pos_val": pos_val, "risk_usd": risk_usd, "vol": vol_str
                })
            elif trend4 and trendd and inZone:
                watch.append(f"{coin} ({dist:+.2f}%)")
            else:
                r = "Daily bear" if not trendd else "4h bear" if not trend4 else "kein PB"
                no.append(f"{coin} ({r})")
        except:
            no.append(f"{coin} (Fehler)")

# ── Telegram Nachricht zusammenbauen ──────────────────────────────────────────
now = datetime.now().strftime("%Y-%m-%d %H:%M")
msg = f"<b>MORGEN-SCAN {now}</b>\n"
msg += "─" * 28 + "\n\n"

msg += f"<b>Makro:</b> {cal_msg}"
msg += f"<b>Funding:</b> {fund_msg}\n"

if skip_today:
    msg += "Kein Trade heute — NEWS-TAG!"
    send(msg)
    exit()

if setups:
    msg += f"<b>SETUPS ({len(setups)})</b>\n"
    msg += "─" * 28 + "\n"
    for s in setups:
        msg += (
            f"\n<b>{s['coin']} — EMA20 Pullback Long</b>\n"
            f"Entry:  ${s['entry']}\n"
            f"SL:     ${s['sl']}  (-{s['sl_pct']}%)\n"
            f"TP:     ${s['tp']}  (+{s['tp_pct']}%)  [2:1]\n"
            f"Invest: ${s['pos_val']}  |  Risiko: ${s['risk_usd']}  |  Vol: {s['vol']}\n"
            f"/alarm {s['coin']} {s['entry']} {s['sl']} {s['tp']}\n"
        )
else:
    msg += "<b>Keine Setups</b> — Markt abwarten.\n"

if watch:
    msg += f"\n<b>Beobachten:</b> {' | '.join(watch)}\n"

msg += f"\n<b>Kein Setup:</b> {' | '.join(no)}"

send(msg)
print(f"Scan gesendet: {now} | Setups: {len(setups)} | Watch: {len(watch)}")
