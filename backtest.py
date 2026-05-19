"""
EMA Sniper Backtest — 6 Monate historische Daten
Testet alle 7 Filter auf 15m + 1H Daten ohne Lookahead.

Ausführen: python backtest.py
Output:    Konsole + backtest_results.csv
"""
import requests
import time
import csv
from datetime import datetime, timedelta

# ── Config ─────────────────────────────────────────────────────────────────────
SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "LINKUSDT",
    "NEARUSDT", "AVAXUSDT", "MAGICUSDT", "DOTUSDT", "ADAUSDT",
    "XRPUSDT", "SUIUSDT", "INJUSDT", "APTUSDT", "ARBUSDT",
    "MATICUSDT", "OPUSDT", "DOGEUSDT", "ATOMUSDT", "LTCUSDT",
]

CONFIG = {
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
    "months":      6,
}

# ── Indikator-Serien (kein Lookahead — volle Serie) ───────────────────────────

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
    gains  = [0.0]
    losses = [0.0]
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
    s_dmp = _rma_full(dm_p,    n)
    s_dmm = _rma_full(dm_m,    n)
    s_tr  = _rma_full(tr_list, n)
    dx = []
    for dmp, dmm, tr in zip(s_dmp, s_dmm, s_tr):
        dip   = 100 * dmp / tr if tr > 0 else 0.0
        dim   = 100 * dmm / tr if tr > 0 else 0.0
        denom = dip + dim
        dx.append(100 * abs(dip - dim) / denom if denom > 0 else 0.0)
    adx = _rma_full(dx, n)
    return [0.0] + adx  # Index 0 hat kein gültiges ADX → 0

def _vol_sma_full(vols, n):
    """SMA(n) ohne aktuelle Kerze (wie in strategy.py)."""
    out = [0.0] * len(vols)
    for i in range(n, len(vols)):
        out[i] = sum(vols[i - n:i]) / n
    return out

# ── Daten laden ────────────────────────────────────────────────────────────────

def fetch_historical(symbol, interval, months):
    """Lädt historische Kerzen (paginiert) für die letzten N Monate."""
    end_ms   = int(time.time() * 1000)
    start_ms = int((datetime.utcnow() - timedelta(days=months * 30)).timestamp() * 1000)
    candles  = []
    batch_ms = start_ms

    print(f"  Lade {symbol} {interval}...", end="", flush=True)
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
            time.sleep(0.08)  # Rate-Limit-Puffer
        except Exception as e:
            print(f" Fehler: {e}")
            break

    print(f" {len(candles)} Kerzen")
    return [
        {"t": int(k[0]), "o": float(k[1]), "h": float(k[2]),
         "l": float(k[3]), "c": float(k[4]), "v": float(k[5])}
        for k in candles
    ]

# ── Backtest ───────────────────────────────────────────────────────────────────

