"""
strategy.py
Neue Strategie — RS Leader + Weinstein Stage 2 (Daily) + 4H VCP Pullback
Long Only | Spot | Halal

Inspiriert von: Kristjan Qullamaggie, Mark Minervini, Pradeep Bonde

Filter-Kaskade (Balanced Version):
  F1 — Daily Golden Cross:    EMA50 > EMA200 + EMA200 steigt
  F2 — Nicht zu extended:     Large Caps ≤12%, Altcoins ≤15% über Daily EMA50
  F3 — 30-Tage-Hoch Breakout: Tagesschluss > Hoch der letzten 30 Tage
  F4 — Relative Stärke:       Coin/BTC Ratio-EMA steigt auf Daily
  F5 — RS Resilienz (gestaffelt):
                              BTC -1.5% → Coin verliert ≤ 0.5%
                              BTC -2.0% → Coin verliert ≤ 1.0%
                              BTC -3.0% → Coin verliert ≤ 1.5%
  F6 — 4H EMA20 Pullback:     Preis max. 3% von 4H EMA20 entfernt
  F7 — VCP Kompression:       2 von 3 müssen erfüllt sein
                              (Range, Volumen, ATR sinken)
  F8 — Bullische Bestätigung: Letzte 4H-Kerze grün ODER Bullish Engulfing
"""

from urllib.request import urlopen, Request
from urllib.parse import urlencode
import json, time, hmac, hashlib
import requests

# ── Large Caps (strengere Extension-Toleranz) ─────────────────────────────────
LARGE_CAPS = {"ETHUSDT", "BNBUSDT", "XRPUSDT", "LTCUSDT"}

# ── Config ────────────────────────────────────────────────────────────────────
DEFAULT_CONFIG = {
    # Indikatoren
    "ema_lens":       [20, 50, 100, 200],
    "atr_len":        14,
    "rs_period":      20,       # EMA-Periode für Coin/BTC Ratio

    # Entry-Filter (Balanced Version)
    "prox_pct":       3.0,      # Max % Abstand von 4H EMA20 (war 2.0)
    "extension_large": 12.0,    # Max % über Daily EMA50 für Large Caps
    "extension_alt":   15.0,    # Max % über Daily EMA50 für Altcoins
    "extension_max":   15.0,    # Fallback (für Rückwärtskompatibilität)

    # SL / TP / Trailing
    "atr_mult":       0.5,      # SL = Swing-Low − atr_mult × ATR
    "swing_len":      10,       # 4H-Kerzen rückwärts für Swing-Low
    "crv":            2.0,      # TP1 = Entry + 2×Risiko (50% Partial Exit)
    "trailing_ema":   20,       # Trailing Stop folgt 4H EMA{N}

    # RS Resilienz — gestaffelte Toleranz (BTC-Drop %, Coin max. Drop %)
    "resilience_tiers": [
        (-1.5, -0.5),
        (-2.0, -1.0),
        (-3.0, -1.5),
    ],

    # VCP — mind. 2 von 3 müssen erfüllt sein (Range, Volumen, ATR)
    "vcp_lookback":   5,
    "vcp_min_hits":   2,
    "vol_avg_len":    20,

    # Breakout & Confirmation
    "breakout_days":  30,       # Tagesschluss muss N-Tage-Hoch durchbrechen
    "confirm_candle": True,     # Letzte 4H-Kerze grün ODER bullish engulfing

    "session":        None,     # None = 24/7 (Krypto)
}

# ── Preis-Formatierung ────────────────────────────────────────────────────────
def round_price(v):
    if v > 100: return round(v, 2)
    if v > 1:   return round(v, 4)
    return round(v, 5)

# ── Indikatoren ───────────────────────────────────────────────────────────────
def calc_ema(series, n):
    """Standard EMA, alpha = 2/(n+1)."""
    if len(series) < 2:
        return series[-1] if series else 0.0
    k, e = 2 / (n + 1), series[0]
    for v in series[1:]:
        e = v * k + e * (1 - k)
    return e

def _rma_series(series, n):
    """Wilder's Smoothing (RMA) als Serie — wie Pine Script ta.rma()."""
    if not series:
        return []
    seed_n = min(n, len(series))
    e = sum(series[:seed_n]) / seed_n
    result = [e] * seed_n
    for v in series[seed_n:]:
        e = (e * (n - 1) + v) / n
        result.append(e)
    return result

