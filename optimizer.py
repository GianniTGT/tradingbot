"""
EMA Sniper Parameter-Optimizer — Top 7 Coins, letzte 6 Monate

Schritt 1: Daten einmal herunterladen und lokal speichern (data_cache/)
Schritt 2: 81 Parameter-Kombinationen auf gecachten Daten testen
Schritt 3: Beste Kombination nach Profit Factor ausgeben

Ausführen: python optimizer.py
"""
import requests
import time
import json
import os
import itertools
from datetime import datetime, timedelta

# ── Nur die profitablen 7 Coins ────────────────────────────────────────────────
SYMBOLS = ["ATOMUSDT", "LINKUSDT", "BNBUSDT", "DOTUSDT", "SUIUSDT", "INJUSDT", "APTUSDT"]

MONTHS   = 6
CACHE_DIR = os.path.join(os.path.dirname(__file__), "data_cache")

# ── Parameter-Grid ─────────────────────────────────────────────────────────────
GRID = {
    "adx_min":    [20, 25, 30],
    "prox_pct":   [0.3, 0.5, 0.8],
    "atr_mult":   [1.0, 1.5, 2.0],
    "swing_len":  [3, 5, 7],
}

BASE_CONFIG = {
    "ema_lens":    [20, 50, 100, 200],
    "atr_len":     14,
    "crv":         2.0,
    "rsi_len":     14,
    "adx_len":     14,
    "vol_mult":    1.2,
    "range_mult":  0.8,
    "vol_avg_len": 20,
}

# ── Indikator-Serien ───────────────────────────────────────────────────────────
def _ema_full(values, n):
    k, e = 2 / (n + 1), values[0]
    out = []
    for v in values:
        e = v * k + e * (1 - k)
        out.append(e)
    return out

def _rma_full(values, n):
    if not values:
        return []
    seed = min(n, len(values))
    e = sum(values[:seed]) / seed
    out = [e] * seed
    for v in values[seed:]:
        e = (e * (n - 1) + v) / n
        out.append(e)
    return out

def _atr_full(highs, lows, closes, n):
    tr = [highs[0] - lows[0]]
    for i in range(1, len(closes)):
        tr.append(max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i]  - closes[i - 1]),
        ))
    return _rma_full(tr, n)

def _rsi_full(closes, n):
    gains, losses = [0.0], [0.0]
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    ag = _rma_full(gains,  n)
    al = _rma_full(losses, n)
    return [100 - 100 / (1 + g / l) if l > 0 else 100.0 for g, l in zip(ag, al)]

def _adx_full(highs, lows, closes, n):
    dm_p, dm_m, tr_list = [], [], []
    for i in range(1, len(closes)):
        up = highs[i] - highs[i - 1]
        dn = lows[i - 1] - lows[i]
        dm_p.append(up if up > dn and up > 0 else 0.0)
        dm_m.append(dn if dn > up and dn > 0 else 0.0)
        tr_list.append(max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i]  - closes[i - 1]),
        ))
    sdp = _rma_full(dm_p,    n)
    sdm = _rma_full(dm_m,    n)
    str_ = _rma_full(tr_list, n)
    dx = []
    for dmp, dmm, tr in zip(sdp, sdm, str_):
        dip = 100 * dmp / tr if tr > 0 else 0.0
        dim = 100 * dmm / tr if tr > 0 else 0.0
        d   = dip + dim
        dx.append(100 * abs(dip - dim) / d if d > 0 else 0.0)
    return [0.0] + _rma_full(dx, n)

def _vol_sma_full(vols, n):
    out = [0.0] * len(vols)
    for i in range(n, len(vols)):
        out[i] = sum(vols[i - n:i]) / n
    return out

# ── Daten laden & cachen ───────────────────────────────────────────────────────
def fetch_and_cache(symbol, interval, months):
    os.makedirs(CACHE_DIR, exist_ok=True)
    fname = os.path.join(CACHE_DIR, f"{symbol}_{interval}_{months}m.json")

    if os.path.exists(fname):
        print(f"  Cache: {symbol} {interval}")
        with open(fname) as f:
            return json.load(f)

    print(f"  Lade {symbol} {interval} von Binance...", end="", flush=True)
    end_ms   = int(time.time() * 1000)
    start_ms = int((datetime.utcnow() - timedelta(days=months * 30)).timestamp() * 1000)
    candles  = []
    batch_ms = start_ms

    while batch_ms < end_ms:
        try:
            resp = requests.get(
                "https://api.binance.com/api/v3/klines",
                params={"symbol": symbol, "interval": interval,
                        "startTime": batch_ms, "limit": 1000},
                timeout=15,
            )
            resp.raise_for_status()
            batch = resp.json()
            if not batch:
                break
            candles.extend(batch)
            batch_ms = int(batch[-1][0]) + 1
            time.sleep(0.08)
        except Exception as e:
            print(f" Fehler: {e}")
            break

    data = [
        {"t": int(k[0]), "o": float(k[1]), "h": float(k[2]),
         "l": float(k[3]), "c": float(k[4]), "v": float(k[5])}
        for k in candles
    ]
    with open(fname, "w") as f:
        json.dump(data, f)
    print(f" {len(data)} Kerzen → gespeichert")
    return data