def backtest_symbol(symbol, cfg):
    coin = symbol.replace("USDT", "")
    c15  = fetch_historical(symbol, "15m", cfg["months"])
    c1h  = fetch_historical(symbol, "1h",  cfg["months"])

    if len(c15) < 250 or len(c1h) < 210:
        return None

    # Arrays
    closes = [c["c"] for c in c15]
    opens  = [c["o"] for c in c15]
    highs  = [c["h"] for c in c15]
    lows   = [c["l"] for c in c15]
    vols   = [c["v"] for c in c15]

    cl_1h = [c["c"] for c in c1h]

    # Indikator-Serien (15m)
    el = cfg["ema_lens"]
    e20  = _ema_full(closes, el[0])
    e50  = _ema_full(closes, el[1])
    e100 = _ema_full(closes, el[2])
    e200 = _ema_full(closes, el[3])
    atr  = _atr_full(highs, lows, closes, cfg["atr_len"])
    rsi  = _rsi_full(closes, cfg["rsi_len"])
    adx  = _adx_full(highs, lows, closes, cfg["adx_len"])
    vsma = _vol_sma_full(vols, cfg["vol_avg_len"])

    # Indikator-Serien (1H) — für HTF Confluence
    e20_1h  = _ema_full(cl_1h, el[0])
    e50_1h  = _ema_full(cl_1h, el[1])
    e200_1h = _ema_full(cl_1h, el[3])

    # HTF-Zeitstempel-Alignment: finde zu jedem 15m-Bar den passenden 1H-Bar
    ts_1h = [c["t"] for c in c1h]

    def get_htf_idx(ts_15m):
        """Gibt den letzten abgeschlossenen 1H-Bar-Index zurück."""
        for j in range(len(ts_1h) - 1, -1, -1):
            if ts_1h[j] <= ts_15m:
                return j
        return 0

    warmup = max(el) + cfg["adx_len"] + 10
    trades = []
    in_trade = False
    entry_price = sl_price = tp_price = 0.0

    for i in range(warmup, len(closes) - 1):
        # ── Trade-Exit prüfen ─────────────────────────────────────────────────
        if in_trade:
            lo, hi = lows[i], highs[i]
            if lo <= sl_price and hi >= tp_price:
                # Beide auf selber Kerze → SL (worst case)
                trades[-1]["result"] = "SL"
                trades[-1]["exit_bar"] = i
                trades[-1]["pnl_r"] = -1.0
            elif lo <= sl_price:
                trades[-1]["result"] = "SL"
                trades[-1]["exit_bar"] = i
                trades[-1]["pnl_r"] = -1.0
            elif hi >= tp_price:
                trades[-1]["result"] = "TP"
                trades[-1]["exit_bar"] = i
                trades[-1]["pnl_r"] = cfg["crv"]
            else:
                continue  # Trade noch offen
            in_trade = False
            continue

        # ── 7 Filter ─────────────────────────────────────────────────────────
        f1 = e20[i] > e50[i] and e50[i] > e100[i] and e100[i] > e200[i]
        if not f1: continue

        f2 = e20[i] > e20[i-1] > e20[i-2] > e20[i-3]
        if not f2: continue

        dist = (closes[i] - e20[i]) / e20[i] * 100
        f3 = abs(dist) <= cfg["prox_pct"]
        if not f3: continue

        f4 = 30 <= rsi[i] <= 70
        if not f4: continue

        f5 = adx[i] > cfg["adx_min"]
        if not f5: continue

        f6 = ((highs[i] - lows[i]) > atr[i] * cfg["range_mult"]
              and vols[i] >= vsma[i] * cfg["vol_mult"] and vsma[i] > 0)
        if not f6: continue

        # HTF
        htf_i = get_htf_idx(c15[i]["t"])
        f7 = cl_1h[htf_i] > e200_1h[htf_i] and e20_1h[htf_i] > e50_1h[htf_i]
        if not f7: continue

        # ── Setup gefunden → Entry ────────────────────────────────────────────
        entry_price = opens[i + 1]  # nächste Kerze Open
        swing_low   = min(lows[max(0, i - cfg["swing_len"] + 1):i + 1])
        sl_price    = swing_low - atr[i] * cfg["atr_mult"]
        risk_dist   = entry_price - sl_price
        if risk_dist <= 0:
            continue
        tp_price = entry_price + risk_dist * cfg["crv"]
        sl_pct   = risk_dist / entry_price * 100

        dt = datetime.utcfromtimestamp(c15[i]["t"] / 1000).strftime("%Y-%m-%d %H:%M")
        trades.append({
            "coin":      coin,
            "date":      dt,
            "entry":     round(entry_price, 6),
            "sl":        round(sl_price, 6),
            "tp":        round(tp_price, 6),
            "sl_pct":    round(sl_pct, 2),
            "entry_bar": i + 1,
            "exit_bar":  None,
            "result":    "OFFEN",
            "pnl_r":     0.0,
        })
        in_trade = True

    return trades

# ── Statistiken ────────────────────────────────────────────────────────────────

