# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Telegram-driven crypto trading bot for **Binance Spot** trading. Long-only, no leverage,
halal-conscious coin selection. Deployed to **Railway** as a worker process.

The bot scans 31 USDT pairs on Binance with an 8-filter cascade (Daily + 4H + BTC relative
strength), sends setup alerts to Telegram with inline **JA/NEIN** buttons, and optionally
auto-executes Market Buy + OCO orders via Binance API. Open positions are monitored with a
**TP1 partial-exit + 4H EMA20 trailing stop**.

## Architecture

Two files do almost everything — keep them as the boundary:

- **`strategy.py`** — All trading logic. Pure functions over OHLCV dicts, no Telegram, no
  state. Public API: `scan_all_symbols(symbols, equity, config)` returns
  `(setups, watch, errors)`. Lower-level: `check_new_setup(daily, h4, btc_daily, is_large_cap)`
  runs the 8 filters and returns a result dict. Exports `LARGE_CAPS` set
  (`ETH/BNB/XRP/LTC`) used to pick the right extension limit per coin.
  Also contains Binance helpers (`get_candles`, `get_binance_usdt_balance`,
  `execute_trade` with OCO) and indicator primitives (`calc_ema`, `calc_atr`, `calc_rsi`,
  `calc_adx`).
- **`bot_server.py`** — The runtime. Holds `SYMBOLS` list, polls Telegram via
  `getUpdates`, dispatches `/`-commands, runs scheduled scans (09:00 / 16:00 / 16:45 CEST
  + hourly), persists state to disk, generates 4H candlestick charts via `mplfinance`,
  and handles inline-button callbacks. Imports `calc_ema` from `strategy` to compute the
  trailing stop in `monitor_trade`.

## The 8-filter cascade (Balanced Version)

In `check_new_setup`:

| # | Filter | Rule |
|---|---|---|
| F1 | Daily Golden Cross | `EMA50 > EMA200` AND `EMA200` rising over 20 days |
| F2 | Not extended | Large Caps ≤ `extension_large` (12%), Altcoins ≤ `extension_alt` (15%) over Daily EMA50 |
| F3 | 30-day-high breakout | Today's daily close > max of previous 30 daily highs |
| F4 | Coin/BTC ratio rising | RS-EMA20 of `Coin/BTC` close ratio is higher than 10 days ago |
| F5 | RS resilience (graduated) | `resilience_tiers` list: for each BTC drop ≥ tier threshold in last 30 days, coin's same-day drop must be within the tier's limit |
| F6 | 4H EMA20 pullback | Price within `-1.5%` … `+prox_pct` (3%) of 4H EMA20 |
| F7 | VCP compression (2-of-3) | At least `vcp_min_hits` (default 2) of: range shrinking, volume drying, ATR declining |
| F8 | Bullish confirmation | Last 4H candle is green **OR** bullish engulfing pattern |

**Bear-market protection is implicit**: F1 fails → 0 setups, no special "Plan B" branch.

## TP1 partial exit + trailing stop (`monitor_trade`)

When a user runs `/trade COIN entry sl tp`, the bot starts a `monitor_trade` thread that:

1. Computes `tp1 = entry + 2 × (entry - sl)` — the externally-supplied `tp` arg is
   ignored for exit logic, kept only in the alert payload for context.
2. Sends a status message every 4 hours.
3. On `price >= tp1` (and not yet partially exited):
   - Sends "🎯 TP1 ERREICHT" with manual instruction to close 50%.
   - Sets `partial_taken = True`, `trailing_stop = entry` (breakeven protection).
4. While `partial_taken`, fetches 4H candles each iteration, computes `calc_ema(closes, 20)`,
   and updates `trailing_stop` (only upward — never lowers).
5. On `price <= trailing_stop` → exit (either initial SL or trailing exit, with different
   message text).

**This is notification-only.** The bot does **not** auto-execute partial sells on Binance
even when `AUTO_TRADE=true` — OCO orders placed by `execute_trade` are immutable. The user
manually closes 50% on Binance when TP1 fires.

## Setup flow & button contract

Setup alerts go out via `send_photo_with_buttons` which attaches inline buttons with
callback_data `trade_yes_{SYMBOL}` / `trade_no_{SYMBOL}`. Pressing JA fires
`_fire_trade()` which calls `strategy.execute_trade()` (Market Buy + OCO at fixed TP, no
trailing on Binance side). Pending setups are persisted to `pending_setups.json` so a
Railway restart doesn't lose them. The `CONFIRM_WINDOW_SEC` (default 300s) gates how long
a button stays valid.

There is **also** a legacy `accept_{SYMBOL}` / `reject_{SYMBOL}` callback path used only
by `/testsetup` — both are handled in `handle_callback_query`.