def calc_atr(highs, lows, closes, n=14):
    """ATR via Wilder's RMA."""
    tr = []
    for i in range(1, len(closes)):
        tr.append(max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i]  - closes[i - 1]),
        ))
    if not tr:
        return 0.0
    return _rma_series(tr, n)[-1]

def calc_rsi(closes, n=14):
    """RSI via Wilder's RMA."""
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    if not gains:
        return 50.0
    avg_g = _rma_series(gains,  n)[-1]
    avg_l = _rma_series(losses, n)[-1]
    if avg_l == 0:
        return 100.0
    return 100 - 100 / (1 + avg_g / avg_l)

def calc_adx(highs, lows, closes, n=14):
    """ADX via Wilder's RMA."""
    dm_plus, dm_minus, tr_list = [], [], []
    for i in range(1, len(closes)):
        up   = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        dm_plus.append(up   if (up > down and up > 0)   else 0.0)
        dm_minus.append(down if (down > up and down > 0) else 0.0)
        tr_list.append(max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i]  - closes[i - 1]),
        ))
    if len(tr_list) < n:
        return 0.0, 0.0, 0.0
    s_dm_plus  = _rma_series(dm_plus,  n)
    s_dm_minus = _rma_series(dm_minus, n)
    s_tr       = _rma_series(tr_list,  n)
    dx_list, di_p_list, di_m_list = [], [], []
    for i in range(len(s_tr)):
        atr_i = s_tr[i]
        di_p  = 100 * s_dm_plus[i]  / atr_i if atr_i > 0 else 0.0
        di_m  = 100 * s_dm_minus[i] / atr_i if atr_i > 0 else 0.0
        di_p_list.append(di_p)
        di_m_list.append(di_m)
        denom = di_p + di_m
        dx_list.append(100 * abs(di_p - di_m) / denom if denom > 0 else 0.0)
    adx  = _rma_series(dx_list, n)[-1]
    di_p = di_p_list[-1] if di_p_list else 0.0
    di_m = di_m_list[-1] if di_m_list else 0.0
    return di_p, di_m, adx

# ── Binance Signed Requests ───────────────────────────────────────────────────
def _binance_signed(method, path, params, api_key, api_secret):
    params["timestamp"] = int(time.time() * 1000)
    query = urlencode(sorted(params.items()))
    sig   = hmac.new(api_secret.encode(), query.encode(), hashlib.sha256).hexdigest()
    url   = f"https://api.binance.com{path}"
    headers = {"X-MBX-APIKEY": api_key}
    if method == "GET":
        resp = requests.get(url, params={**params, "signature": sig}, headers=headers, timeout=10)
    else:
        resp = requests.post(url, params={**params, "signature": sig}, headers=headers, timeout=10)
    resp.raise_for_status()
    return resp.json()

# ── Binance Account ───────────────────────────────────────────────────────────
def get_binance_usdt_balance(api_key, api_secret, fallback=0.0):
    if not api_key or not api_secret:
        return fallback
    try:
        data = _binance_signed("GET", "/api/v3/account", {}, api_key, api_secret)
        for b in data.get("balances", []):
            if b["asset"] == "USDT":
                return float(b["free"])
    except Exception:
        pass
    return fallback

# ── Order Execution ───────────────────────────────────────────────────────────
def get_symbol_filters(symbol):
    resp   = requests.get("https://api.binance.com/api/v3/exchangeInfo",
                          params={"symbol": symbol}, timeout=10)
    result = {"step_size": 0.00001, "tick_size": 0.01, "min_notional": 10.0}
    for f in resp.json()["symbols"][0]["filters"]:
        if f["filterType"] == "LOT_SIZE":
            result["step_size"] = float(f["stepSize"])
        elif f["filterType"] == "PRICE_FILTER":
            result["tick_size"] = float(f["tickSize"])
        elif f["filterType"] in ("MIN_NOTIONAL", "NOTIONAL"):
            result["min_notional"] = float(f.get("minNotional", f.get("notional", 10.0)))
    return result

def _floor_step(qty, step):
    import math
    if step <= 0:
        return qty
    decimals = max(0, len(f"{step:.10f}".rstrip("0").split(".")[-1]))
    return round(math.floor(qty / step) * step, decimals)

