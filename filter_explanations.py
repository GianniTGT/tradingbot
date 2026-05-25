"""
filter_explanations.py
Generiert 8 druckfertige Lern-Charts (A4 Querformat, 200 DPI) für die
8 Filter der VCP Balanced Strategie. Ein Bild pro Filter — zum Drucken
und an die Wand kleben.

Verwendung:  python filter_explanations.py
Output:      ./filter_explanations/F1_*.png ... F8_*.png
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, Rectangle
import numpy as np

from strategy import get_candles

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "filter_explanations")
os.makedirs(OUT_DIR, exist_ok=True)

# ── Style ─────────────────────────────────────────────────────────────────────
plt.rcParams.update({
    'font.family':         'DejaVu Sans',
    'axes.grid':           True,
    'grid.alpha':          0.22,
    'axes.spines.top':     False,
    'axes.spines.right':   False,
    'axes.edgecolor':      '#444',
})

GREEN, RED, BLUE, ORANGE, GRAY = '#26a69a', '#ef5350', '#1976d2', '#ff9800', '#888888'
TITLE_COLOR  = '#0d1b2a'
PASTEL_GREEN = '#d4edda'
PASTEL_RED   = '#f8d7da'

# ── Helper ────────────────────────────────────────────────────────────────────
def ema_series(closes, n):
    if not closes: return []
    k = 2 / (n + 1)
    out = [closes[0]]
    for v in closes[1:]:
        out.append(out[-1] * (1 - k) + v * k)
    return out

def setup_figure(filter_num, title, rule):
    """A4 Querformat, mit Titel und Regel oben."""
    fig = plt.figure(figsize=(11.69, 8.27), facecolor='white')
    fig.text(0.5, 0.945, f"F{filter_num}  —  {title}",
             ha='center', fontsize=28, weight='bold', color=TITLE_COLOR)
    fig.text(0.5, 0.895, rule,
             ha='center', fontsize=13, color='#555', style='italic')
    ax = fig.add_axes([0.08, 0.22, 0.86, 0.60])
    return fig, ax

def add_explanation(fig, text):
    fig.text(0.5, 0.10, text, ha='center', fontsize=11.5, color='#1a1a1a',
             bbox=dict(boxstyle='round,pad=0.7', facecolor='#eef3f8',
                       edgecolor='#88a', linewidth=1))

def save(fig, filter_num, name):
    fig.text(0.5, 0.025,
             "VCP Balanced Strategie  ·  Print für die Wand  ·  github.com/GianniTGT/tradingbot",
             ha='center', fontsize=8.5, color='#aaa')
    path = os.path.join(OUT_DIR, f"F{filter_num}_{name}.png")
    fig.savefig(path, dpi=200, bbox_inches='tight', facecolor='white')
    plt.close(fig)
    print(f"  OK  {path}")

# ══════════════════════════════════════════════════════════════════════════════
# F1 — DAILY GOLDEN CROSS
# ══════════════════════════════════════════════════════════════════════════════
def make_f1():
    fig, ax = setup_figure(1, "Daily Golden Cross",
                           "EMA50 > EMA200  UND  EMA200 steigt")
    # Synthetisches sauberes Beispiel — zeigt Bärenmarkt → Wende → Bullenmarkt
    np.random.seed(3)
    n = 400
    # Phase 1: Bärenmarkt (Tag 0-150) — Preis fällt
    p1 = 100 + np.cumsum(np.random.randn(150) * 0.8 - 0.4)
    # Phase 2: Bodenbildung (Tag 150-220) — Seitwärts
    p2 = p1[-1] + np.cumsum(np.random.randn(70) * 0.6)
    # Phase 3: Bullenmarkt (Tag 220-400) — Preis steigt
    p3 = p2[-1] + np.cumsum(np.random.randn(180) * 0.8 + 0.5)
    closes = np.concatenate([p1, p2, p3]).tolist()
    e50, e200 = ema_series(closes, 50), ema_series(closes, 200)
    x = np.arange(len(closes))

    ax.plot(x, closes, color='#333', linewidth=1.0, alpha=0.5, label='Preis')
    ax.plot(x, e50,    color=ORANGE, linewidth=2.6, label='EMA50')
    ax.plot(x, e200,   color=GRAY,   linewidth=2.6, label='EMA200')

    cross_idx = None
    for i in range(51, len(e50)):
        if e50[i] > e200[i] and e50[i-1] <= e200[i-1]:
            cross_idx = i; break

    if cross_idx:
        ax.axvline(cross_idx, color=GREEN, linewidth=2, linestyle='--', alpha=0.7)
        ax.annotate('● GOLDEN CROSS\nKauf-Modus AN',
                    xy=(cross_idx, e50[cross_idx]),
                    xytext=(cross_idx+25, e50[cross_idx]*0.75),
                    fontsize=15, color=GREEN, weight='bold',
                    arrowprops=dict(arrowstyle='->', color=GREEN, lw=2.5))
        y_min, y_max = min(closes), max(closes)
        ax.axvspan(0, cross_idx,           color=RED,   alpha=0.06)
        ax.axvspan(cross_idx, len(closes), color=GREEN, alpha=0.06)
        ax.text(cross_idx/2, y_max*0.97, '✗ NICHT kaufen', ha='center',
                fontsize=13, color=RED, weight='bold')
        ax.text((cross_idx+len(closes))/2, y_max*0.97, '✓ Setup erlaubt',
                ha='center', fontsize=13, color=GREEN, weight='bold')

    ax.set_title("Beispiel: Bärenmarkt → Bodenbildung → Golden Cross → Bullenmarkt",
                 fontsize=11, pad=10)
    ax.set_ylabel("Preis (USDT)")
    ax.legend(loc='upper left', fontsize=10, framealpha=0.9)

    add_explanation(fig,
        "WARUM:  EMA50 unter EMA200 = Markt fällt → wir kaufen NIE in Abwärtstrends.\n"
        "Sobald EMA50 das EMA200 von unten nach oben kreuzt → Trendwende bestätigt → Kauf-Modus AN.")
    save(fig, 1, "Daily_Golden_Cross")

# ══════════════════════════════════════════════════════════════════════════════
# F2 — NICHT ZU EXTENDED
# ══════════════════════════════════════════════════════════════════════════════
def make_f2():
    fig, ax = setup_figure(2, "Nicht zu extended",
                           "Large Caps max. 12%  |  Altcoins max. 15% über Daily EMA50")
    candles = get_candles("NEARUSDT", "1d", 180)
    closes  = [c["close"] for c in candles]
    e50     = ema_series(closes, 50)
    x       = np.arange(len(closes))

    ax.plot(x, closes, color='#333', linewidth=1.4, label='NEAR Preis')
    ax.plot(x, e50,    color=ORANGE, linewidth=2.6, label='EMA50')

    upper15 = [v * 1.15 for v in e50]
    ax.fill_between(x, e50, upper15, color=GREEN, alpha=0.15, label='✓ Kaufzone (0–15%)')
    ax.plot(x, upper15, color=GREEN, linewidth=1.2, linestyle='--')

    upper50 = [v * 1.50 for v in e50]
    ax.fill_between(x, upper15, upper50, color=RED, alpha=0.12, label='✗ Überdehnt (>15%)')

    cur_ext = (closes[-1] - e50[-1]) / e50[-1] * 100
    ax.annotate(f'NEAR JETZT:\n+{cur_ext:.0f}% über EMA50\n→ NICHT kaufen',
                xy=(len(closes)-1, closes[-1]),
                xytext=(len(closes)-50, closes[-1]*0.85),
                fontsize=13, color=RED, weight='bold', ha='right',
                arrowprops=dict(arrowstyle='->', color=RED, lw=2.5))

    ax.set_title("Beispiel: NEAR Daily — Preis ist viel zu weit über EMA50",
                 fontsize=11, pad=10)
    ax.set_ylabel("Preis (USDT)")
    ax.legend(loc='upper left', fontsize=10, framealpha=0.9)

    add_explanation(fig,
        "WARUM:  Coins die zu weit über ihrem EMA50 stehen → Korrektur zur EMA50 ist sehr wahrscheinlich.\n"
        "Wir wollen Coins in der Pullback-Zone (0–15% über EMA50) — nicht am Top kaufen!")
    save(fig, 2, "Nicht_Extended")

# ══════════════════════════════════════════════════════════════════════════════
# F3 — 30-TAGE-HOCH BREAKOUT
# ══════════════════════════════════════════════════════════════════════════════
def make_f3():
    fig, ax = setup_figure(3, "30-Tage-Hoch Breakout",
                           "Tagesschluss muss das Hoch der letzten 30 Tage überschreiten")
    candles = get_candles("LINKUSDT", "1d", 120)
    closes  = [c["close"] for c in candles]
    highs   = [c["high"]  for c in candles]
    x       = np.arange(len(closes))

    # 30-day rolling high
    rolling_high = [max(highs[max(0,i-30):i]) if i > 0 else highs[0] for i in range(len(highs))]

    ax.plot(x, closes,        color='#333', linewidth=1.5, label='LINK Preis')
    ax.plot(x, rolling_high,  color=RED,   linewidth=2.0, linestyle='--', label='30-Tage-Hoch')

    # find breakout
    bo_idx = None
    for i in range(31, len(closes)):
        if closes[i] > rolling_high[i-1] >= closes[i-1]:
            bo_idx = i; break
    if bo_idx:
        ax.scatter(bo_idx, closes[bo_idx], color=GREEN, s=300, zorder=5, marker='^',
                   edgecolor='black', linewidth=2)
        ax.annotate('★ BREAKOUT!\n30-Tage-Hoch geknackt',
                    xy=(bo_idx, closes[bo_idx]),
                    xytext=(bo_idx-25, closes[bo_idx]*1.15),
                    fontsize=14, color=GREEN, weight='bold', ha='left',
                    arrowprops=dict(arrowstyle='->', color=GREEN, lw=2.5))

    ax.set_title("Beispiel: LINK Daily — Frischer Trend-Start nach Konsolidierung",
                 fontsize=11, pad=10)
    ax.set_ylabel("Preis (USDT)")
    ax.legend(loc='upper left', fontsize=10, framealpha=0.9)

    add_explanation(fig,
        "WARUM:  Ein neues 30-Tage-Hoch zeigt: alle Verkäufer der letzten 30 Tage sind im Plus.\n"
        "Niemand will mehr verkaufen → starke Kaufkraft → echter Trend-Start (Minervini-Stil).")
    save(fig, 3, "30Tage_Breakout")

# ══════════════════════════════════════════════════════════════════════════════
# F4 — RELATIVE STÄRKE (Coin/BTC Ratio steigt)
# ══════════════════════════════════════════════════════════════════════════════
def make_f4():
    fig, ax = setup_figure(4, "Relative Stärke",
                           "Coin/BTC Ratio-EMA steigt auf Daily — Coin schlägt BTC")
    # Use NEAR as example (recently strong vs BTC)
    coin_c = [c["close"] for c in get_candles("NEARUSDT", "1d", 120)]
    btc_c  = [c["close"] for c in get_candles("BTCUSDT",  "1d", 120)]
    n      = min(len(coin_c), len(btc_c))
    ratio  = [coin_c[-n+i] / btc_c[-n+i] * 100000 for i in range(n)]  # *100k for scale
    ratio_ema = ema_series(ratio, 20)
    x = np.arange(n)

    ax.plot(x, ratio,     color='#888', linewidth=1.2, alpha=0.6, label='NEAR / BTC')
    ax.plot(x, ratio_ema, color=BLUE,   linewidth=3,   label='RS-EMA20')

    # Show direction comparison
    if ratio_ema[-1] > ratio_ema[-10]:
        ax.annotate('✓ RS-EMA STEIGT\nNEAR schlägt BTC',
                    xy=(n-1, ratio_ema[-1]),
                    xytext=(n-40, ratio_ema[-1]*1.10),
                    fontsize=14, color=GREEN, weight='bold', ha='center',
                    arrowprops=dict(arrowstyle='->', color=GREEN, lw=2.5))

    # Zones
    ax.axhspan(min(ratio)*0.95, np.mean(ratio), color=RED,   alpha=0.06,
               label='✗ schwacher Coin')
    ax.axhspan(np.mean(ratio),  max(ratio)*1.05, color=GREEN, alpha=0.06,
               label='✓ RS Leader')

    ax.set_title("Beispiel: NEAR/BTC Ratio Daily (120 Tage)", fontsize=11, pad=10)
    ax.set_ylabel("Ratio (NEAR / BTC, skaliert)")
    ax.legend(loc='upper left', fontsize=10, framealpha=0.9)

    add_explanation(fig,
        "WARUM:  Wenn BTC steigt → fast alle Coins steigen. Aber nur die STÄRKSTEN schlagen BTC.\n"
        "Wir kaufen nur RS-Leader — sie bringen 2–3× mehr Gewinn als der Markt.")
    save(fig, 4, "Relative_Staerke")

# ══════════════════════════════════════════════════════════════════════════════
# F5 — RS RESILIENZ (Coin hält wenn BTC fällt)
# ══════════════════════════════════════════════════════════════════════════════
def make_f5():
    fig, ax = setup_figure(5, "RS Resilienz",
                           "Wenn BTC fällt → Coin verliert deutlich weniger (gestaffelt)")
    # Synthetisches Beispiel — klarer als reale Daten
    days = np.arange(30)
    np.random.seed(7)
    btc = 100 + np.cumsum(np.random.randn(30) * 0.5)
    coin_strong = btc.copy()
    coin_weak   = btc.copy()
    # Inject 3 BTC drop days
    drop_days = [8, 17, 24]
    for d in drop_days:
        btc[d:] -= 3.0
        coin_strong[d:] -= 0.5   # resilient — hält sich
        coin_weak[d:]   -= 4.0   # bricht stärker ein

    ax.plot(days, btc,         color=ORANGE, linewidth=2.5, label='BTC')
    ax.plot(days, coin_strong, color=GREEN,  linewidth=2.5, label='✓ Resilienter Coin')
    ax.plot(days, coin_weak,   color=RED,    linewidth=2.5, label='✗ Schwacher Coin')

    for d in drop_days:
        ax.axvline(d, color='#aaa', linestyle=':', alpha=0.6)
        ax.text(d, ax.get_ylim()[1]*0.99, 'BTC -3%', ha='center', fontsize=9,
                color=ORANGE, weight='bold', alpha=0.8)

    ax.annotate('Coin hält\n(nur -0.5%)',
                xy=(drop_days[-1]+1, coin_strong[drop_days[-1]+1]),
                xytext=(drop_days[-1]-3, coin_strong[drop_days[-1]+1]+3),
                fontsize=12, color=GREEN, weight='bold',
                arrowprops=dict(arrowstyle='->', color=GREEN, lw=2))
    ax.annotate('Coin bricht ein\n(-4%)',
                xy=(drop_days[-1]+1, coin_weak[drop_days[-1]+1]),
                xytext=(drop_days[-1]-3, coin_weak[drop_days[-1]+1]-5),
                fontsize=12, color=RED, weight='bold',
                arrowprops=dict(arrowstyle='->', color=RED, lw=2))

    ax.set_title("Beispiel: BTC fällt 3× — wie reagieren Coin A vs Coin B?",
                 fontsize=11, pad=10)
    ax.set_xlabel("Tag")
    ax.set_ylabel("Preis-Index (Start = 100)")
    ax.legend(loc='lower left', fontsize=10, framealpha=0.9)

    add_explanation(fig,
        "REGEL:  BTC -1.5% → Coin max -0.5%   |   BTC -2% → Coin max -1%   |   BTC -3% → Coin max -1.5%\n"
        "WARUM:  Coins die in BTC-Drops hart fallen sind schwach. Resiliente Coins = echte Kaufkraft.")
    save(fig, 5, "RS_Resilienz")

# ══════════════════════════════════════════════════════════════════════════════
# F6 — 4H EMA20 PULLBACK
# ══════════════════════════════════════════════════════════════════════════════
def make_f6():
    fig, ax = setup_figure(6, "4H EMA20 Pullback",
                           "Preis fällt zurück nahe EMA20 — max. 3% Abstand")
    # synthetic data showing pullback
    x = np.arange(100)
    np.random.seed(42)
    trend  = 100 + x * 0.6 + np.random.randn(100) * 1.5
    # add pullback near end
    trend[80:90] = trend[80:90] - np.linspace(0, 5, 10)
    trend[90:]   = trend[90:]   - 5 + np.linspace(0, 3, 10)
    closes = trend.tolist()
    e20    = ema_series(closes, 20)

    ax.plot(x, closes, color='#333', linewidth=1.6, label='Preis')
    ax.plot(x, e20,    color=BLUE,   linewidth=2.8, label='4H EMA20')

    upper3 = [v * 1.03 for v in e20]
    lower1 = [v * 0.985 for v in e20]
    ax.fill_between(x, lower1, upper3, color=GREEN, alpha=0.20, label='✓ Kaufzone (≤3%)')

    # Mark current bar
    ax.scatter(99, closes[99], color=GREEN, s=300, zorder=5, marker='o',
               edgecolor='black', linewidth=2)
    dist = (closes[99] - e20[99]) / e20[99] * 100
    ax.annotate(f'PULLBACK!\nPreis nur +{dist:.1f}% von EMA20\n→ Entry-Zone',
                xy=(99, closes[99]),
                xytext=(75, closes[99]*1.08),
                fontsize=13, color=GREEN, weight='bold', ha='center',
                arrowprops=dict(arrowstyle='->', color=GREEN, lw=2.5))

    # Show "too high" zone
    ax.annotate('zu weit weg\nvon EMA20',
                xy=(50, closes[50]),
                xytext=(50, closes[50]+8),
                fontsize=11, color=RED, weight='bold', ha='center',
                arrowprops=dict(arrowstyle='->', color=RED, lw=1.5))

    ax.set_title("Beispiel: 4H-Chart — Pullback zur EMA20 nach Aufwärtsbewegung",
                 fontsize=11, pad=10)
    ax.set_xlabel("4H-Kerzen")
    ax.set_ylabel("Preis")
    ax.legend(loc='upper left', fontsize=10, framealpha=0.9)

    add_explanation(fig,
        "WARUM:  EMA20 ist die natürliche Support-Linie im Aufwärtstrend.\n"
        "Pullbacks zur EMA20 = günstige Einstiege in laufenden Trends.  Wir kaufen NIE Tops — wir kaufen Dips.")
    save(fig, 6, "4H_EMA20_Pullback")

# ══════════════════════════════════════════════════════════════════════════════
# F7 — VCP KOMPRESSION
# ══════════════════════════════════════════════════════════════════════════════
def make_f7():
    fig, ax = setup_figure(7, "VCP Kompression",
                           "Mind. 2 von 3: Range schrumpft, Volumen trocknet aus, ATR sinkt")
    # synthetic VCP pattern
    x = np.arange(40)
    base = 100
    np.random.seed(11)
    # decreasing ranges
    ranges = np.array([6,5.5,5,4.8,4.5,4.2,4,3.8,3.5,3.2,3,2.8,2.5,2.3,2,1.8,1.7,1.5,1.4,1.3,
                       1.2,1.1,1.0,1.0,0.9,0.9,0.8,0.8,0.7,0.7,0.7,0.7,0.7,0.6,0.6,0.6,0.6,0.5,0.5,0.5])
    closes = [base + np.random.uniform(-r/2, r/2) for r in ranges]
    highs  = [c + r/2 for c, r in zip(closes, ranges)]
    lows   = [c - r/2 for c, r in zip(closes, ranges)]
    vols   = np.linspace(100, 25, 40) + np.random.randn(40) * 5

    # Plot range bars
    for i in range(len(closes)):
        ax.plot([i, i], [lows[i], highs[i]], color='#333', linewidth=1.5)
        col = GREEN if closes[i] > (highs[i]+lows[i])/2 else RED
        ax.scatter(i, closes[i], color=col, s=20, zorder=3)

    ax.set_xlim(-2, 42)
    ymin, ymax = min(lows)-2, max(highs)+2
    ax.set_ylim(ymin, ymax)

    # Volume as bottom bars
    vol_scale = (ymax - ymin) * 0.20 / max(vols)
    for i, v in enumerate(vols):
        ax.add_patch(Rectangle((i-0.35, ymin), 0.7, v * vol_scale,
                               color='#999', alpha=0.5))

    # Arrow showing compression
    ax.annotate('', xy=(38, ymin+2), xytext=(2, ymin+2),
                arrowprops=dict(arrowstyle='->', color=BLUE, lw=3))
    ax.text(20, ymin+1, 'Range schrumpft  +  Volumen trocknet aus  =  VCP ✓',
            ha='center', fontsize=12, color=BLUE, weight='bold')

    ax.annotate('große Kerzen +\nhohes Volumen',
                xy=(3, highs[3]), xytext=(3, highs[3]+1.5),
                fontsize=10, color=RED, weight='bold', ha='center')
    ax.annotate('kleine Kerzen +\nniedriges Volumen\n→ KAUFBEREIT',
                xy=(36, highs[36]), xytext=(36, highs[36]+1.5),
                fontsize=10, color=GREEN, weight='bold', ha='center')

    ax.set_title("VCP = Volatility Contraction Pattern (Mark Minervini)",
                 fontsize=11, pad=10)
    ax.set_xlabel("4H-Kerzen")
    ax.set_ylabel("Preis")
    ax.grid(alpha=0.15)

    add_explanation(fig,
        "WARUM:  Schrumpfende Range + sinkendes Volumen = Markt holt Atem vor dem nächsten Move.\n"
        "Klassisches Profi-Signal vor explosiven Ausbrüchen. Wir kaufen GENAU vor dem Sprung.")
    save(fig, 7, "VCP_Kompression")

# ══════════════════════════════════════════════════════════════════════════════
# F8 — BULLISCHE BESTÄTIGUNG
# ══════════════════════════════════════════════════════════════════════════════
def make_f8():
    fig, ax = setup_figure(8, "Bullische Bestätigung",
                           "Letzte 4H-Kerze grün ODER Bullish Engulfing (kein fallendes Messer)")

    # Two side-by-side mini-examples
    ax.set_xticks([])
    ax.set_yticks([])
    ax.set_xlim(0, 100)
    ax.set_ylim(0, 100)
    ax.spines['left'].set_visible(False)
    ax.spines['bottom'].set_visible(False)
    ax.grid(False)

    # Left: GREEN candle
    def draw_candle(cx, cy, op, cl, hi, lo, color):
        ax.plot([cx, cx], [lo, hi], color='#333', linewidth=2)
        h = abs(cl - op)
        ax.add_patch(Rectangle((cx-2.5, min(op, cl)), 5, h, color=color, ec='black', lw=1.5))

    # Example 1: Simple green candle
    ax.text(25, 90, '✓ GRÜNE KERZE', ha='center', fontsize=16, color=GREEN, weight='bold')
    # Previous red candle
    draw_candle(15, 65, 70, 60, 72, 58, RED)
    # Current green candle (confirmation)
    draw_candle(25, 65, 60, 75, 78, 58, GREEN)
    # Next bar (start)
    draw_candle(35, 65, 75, 75, 75, 75, GREEN)
    ax.annotate('Kauf bei dieser\ngrünen Kerze',
                xy=(25, 75), xytext=(35, 88),
                fontsize=11, color=GREEN, weight='bold', ha='center',
                arrowprops=dict(arrowstyle='->', color=GREEN, lw=2))

    # Divider
    ax.plot([50, 50], [10, 95], color='#888', linewidth=1, linestyle='--')

    # Example 2: Bullish Engulfing
    ax.text(75, 90, '✓ BULLISH ENGULFING', ha='center', fontsize=16, color=GREEN, weight='bold')
    draw_candle(65, 50, 56, 48, 58, 46, RED)
    draw_candle(75, 50, 47, 60, 62, 45, GREEN)
    draw_candle(85, 50, 60, 60, 60, 60, GREEN)
    ax.annotate('grüne Kerze\numschließt rote',
                xy=(75, 60), xytext=(85, 80),
                fontsize=11, color=GREEN, weight='bold', ha='center',
                arrowprops=dict(arrowstyle='->', color=GREEN, lw=2))

    # Bad example at bottom
    ax.add_patch(Rectangle((5, 5), 90, 18, color=PASTEL_RED, alpha=0.4))
    ax.text(50, 20, '✗ NIE bei roter Kerze kaufen — das ist ein fallendes Messer!',
            ha='center', fontsize=13, color=RED, weight='bold')
    # Red candle example
    draw_candle(15, 12, 16, 8, 17, 7, RED)
    ax.text(15, 4, 'NICHT KAUFEN', ha='center', fontsize=9, color=RED, weight='bold')

    ax.set_title("Zwei gültige Bestätigungs-Muster", fontsize=11, pad=10)

    add_explanation(fig,
        "WARUM:  Wenn die Kerze noch fällt = Verkäufer kontrollieren. Wir warten bis Käufer übernehmen.\n"
        "Grüne Kerze ODER Bullish Engulfing = Pullback hat gedreht. Jetzt ist Entry sicher.")
    save(fig, 8, "Bullische_Bestaetigung")

# ── Main ─────────────────────────────────────────────────────────────────────
def main():
    print("Generiere 8 Filter-Erklärungs-Charts...")
    print(f"Ausgabe-Ordner: {OUT_DIR}\n")
    make_f1()
    make_f2()
    make_f3()
    make_f4()
    make_f5()
    make_f6()
    make_f7()
    make_f8()
    print(f"\nFertig! Alle 8 Bilder liegen in: {OUT_DIR}")
    print("  -> Doppelklick oeffnet sie in der Foto-App, dann Drucken (A4 Querformat).")

if __name__ == "__main__":
    main()