# ── Indikator-Precompute pro Coin ──────────────────────────────────────────────
def precompute(symbol, months):
    c15 = fetch_and_cache(symbol, "15m", months)
    c1h = fetch_and_cache(symbol, "1h",  months)

    closes = [c["c"] for c in c15]
    opens  = [c["o"] for c in c15]
    highs  = [c["h"] for c in c15]
    lows   = [c["l"] for c in c15]
    vols   = [c["v"] for c in c15]
    cl_1h  = [c["c"] for c in c1h]
    ts_1h  = [c["t"] for c in c1h]
    ts_15m = [c["t"] for c in c15]

    el = BASE_CONFIG["ema_lens"]
    return {
        "closes": closes, "opens": opens,
        "highs":  highs,  "lows":  lows, "vols": vols,
        "e20":   _ema_full(closes, el[0]),
        "e50":   _ema_full(closes, el[1]),
        "e100":  _ema_full(closes, el[2]),
        "e200":  _ema_full(closes, el[3]),
        "atr":   _atr_full(highs, lows, closes, BASE_CONFIG["atr_len"]),
        "rsi":   _rsi_full(closes, BASE_CONFIG["rsi_len"]),
        "adx":   _adx_full(highs, lows, closes, BASE_CONFIG["adx_len"]),
        "vsma":  _vol_sma_full(vols, BASE_CONFIG["vol_avg_len"]),
        # HTF Alignment
        "e20_1h":  _ema_full(cl_1h, el[0]),
        "e50_1h":  _ema_full(cl_1h, el[1]),
        "e200_1h": _ema_full(cl_1h, el[3]),
        "ts_1h":   ts_1h,
        "ts_15m":  ts_15m,
    }

def get_htf_idx(ts_15m_val, ts_1h):
    for j in range(len(ts_1h) - 1, -1, -1):
        if ts_1h[j] <= ts_15m_val:
            return j
    return 0

# ── Single Backtest auf gecachten Daten ───────────────────────────────────────
def run_backtest(precomp, cfg):
    closes = precomp["closes"]
    opens  = precomp["opens"]
    highs  = precomp["highs"]
    lows   = precomp["lows"]
    vols   = precomp["vols"]
    e20    = precomp["e20"]
    e50    = precomp["e50"]
    e100   = precomp["e100"]
    e200   = precomp["e200"]
    atr    = precomp["atr"]
    rsi    = precomp["rsi"]
    adx    = precomp["adx"]
    vsma   = precomp["vsma"]
    e20_1h  = precomp["e20_1h"]
    e50_1h  = precomp["e50_1h"]
    e200_1h = precomp["e200_1h"]
    ts_1h   = precomp["ts_1h"]
    ts_15m  = precomp["ts_15m"]

    el      = BASE_CONFIG["ema_lens"]
    warmup  = max(el) + BASE_CONFIG["adx_len"] + 10
    wins = losses = 0
    in_trade = False
    entry_p = sl_p = tp_p = 0.0

    for i in range(warmup, len(closes) - 1):
        if in_trade:
            lo, hi = lows[i], highs[i]
            if lo <= sl_p and hi >= tp_p:
                losses += 1; in_trade = False
            elif lo <= sl_p:
                losses += 1; in_trade = False
            elif hi >= tp_p:
                wins   += 1; in_trade = False
            continue

        f1 = e20[i] > e50[i] and e50[i] > e100[i] and e100[i] > e200[i]
        if not f1: continue
        f2 = e20[i] > e20[i-1] > e20[i-2] > e20[i-3]
        if not f2: continue
        dist = (closes[i] - e20[i]) / e20[i] * 100
        if abs(dist) > cfg["prox_pct"]: continue
        if not (30 <= rsi[i] <= 70): continue
        if adx[i] <= cfg["adx_min"]: continue
        if not ((highs[i] - lows[i]) > atr[i] * BASE_CONFIG["range_mult"]
                and vols[i] >= vsma[i] * BASE_CONFIG["vol_mult"]
                and vsma[i] > 0):
            continue
        htf_i = get_htf_idx(ts_15m[i], ts_1h)
        if not (precomp["e20_1h"][htf_i] > precomp["e50_1h"][htf_i]
                and e200_1h[htf_i] < closes[i]):
            continue

        entry_p   = opens[i + 1]
        swing_low = min(lows[max(0, i - cfg["swing_len"] + 1):i + 1])
        sl_p      = swing_low - atr[i] * cfg["atr_mult"]
        risk      = entry_p - sl_p
        if risk <= 0:
            continue
        tp_p     = entry_p + risk * BASE_CONFIG["crv"]
        in_trade = True

    total = wins + losses
    if total == 0:
        return {"wins": 0, "losses": 0, "total": 0,
                "winrate": 0, "pf": 0, "total_r": 0}
    gp  = wins   * BASE_CONFIG["crv"]
    gl  = losses * 1.0
    pf  = round(gp / gl, 2) if gl > 0 else 99.0
    wr  = round(wins / total * 100, 1)
    tr  = round(gp - gl, 1)
    return {"wins": wins, "losses": losses, "total": total,
            "winrate": wr, "pf": pf, "total_r": tr}

# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    print("=" * 60)
    print("EMA SNIPER OPTIMIZER — Top 7 Coins, 6 Monate")
    print("=" * 60)

    # Schritt 1: Daten laden (einmalig)
    print("\n[1/2] Daten laden (beim ersten Mal von Binance, danach aus Cache):")
    precomps = {}
    for sym in SYMBOLS:
        print(f"\n{sym}:")
        precomps[sym] = precompute(sym, MONTHS)

    # Schritt 2: Parameter-Grid testen
    keys   = list(GRID.keys())
    values = list(GRID.values())
    combos = list(itertools.product(*values))
    total  = len(combos)

    print(f"\n[2/2] Teste {total} Parameter-Kombinationen auf {len(SYMBOLS)} Coins...\n")
    results = []

    for idx, combo in enumerate(combos):
        cfg = {**BASE_CONFIG, **dict(zip(keys, combo))}
        agg = {"wins": 0, "losses": 0, "total": 0, "total_r": 0.0}

        for sym in SYMBOLS:
            r = run_backtest(precomps[sym], cfg)
            agg["wins"]    += r["wins"]
            agg["losses"]  += r["losses"]
            agg["total"]   += r["total"]
            agg["total_r"] += r["total_r"]

        total_trades = agg["wins"] + agg["losses"]
        if total_trades == 0:
            continue
        gp = agg["wins"]   * BASE_CONFIG["crv"]
        gl = agg["losses"] * 1.0
        pf = round(gp / gl, 2) if gl > 0 else 99.0
        wr = round(agg["wins"] / total_trades * 100, 1)
        tr = round(gp - gl, 1)

        results.append({
            "adx_min":   cfg["adx_min"],
            "prox_pct":  cfg["prox_pct"],
            "atr_mult":  cfg["atr_mult"],
            "swing_len": cfg["swing_len"],
            "setups":    total_trades,
            "winrate":   wr,
            "pf":        pf,
            "total_r":   tr,
        })

        if (idx + 1) % 20 == 0:
            print(f"  {idx + 1}/{total} getestet...")

    # ── Ausgabe: Top 15 nach Profit Factor ────────────────────────────────────
    results.sort(key=lambda x: (x["pf"], x["total_r"]), reverse=True)

    print("\n" + "=" * 72)
    print("TOP 15 PARAMETER-KOMBINATIONEN (nach Profit Factor)")
    print("=" * 72)
    print(f"{'ADX':>4} {'Prox%':>6} {'ATR×':>5} {'Swing':>6} "
          f"{'Setups':>7} {'Win%':>6} {'PF':>5} {'Total R':>8}")
    print("-" * 72)
    for r in results[:15]:
        print(
            f"{r['adx_min']:>4} {r['prox_pct']:>6} {r['atr_mult']:>5} "
            f"{r['swing_len']:>6} {r['setups']:>7} {r['winrate']:>5}% "
            f"{r['pf']:>5} {r['total_r']:>+8.1f}R"
        )

    if results:
        best = results[0]
        print(f"""
{'═'*60}
✅ BESTE KOMBINATION:
   ADX Minimum:      {best['adx_min']}
   Proximity EMA20:  {best['prox_pct']}%
   ATR-Multiplikator:{best['atr_mult']}
   SwingLow Lookback:{best['swing_len']} Kerzen
{'─'*60}
   Setups:     {best['setups']}
   Winrate:    {best['winrate']}%
   PF:         {best['pf']}
   Total R:    {best['total_r']:+.1f}R
{'═'*60}
""")

if __name__ == "__main__":
    main()