def _round_tick(price, tick):
    if tick <= 0:
        return price
    decimals = max(0, len(f"{tick:.10f}".rstrip("0").split(".")[-1]))
    return round(round(price / tick) * tick, decimals)

def execute_trade(symbol, entry, sl, tp, equity, api_key, api_secret):
    """
    Market Buy + OCO Sell (SL + TP).
    Positionsgrösse = 1% Risiko / SL-Abstand.
    """
    try:
        filters      = get_symbol_filters(symbol)
        step_size    = filters["step_size"]
        tick_size    = filters["tick_size"]
        min_notional = filters["min_notional"]

        sl_dist = entry - sl
        if sl_dist <= 0:
            return {"ok": False, "error": "SL >= Entry"}

        qty = _floor_step((equity * 0.01) / sl_dist, step_size)
        if qty * entry < min_notional:
            return {"ok": False, "error": f"Zu klein: ${qty * entry:.2f} < min ${min_notional}"}

        buy        = _binance_signed("POST", "/api/v3/order", {
            "symbol": symbol, "side": "BUY", "type": "MARKET", "quantity": qty,
        }, api_key, api_secret)
        filled_qty = _floor_step(float(buy.get("executedQty", qty)), step_size)

        tp_price = _round_tick(tp,       tick_size)
        sl_price = _round_tick(sl,       tick_size)
        sl_limit = _round_tick(sl * 0.999, tick_size)

        oco = _binance_signed("POST", "/api/v3/orderList/oco", {
            "symbol": symbol, "side": "SELL", "quantity": filled_qty,
            "price": tp_price, "stopPrice": sl_price,
            "stopLimitPrice": sl_limit, "stopLimitTimeInForce": "GTC",
        }, api_key, api_secret)

        return {"ok": True, "qty": filled_qty, "buy_order": buy, "oco_order": oco}
    except Exception as e:
        return {"ok": False, "error": str(e)}

# ── Binance Daten ─────────────────────────────────────────────────────────────
def get_candles(symbol, interval, limit=300):
    """
    OHLCV-Kerzen von Binance.
    interval: "1d", "4h", "1h", "15m", etc.
    """
    url    = "https://api.binance.com/api/v3/klines"
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    resp   = requests.get(url, params=params, timeout=10)
    resp.raise_for_status()
    return [
        {
            "open":   float(c[1]),
            "high":   float(c[2]),
            "low":    float(c[3]),
            "close":  float(c[4]),
            "volume": float(c[5]),
        }
        for c in resp.json()
    ]

# Alias für Rückwärtskompatibilität
get_binance_candles = get_candles