## Persistent state (gitignored, written at runtime)

- `alarms.json` — active price alarms, restored on startup
- `pending_setups.json` — setups waiting for JA/NEIN button click
- `accepted_trades.txt` — append-only log of clicked-JA setups
- `signals.log` — rotating log of every Telegram message sent (5MB × 5 backups)
- `alarm_inbox.json` — external scripts (e.g. `auto_scan.py`) drop new alarms here

## Configuration

Env vars (Railway) take precedence over local config files:

| Env var | Local fallback |
|---|---|
| `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID` | `telegram_config.json` |
| `BINANCE_API_KEY`, `BINANCE_API_SECRET` | (none — required for live trading) |
| `AUTO_TRADE` (`true`/`false`) | default `false` (button confirmation) |
| `POSITION_SIZE` | default `1000` (only used when Binance balance call fails) |
| `COINGLASS_API_KEY` | (optional — for `/zonen` liquidation chart) |

## Common commands

```powershell
# Run locally (requires telegram_config.json)
cd C:\Users\Gianni\tradingbot
python bot_server.py

# Verify all 31 coins are tradable on Binance
python -c "from strategy import get_candles; import bot_server; [get_candles(s,'1d',5) for s in bot_server.SYMBOLS]"

# One-off scan test (no Telegram needed)
python -c "from strategy import scan_all_symbols; print(scan_all_symbols(['ETHUSDT','SOLUSDT'], 1000))"

# Test a single coin through all 8 filters
python -c "from strategy import check_new_setup, get_candles, LARGE_CAPS; d=get_candles('ETHUSDT','1d',300); h=get_candles('ETHUSDT','4h',100); b=get_candles('BTCUSDT','1d',300); print(check_new_setup(d,h,b,is_large_cap='ETHUSDT' in LARGE_CAPS))"

# Backtest the OLD 7-filter strategy on 6 months of data
python backtest.py

# Deploy to Railway (PowerShell blocks railway.ps1, use cmd)
cmd /c "railway up"
```

## Working with the strategy

- **Tuning filter strictness** lives in `DEFAULT_CONFIG` at the top of `strategy.py`:
  `prox_pct`, `extension_large`/`extension_alt`, `breakout_days`, `vcp_lookback`,
  `vcp_min_hits`, `resilience_tiers`, `confirm_candle`, `trailing_ema`, etc. All filter
  results are returned in the setup dict, so loosening one in the config immediately
  flows into the Telegram captions and the `/strategie` command output.
- **Adding a coin to `LARGE_CAPS`** switches it to the stricter `extension_large` limit
  — important for high-cap coins that don't typically run as far above the EMA50.
- **BTC daily candles are fetched once per scan** (in `scan_all_symbols`) and reused for
  RS calculation across all coins — don't move that fetch inside the per-coin loop.
- **`resilience_tiers`** is a sorted list of `(btc_drop_pct, coin_max_drop_pct)` tuples.
  The loop in F5 finds the strictest applicable tier per historical BTC-down day and
  rejects the coin if its drop exceeded the tier's allowance.
- **Position sizing** is in `execute_trade`: `qty = (equity * 0.01) / (entry - sl)`,
  then floored to `LOT_SIZE.stepSize` from `/api/v3/exchangeInfo`. Risk per trade is
  hardcoded to 1% — change in one place only.
- The `candles_15m` field on a setup dict actually carries **4H candles** (legacy name,
  reused for the chart). Don't rename without updating `generate_chart` in `bot_server.py`.

## Telegram messages — language & idioms

Bot text is **German**. Earlier versions had Albanian remnants which were removed —
when adding new messages, keep German and avoid mixing languages. HTML parse mode is
the default in `send()`; escape `<` and `>` as `&lt;` `&gt;` in user-supplied content.

## Deployment

GitHub repo is `GianniTGT/tradingbot` (default branch `master`). Railway is connected
to GitHub for auto-deploy on push. `Procfile` declares a `worker` (no HTTP port).
`railway.toml` sets the start command and restart policy. `requirements.txt` is the
single source of Python dependencies — Nixpacks handles the rest.

## Out-of-scope / legacy

- `scan_stocks()` in `bot_server.py` — unused since the Plan A/B switch was removed.
  Kept around in case stock scanning gets revived. Requires `yfinance` which is **not**
  in `requirements.txt`.
- `backtest.py` — still references the OLD 7-filter strategy (EMA Sniper). Not updated
  to the current 8-filter Balanced cascade. Useful as a template if you want to backtest
  the new one.
- `auto_scan.py` — standalone scan script for Windows Task Scheduler; superseded by the
  Railway-hosted `bot_server.py` scheduled scans but still works.
- `*.pine` files — TradingView Pine Script versions of earlier strategies, for chart
  visualization only, not executed by the bot.
