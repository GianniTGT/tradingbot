"""
EMA Sniper Strategy — 7-Filter Logic (Pine Script v6 Port)
API-agnostisch: nimmt OHLCV-Dicts, gibt Setup-Dict zurück.

Einsatz: Krypto (Binance 15m + 1H) und später Aktien (gleiche Logik, andere Quelle).
"""
from urllib.request import urlopen, Request
from urllib.parse import urlencode
import json, time, hmac, hashlib
import requests

# ── Default Config (spiegelt Pine Script Inputs) ───────────────────────────────
DEFAULT_CONFIG = {
    "ema_lens":    [20, 50, 100, 200],
    "atr_len":     14,
    "atr_mult":    1.5,
    "swing_len":   5,
    "crv":         2.0,
    "prox_pct":    0.5,
    "rsi_len":     14,
    "adx_len":     14,
    "adx_min":     25.0,
    "vol_mult":    1.2,
    "range_mult":  0.8,
    "vol_avg_len": 20,
    # session: None = 24/7 (Krypto).
    # Für Aktien: {"start": "15:30", "end": "22:00", "close_before": "21:45"}
    "session":     None,
}

# ── Preis-Formatierung ─────────────────────────────────────────────────────────
def round_price(v):
    if v > 100: return round(v, 2)
    if v > 1:   return round(v, 4)
    return round(v, 5)

# ── Indikatoren ────────────────────────────────────────────────────────────────
def calc_ema(series, n):
    """Standard EMA, alpha = 2/(n+1). Entspricht Pine Script ta.ema()."""
    if len(series) < 2:
        return series[-1] if series else 0.0
    k, e = 2 / (n + 1), series[0]
    for v in series[1:]:
        e = v * k + e * (1 - k)
    return e

def _rma_series(series, n):
    """
    Wilder's Smoothing (RMA) als Serie — exakt wie Pine Script ta.rma().
    Seed = SMA der ersten n Werte, dann iterativ geglättet.
    """
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
    """ATR via Wilder's RMA — wie Pine Script ta.atr()."""
    tr = []
    for i in range(1, len(closes)):
        tr.append(max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i]  - closes[i - 1]),
        ))
    if not tr:
        return 0.0
    rma = _rma_series(tr, n)
    return rma[-1]

def calc_rsi(closes, n=14):
    """RSI via Wilder's RMA — wie Pine Script ta.rsi()."""
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
    """
    ADX via Wilder's RMA — exakt wie Pine Script ta.dmi().
    Gibt (di_plus, di_minus, adx) zurück.
    """
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

    adx   = _rma_series(dx_list, n)[-1]
    di_p  = di_p_list[-1]  if di_p_list  else 0.0
    di_m  = di_m_list[-1] if di_m_list else 0.0
    return di_p, di_m, adx

# ── Binance Account ────────────────────────────────────────────────────────────
def get_binance_usdt_balance(api_key, api_secret, fallback=0.0):
    """
    Holt das freie USDT-Guthaben vom Binance Spot Account (signierter Request).
    Gibt fallback zurück wenn kein Key gesetzt oder Fehler.
    """
    if not api_key or not api_secret:
        return fallback
    try:
        ts      = int(time.time() * 1000)
        params  = f"timestamp={ts}"
        sig     = hmac.new(api_secret.encode(), params.encode(), hashlib.sha256).hexdigest()
        url     = f"https://api.binance.com/api/v3/account?{params}&signature={sig}"
        req     = Request(url, headers={"X-MBX-APIKEY": api_key})
        with urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        for b in data.get("balances", []):
            if b["asset"] == "USDT":
                return float(b["free"])
    except Exception:
        pass
    return fallback