def statistics(all_trades):
    closed = [t for t in all_trades if t["result"] in ("TP", "SL")]
    wins   = [t for t in closed if t["result"] == "TP"]
    losses = [t for t in closed if t["result"] == "SL"]

    if not closed:
        return {}

    winrate      = len(wins) / len(closed) * 100
    total_r      = sum(t["pnl_r"] for t in closed)
    avg_win_r    = sum(t["pnl_r"] for t in wins)   / len(wins)   if wins   else 0
    avg_loss_r   = sum(t["pnl_r"] for t in losses) / len(losses) if losses else 0
    gross_profit = sum(t["pnl_r"] for t in wins)
    gross_loss   = abs(sum(t["pnl_r"] for t in losses))
    pf           = gross_profit / gross_loss if gross_loss > 0 else float("inf")

    return {
        "total":      len(all_trades),
        "closed":     len(closed),
        "wins":       len(wins),
        "losses":     len(losses),
        "open":       len(all_trades) - len(closed),
        "winrate":    round(winrate, 1),
        "total_r":    round(total_r, 2),
        "avg_win_r":  round(avg_win_r, 2),
        "avg_loss_r": round(avg_loss_r, 2),
        "pf":         round(pf, 2),
    }

# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print(f"EMA SNIPER BACKTEST — letzte {CONFIG['months']} Monate")
    print("=" * 60)

    all_trades = []
    per_coin   = []

    for sym in SYMBOLS:
        print(f"\n{sym}:")
        trades = backtest_symbol(sym, CONFIG)
        if trades is None:
            print("  Nicht genug Daten — übersprungen")
            continue
        all_trades.extend(trades)
        stats = statistics(trades)
        if stats:
            per_coin.append({"symbol": sym.replace("USDT",""), **stats})
            print(
                f"  Setups: {stats['total']} | "
                f"Closed: {stats['closed']} | "
                f"Win: {stats['wins']} ({stats['winrate']}%) | "
                f"PF: {stats['pf']} | "
                f"Total R: {stats['total_r']:+.1f}R"
            )
        else:
            print("  Keine abgeschlossenen Trades")

    # ── Gesamt-Statistik ──────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("GESAMT-ERGEBNIS (alle 20 Coins)")
    print("=" * 60)
    total_stats = statistics(all_trades)
    if total_stats:
        print(f"  Setups gesamt:   {total_stats['total']}")
        print(f"  Abgeschlossen:   {total_stats['closed']}")
        print(f"  Gewinner (TP):   {total_stats['wins']}")
        print(f"  Verlierer (SL):  {total_stats['losses']}")
        print(f"  Noch offen:      {total_stats['open']}")
        print(f"  Winrate:         {total_stats['winrate']}%")
        print(f"  Profit Factor:   {total_stats['pf']}")
        print(f"  Total R:         {total_stats['total_r']:+.2f}R")
        print(f"  Ø Gewinner:      +{total_stats['avg_win_r']}R")
        print(f"  Ø Verlierer:     {total_stats['avg_loss_r']}R")

    # ── CSV Export ────────────────────────────────────────────────────────────
    if all_trades:
        fname = "backtest_results.csv"
        with open(fname, "w", newline="", encoding="utf-8") as f:
            fields = ["coin","date","entry","sl","tp","sl_pct","result","pnl_r"]
            w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            w.writeheader()
            w.writerows(all_trades)
        print(f"\n✅ Alle Trades gespeichert: {fname}")

    # ── Pro-Coin-Tabelle ──────────────────────────────────────────────────────
    if per_coin:
        print("\n" + "─" * 60)
        print(f"{'Coin':<8} {'Setups':>6} {'Win%':>6} {'PF':>5} {'Total R':>8}")
        print("─" * 60)
        for c in sorted(per_coin, key=lambda x: x["total_r"], reverse=True):
            print(f"{c['symbol']:<8} {c['total']:>6} {c['winrate']:>5}% {c['pf']:>5} {c['total_r']:>+8.1f}R")

if __name__ == "__main__":
    main()