# ── Neue Strategie: Haupt-Check ───────────────────────────────────────────────
def check_new_setup(daily_candles, candles_4h, btc_daily, is_large_cap=False, config=None):
    """
    Prüft alle 8 Filter der Balanced-Strategie.

    Args:
        daily_candles:  OHLCV-Liste (Daily, mind. 210 Kerzen)
        candles_4h:     OHLCV-Liste (4H,    mind. 50 Kerzen)
        btc_daily:      OHLCV-Liste (BTC Daily, für RS-Berechnung)
        is_large_cap:   True für ETH/BNB/XRP/LTC → strengere Extension-Grenze
        config:         Optionaler Config-Dict

    Returns:
        {pass, watch, entry, sl, tp, ...} oder None bei unvollständigen Daten
    """
    cfg = {**DEFAULT_CONFIG, **(config or {})}

    if len(daily_candles) < 210 or len(candles_4h) < 50:
        return None

    d_closes = [c["close"]  for c in daily_candles]
    d_highs  = [c["high"]   for c in daily_candles]
    h4_opens  = [c["open"]   for c in candles_4h]
    h4_closes = [c["close"]  for c in candles_4h]
    h4_highs  = [c["high"]   for c in candles_4h]
    h4_lows   = [c["low"]    for c in candles_4h]
    h4_vols   = [c["volume"] for c in candles_4h]

    # ── F1: Daily Golden Cross ─────────────────────────────────────────────
    ema50_d        = calc_ema(d_closes,       50)
    ema200_d       = calc_ema(d_closes,       200)
    ema200_d_20ago = calc_ema(d_closes[:-20], 200)

    f1_cross  = ema50_d > ema200_d            # EMA50 über EMA200
    f1_rising = ema200_d > ema200_d_20ago     # EMA200 steigt

    if not (f1_cross and f1_rising):
        return {"pass": False, "watch": False,
                "reason": "Kein Golden Cross / EMA200 fällt"}

    # ── F2: Nicht zu extended (Large Caps strenger als Altcoins) ──────────
    cur_daily = d_closes[-1]
    ext_pct   = (cur_daily - ema50_d) / ema50_d * 100
    ext_limit = cfg["extension_large"] if is_large_cap else cfg["extension_alt"]
    f2_ok     = ext_pct <= ext_limit

    if not f2_ok:
        cap_label = "Large Cap" if is_large_cap else "Altcoin"
        return {"pass": False, "watch": False,
                "reason": f"Zu extended: +{round(ext_pct,1)}% (Grenze {cap_label}: {ext_limit}%)"}

    # ── F3: 30-Tage-Hoch Breakout (Minervini-Stil) ────────────────────────
    bd = cfg["breakout_days"]
    if len(d_highs) >= bd + 1:
        prior_high = max(d_highs[-(bd + 1):-1])   # Hoch der letzten 30 Tage OHNE heute
        f3_breakout = cur_daily > prior_high
        breakout_diff = round((cur_daily - prior_high) / prior_high * 100, 2)
    else:
        f3_breakout   = True   # zu wenig Daten → Filter überspringen
        breakout_diff = 0.0

    if not f3_breakout:
        return {"pass": False, "watch": True,
                "reason": f"Kein {bd}-Tage-Breakout (noch {breakout_diff}% darunter)",
                "dist_pct": 0, "rsi": 0, "adx": 0}

    # ── F4: Relative Stärke (Coin/BTC Ratio steigt) ───────────────────────
    f3_ok = True
    if len(btc_daily) >= 30:
        try:
            btc_closes = [c["close"] for c in btc_daily]
            min_len    = min(len(d_closes), len(btc_closes), 60)
            ratio      = [
                d_closes[-min_len + i] / btc_closes[-min_len + i]
                for i in range(min_len)
            ]
            r_ema_now  = calc_ema(ratio,       cfg["rs_period"])
            r_ema_prev = calc_ema(ratio[:-10], cfg["rs_period"])
            f3_ok      = r_ema_now > r_ema_prev
        except Exception:
            f3_ok = True   # API-Fehler → Filter überspringen

    # ── F4: RS Resilienz (gestaffelt: BTC-Drop bestimmt akzeptable Coin-Toleranz) ─
    # Tier-Beispiel: BTC -1.5% → Coin max -0.5%, BTC -2% → Coin max -1%, BTC -3% → Coin max -1.5%
    f4_ok = True
    if len(btc_daily) >= 30:
        try:
            btc_closes = [c["close"] for c in btc_daily]
            n          = min(30, len(d_closes), len(btc_closes))
            btc_last   = btc_closes[-n:]
            coin_last  = d_closes[-n:]
            # Tiers ABSTEIGEND nach BTC-Drop (zuerst strengste Stufe prüfen)
            tiers = sorted(cfg["resilience_tiers"], key=lambda t: t[0])
            for i in range(1, n):
                btc_chg = (btc_last[i] - btc_last[i - 1]) / btc_last[i - 1] * 100
                # Welcher Tier greift? Größter BTC-Drop ≤ btc_chg
                matched = None
                for btc_thresh, coin_thresh in tiers:
                    if btc_chg <= btc_thresh:
                        matched = (btc_thresh, coin_thresh)
                if matched is None:
                    continue   # BTC-Bewegung zu klein → kein Tier greift
                coin_chg = (coin_last[i] - coin_last[i - 1]) / coin_last[i - 1] * 100
                if coin_chg < matched[1]:
                    f4_ok = False
                    break
        except Exception:
            f4_ok = True

    # ── F5: 4H EMA20 Pullback (max. 3% Abstand) ───────────────────────────
    ema20_4h = calc_ema(h4_closes, 20)
    cur_4h   = h4_closes[-1]
    dist_4h  = (cur_4h - ema20_4h) / ema20_4h * 100
    f5_ok    = -1.5 <= dist_4h <= cfg["prox_pct"]   # knapp über/an EMA20 (3% Toleranz)

    # ── F6: VCP — 2 von 3 müssen erfüllt sein (Range, Volumen, ATR sinken) ─
    lb = cfg["vcp_lookback"]
    vcp_hits      = 0
    f6_range_ok   = False
    f6_vol_ok     = False
    f6_atr_ok     = False
    if len(h4_vols) >= lb * 2 and len(h4_highs) >= lb * 2:
        # Check 1: Range schrumpft
        ranges_recent = sum(h4_highs[-i] - h4_lows[-i] for i in range(1, lb + 1))           / lb
        ranges_prev   = sum(h4_highs[-i] - h4_lows[-i] for i in range(lb + 1, lb * 2 + 1)) / lb
        f6_range_ok   = ranges_recent < ranges_prev

        # Check 2: Volumen trocknet aus
        vol_recent = sum(h4_vols[-(lb):])     / lb
        vol_prev   = sum(h4_vols[-(lb*2):-lb]) / lb
        f6_vol_ok  = vol_recent < vol_prev * 0.9

        # Check 3: ATR(14) sinkt
        if len(h4_closes) >= cfg["atr_len"] + lb * 2:
            atr_recent = calc_atr(h4_highs[-(cfg["atr_len"] + lb):],
                                  h4_lows[-(cfg["atr_len"] + lb):],
                                  h4_closes[-(cfg["atr_len"] + lb):], cfg["atr_len"])
            atr_prev   = calc_atr(h4_highs[-(cfg["atr_len"] + lb * 2):-lb],
                                  h4_lows[-(cfg["atr_len"] + lb * 2):-lb],
                                  h4_closes[-(cfg["atr_len"] + lb * 2):-lb], cfg["atr_len"])
            f6_atr_ok = atr_recent < atr_prev

        vcp_hits = int(f6_range_ok) + int(f6_vol_ok) + int(f6_atr_ok)
    f6_ok = vcp_hits >= cfg["vcp_min_hits"]   # 2 von 3

    # ── F8: Bullische Bestätigung — grüne Kerze ODER bullish engulfing ────
    if cfg["confirm_candle"]:
        last_open  = h4_opens[-1]
        last_close = h4_closes[-1]
        f8_green   = last_close > last_open
        # Bullish Engulfing: rote Vorkerze + aktuelle grüne Kerze umschließt Vorkerze
        f8_engulf = False
        if len(h4_opens) >= 2 and len(h4_closes) >= 2:
            prev_open, prev_close = h4_opens[-2], h4_closes[-2]
            f8_engulf = (last_close > last_open and          # aktuelle grün
                         prev_close < prev_open and          # vorherige rot
                         last_open  <= prev_close and        # öffnet ≤ vorigem Close
                         last_close >= prev_open)            # schließt ≥ vorigem Open
        f8_ok = f8_green or f8_engulf
    else:
        f8_ok = True

    # ── Watch: Trend + RS ok, aber noch kein Pullback ─────────────────────
    is_watch = f3_ok and not f5_ok

    # ── Alle Filter prüfen ────────────────────────────────────────────────
    if not (f3_ok and f4_ok and f5_ok and f6_ok and f8_ok):
        fail_reason = ""
        if not f3_ok: fail_reason = "RS schwach"
        elif not f4_ok: fail_reason = "Hält nicht bei BTC-Drop"
        elif not f5_ok: fail_reason = f"Zu weit von 4H EMA20 ({round(dist_4h,1)}%)"
        elif not f6_ok: fail_reason = "Keine VCP-Kompression"
        elif not f8_ok: fail_reason = "Letzte 4H-Kerze nicht grün — keine Bestätigung"
        return {
            "pass":     False,
            "watch":    is_watch,
            "dist_pct": round(dist_4h, 2),
            "rsi":      round(calc_rsi(h4_closes, 14), 1),
            "adx":      0,
            "reason":   fail_reason,
        }

    # ── SL / TP berechnen ─────────────────────────────────────────────────
    swing_low = min(h4_lows[-cfg["swing_len"]:])
    atr_4h    = calc_atr(h4_highs, h4_lows, h4_closes, cfg["atr_len"])
    entry     = round_price(cur_4h)
    sl        = round_price(swing_low - atr_4h * cfg["atr_mult"])
    risk_dist = entry - sl

    if risk_dist <= 0 or sl <= 0:
        return {"pass": False, "watch": False, "reason": "SL ungültig"}

    tp     = round_price(entry + risk_dist * cfg["crv"])
    sl_pct = round(risk_dist / entry * 100, 2)
    tp_pct = round(risk_dist * cfg["crv"] / entry * 100, 2)
    rsi    = calc_rsi(h4_closes, 14)

    return {
        "pass":           True,
        "watch":          False,
        "entry":          entry,
        "sl":             sl,
        "tp":             tp,
        "sl_pct":         sl_pct,
        "tp_pct":         tp_pct,
        "atr":            round(atr_4h, 6),
        "ema20":          round_price(ema20_4h),
        "ema50_d":        round_price(ema50_d),
        "ema200_d":       round_price(ema200_d),
        "extension_pct":  round(ext_pct, 2),
        "dist_pct":       round(dist_4h, 2),
        "rsi":            round(rsi, 1),
        "adx":            0,
        "htf_bull":       True,
        "rs_ok":          f3_ok,
        "resilient":      f4_ok,
        "vcp_ok":         f6_ok,
        "vcp_hits":       vcp_hits,
        "breakout_ok":    f3_breakout,
        "breakout_diff":  breakout_diff,
        "confirm_ok":     f8_ok,
        "is_large_cap":   is_large_cap,
    }