# ── Binance Daten ──────────────────────────────────────────────────────────────
def get_candles(symbol: str, interval: str, limit: int = 300) -> list:
    """
    Holt OHLCV-Kerzen von Binance Spot API.
    Gibt Liste von Dicts zurück: [{"open", "high", "low", "close", "volume"}, ...]
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

# Alias für Rückwärtskompatibilität mit bot_server.py
get_binance_candles = get_candles

# ── 7-Filter Core ──────────────────────────────────────────────────────────────
def check_ema_sniper_setup(candles_15m, candles_1h, config=None):
    """
    Prüft alle 7 Filter des EMA Sniper auf dem letzten geschlossenen Candle.

    Args:
        candles_15m: Liste von OHLCV-Dicts (15m, mind. 250 Kerzen)
        candles_1h:  Liste von OHLCV-Dicts (1H, mind. 250 Kerzen)
        config:      Optionaler Config-Dict (Default = DEFAULT_CONFIG)

    Returns:
        Dict mit Feldern:
          pass=True  → alle 7 Filter bestanden, Setup aktiv
          pass=False, watch=True → EMA-Stack ok, nahe EMA20, aber nicht alle Filter
          pass=False, watch=False → kein Setup, kein Interesse
        Oder None wenn Daten unvollständig.
    """
    cfg = {**DEFAULT_CONFIG, **(config or {})}
    el  = cfg["ema_lens"]

    if len(candles_15m) < max(el) + 20 or len(candles_1h) < max(el) + 20:
        return None

    # Arrays extrahieren (15m)
    closes = [c["close"]  for c in candles_15m]
    opens  = [c["open"]   for c in candles_15m]
    highs  = [c["high"]   for c in candles_15m]
    lows   = [c["low"]    for c in candles_15m]
    vols   = [c["volume"] for c in candles_15m]

    cur_close = closes[-1]
    cur_high  = highs[-1]
    cur_low   = lows[-1]

    # EMAs (15m) — 4 Staffel
    ema20  = calc_ema(closes,       el[0])
    ema50  = calc_ema(closes,       el[1])
    ema100 = calc_ema(closes,       el[2])
    ema200 = calc_ema(closes,       el[3])

    # EMA20-Verlauf für Trend-Filter (3 zurückliegende Werte)
    ema20_1 = calc_ema(closes[:-1], el[0])
    ema20_2 = calc_ema(closes[:-2], el[0])
    ema20_3 = calc_ema(closes[:-3], el[0])

    # Indikatoren (15m)
    atr = calc_atr(highs, lows, closes, cfg["atr_len"])
    rsi = calc_rsi(closes,              cfg["rsi_len"])
    _, _, adx = calc_adx(highs, lows, closes, cfg["adx_len"])

    # Volumen-SMA20 (ohne aktuelle Kerze = saubererer Vergleich)
    vol_avg = sum(vols[-(cfg["vol_avg_len"] + 1):-1]) / cfg["vol_avg_len"]
    cur_vol = vols[-1]

    # HTF 1H
    closes_1h = [c["close"] for c in candles_1h]
    ema20_1h  = calc_ema(closes_1h, el[0])
    ema50_1h  = calc_ema(closes_1h, el[1])
    ema200_1h = calc_ema(closes_1h, el[3])
    htf_bull  = closes_1h[-1] > ema200_1h and ema20_1h > ema50_1h

    dist_pct = (cur_close - ema20) / ema20 * 100

    # ── Die 7 Filter ──────────────────────────────────────────────────────────
    f1_stack     = ema20 > ema50 and ema50 > ema100 and ema100 > ema200
    f2_trend     = ema20 > ema20_1 and ema20_1 > ema20_2 and ema20_2 > ema20_3
    f3_prox      = abs(dist_pct) <= cfg["prox_pct"]
    f4_rsi       = 30 <= rsi <= 70
    f5_adx       = adx > cfg["adx_min"]
    f6_range_vol = (cur_high - cur_low) > atr * cfg["range_mult"] and cur_vol >= vol_avg * cfg["vol_mult"]
    f7_htf       = htf_bull

    filters = {
        "stack":     f1_stack,
        "trend":     f2_trend,
        "prox":      f3_prox,
        "rsi":       f4_rsi,
        "adx":       f5_adx,
        "range_vol": f6_range_vol,
        "htf":       f7_htf,
    }

    base_info = {
        "ema20":    round_price(ema20),
        "ema50":    round_price(ema50),
        "ema100":   round_price(ema100),
        "ema200":   round_price(ema200),
        "rsi":      round(rsi,  1),
        "adx":      round(adx,  1),
        "htf_bull": htf_bull,
        "dist_pct": round(dist_pct, 2),
        "filters":  filters,
    }

    if not (f1_stack and f2_trend and f3_prox and f4_rsi and f5_adx and f6_range_vol and f7_htf):
        watch = f1_stack and f2_trend and abs(dist_pct) <= cfg["prox_pct"] * 2
        return {"pass": False, "watch": watch, **base_info}

    # ── SL / TP berechnen ─────────────────────────────────────────────────────
    swing_low = min(lows[-cfg["swing_len"]:])
    sl        = round_price(swing_low - atr * cfg["atr_mult"])
    entry     = round_price(cur_close)
    risk_dist = entry - sl

    if risk_dist <= 0:
        return {"pass": False, "watch": False, **base_info}

    tp     = round_price(entry + risk_dist * cfg["crv"])
    sl_pct = round(risk_dist / entry * 100, 2)
    tp_pct = round(risk_dist * cfg["crv"] / entry * 100, 2)

    return {
        "pass":    True,
        "watch":   False,
        "entry":   entry,
        "sl":      sl,
        "tp":      tp,
        "sl_pct":  sl_pct,
        "tp_pct":  tp_pct,
        "atr":     round(atr, 6),
        **base_info,
    }

# ── Haupt-Scan ─────────────────────────────────────────────────────────────────
def scan_all_symbols(symbols, equity=0, config=None):
    """
    Scannt alle Symbole mit EMA Sniper Strategie (7 Filter).

    Args:
        symbols: Liste von Binance-Symbol-Strings (z.B. ["BTCUSDT", ...])
        equity:  Gesamtkapital in USD (für 1%-Risiko Position Sizing)
        config:  Optionaler Config-Dict

    Returns:
        (setups, watch, errors)
        setups: Liste von Dicts mit vollständigem Setup inkl. candles_15m
        watch:  Liste von Dicts {"coin", "dist_pct", "rsi", "adx", "filters"}
        errors: Liste von Strings (Coin + Fehlergrund)
    """
    cfg      = {**DEFAULT_CONFIG, **(config or {})}
    risk_usd = equity * 0.01 if equity > 0 else 100
    setups, watch, errors = [], [], []

    for sym in symbols:
        coin = sym.replace("USDT", "")
        try:
            candles_15m = get_candles(sym, "15m", 300)
            candles_1h  = get_candles(sym, "1h",  300)
            result      = check_ema_sniper_setup(candles_15m, candles_1h, cfg)

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
                    "coin":        coin,
                    "symbol":      sym,
                    "entry":       entry,
                    "sl":          sl,
                    "tp":          result["tp"],
                    "sl_pct":      result["sl_pct"],
                    "tp_pct":      result["tp_pct"],
                    "pos_size":    pos_size,
                    "pos_val":     pos_val,
                    "risk_usd":    risk_usd,
                    "rsi":         result["rsi"],
                    "adx":         result["adx"],
                    "htf_bull":    result["htf_bull"],
                    "ema20":       result["ema20"],
                    "atr":         result["atr"],
                    "candles_15m": candles_15m,
                })
            elif result.get("watch"):
                watch.append({
                    "coin":     coin,
                    "dist_pct": result["dist_pct"],
                    "rsi":      result["rsi"],
                    "adx":      result["adx"],
                    "filters":  result["filters"],
                })

        except Exception:
            errors.append(f"{coin} (Fehler)")

    return setups, watch, errors