# ── Haupt-Scan ────────────────────────────────────────────────────────────────
def scan_all_symbols(symbols, equity=0, config=None):
    """
    Scannt alle Symbole mit der neuen Strategie (Daily + 4H + BTC-RS).

    Returns:
        (setups, watch, errors)
    """
    cfg      = {**DEFAULT_CONFIG, **(config or {})}
    risk_usd = equity * 0.01 if equity > 0 else 100
    setups, watch, errors = [], [], []

    # BTC Daily einmal laden (für RS-Berechnung aller Coins)
    btc_daily = []
    try:
        btc_daily = get_candles("BTCUSDT", "1d", 300)
    except Exception:
        print("[Scan] BTC Daily nicht verfügbar — RS-Filter übersprungen", flush=True)

    for sym in symbols:
        coin = sym.replace("USDT", "")
        try:
            daily_candles = get_candles(sym, "1d", 300)
            candles_4h    = get_candles(sym, "4h", 100)
            is_large      = sym in LARGE_CAPS
            result        = check_new_setup(daily_candles, candles_4h, btc_daily,
                                            is_large_cap=is_large, config=cfg)

            if result is None:
                errors.append(f"{coin} (Daten unvollständig)")
                continue

            if result["pass"]:
                entry     = result["entry"]
                sl        = result["sl"]
                risk_dist = entry - sl
                pos_size  = round(risk_usd / risk_dist, 4) if risk_dist > 0 else 0
                pos_val   = round(pos_size * entry, 2)
                setups.append({
                    "coin":          coin,
                    "symbol":        sym,
                    "entry":         entry,
                    "sl":            sl,
                    "tp":            result["tp"],
                    "sl_pct":        result["sl_pct"],
                    "tp_pct":        result["tp_pct"],
                    "pos_size":      pos_size,
                    "pos_val":       pos_val,
                    "risk_usd":      risk_usd,
                    "rsi":           result["rsi"],
                    "adx":           result.get("adx", 0),
                    "htf_bull":      True,
                    "ema20":         result["ema20"],
                    "atr":           result["atr"],
                    "extension_pct": result.get("extension_pct", 0),
                    "rs_ok":         result.get("rs_ok", True),
                    "dist_pct":      result.get("dist_pct", 0),
                    "breakout_diff": result.get("breakout_diff", 0),
                    "confirm_ok":    result.get("confirm_ok", True),
                    "candles_15m":   candles_4h,   # 4H-Kerzen für den Chart
                })
            elif result.get("watch"):
                watch.append({
                    "coin":     coin,
                    "dist_pct": result.get("dist_pct", 0),
                    "rsi":      result.get("rsi", 0),
                    "adx":      result.get("adx", 0),
                    "filters":  {},
                })

        except Exception as e:
            errors.append(f"{coin} (Fehler: {e})")
            print(f"[Scan] {coin} Fehler: {e}", flush=True)

    return setups, watch, errors
