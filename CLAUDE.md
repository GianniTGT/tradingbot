# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Telegram-driven crypto trading bot for **Binance Spot** trading. Long-only, no leverage,
halal-conscious coin selection. Deployed to **Railway** as a worker process.

The bot scans 31 USDT pairs on Binance with an 8-filter cascade (Daily + 4H + BTC relative
strength), sends setup alerts to Telegram with inline **JA/NEIN** buttons, and optionally
auto-executes Market Buy + OCO orders via Binance API.

## Architecture

Two files do almost everything — keep them as the boundary:

- **`strategy.py`** — All trading logic. Pure functions over OHLCV dicts, no Telegram, no
  state. Public API: `scan_all_symbols(symbols, equity, config)` returns
  `(setups, watch, errors)`. Lower-level: `check_new_setup(daily, h4, btc_daily)` runs the
  8 filters and returns a result dict. Also contains Binance helpers (`get_candles`,
  `get_binance_usdt_balance`, `execute_trade` with OCO).
- **`bot_server.py`** — The runtime. Holds `SYMBOLS` list, polls Telegram via
  `getUpdates`, dispatches `/`-commands, runs scheduled scans (09:00 / 16:00 / 16:45 CEST
  + hourly), persists state to disk, generates 4H candlestick charts via `mplfinance`,
  and handles inline-button callbacks.

The 8-filter cascade (in `check_new_setup`):
F1 Daily Golden Cross · F2 not extended (≤12% over Daily EMA50) · F3 30-day-high
breakout · F4 Coin/BTC ratio rising · F5 RS resilience when BTC drops · F6 4H EMA20
pullback (≤2%) · F7 VCP compression · F8 bullish 4H confirmation candle.
**Bear-market protection is implicit**: F1+F4 fail → 0 setups, no special "Plan B" branch.

## Setup flow & button contract

Setup alerts go out via `send_photo_with_buttons` which attaches inline buttons with
callback_data `trade_yes_{SYMBOL}` / `trade_no_{SYMBOL}`. Pressing JA fires
`_fire_trade()` which calls `strategy.execute_trade()` (Market Buy + OCO). Pending
setups are persisted to `pending_setups.json` so a Railway restart doesn't lose them.
The `CONFIRM_WINDOW_SEC` (default 300s) gates how long a button stays valid.

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
python -c "from strategy import get_candles; [get_candles(s,'1d',5) for s in __import__('bot_server').SYMBOLS]"

# One-off scan test (no Telegram needed)
python -c "from strategy import scan_all_symbols; print(scan_all_symbols(['ETHUSDT','SOLUSDT'], 1000))"

# Backtest the OLD 7-filter strategy on 6 months of data
python backtest.py

# Deploy to Railway (PowerShell blocks railway.ps1, use cmd)
cmd /c "railway up"
```

## Working with the strategy

- **Tuning filter strictness** lives in `DEFAULT_CONFIG` at the top of `strategy.py` —
  `prox_pct`, `extension_max`, `breakout_days`, `vcp_lookback`, `confirm_candle`, etc.
  All filter results are returned in the setup dict, so loosening one in the config
  immediately flows into the Telegram captions and `/strategie` output.
- **BTC daily candles are fetched once per scan** (in `scan_all_symbols`) and reused for
  RS calculation across all coins — don't move that fetch inside the per-coin loop.
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
  to the new 8-filter cascade. Useful as a template if you want to backtest the new one.
- `auto_scan.py` — standalone scan script for Windows Task Scheduler; superseded by the
  Railway-hosted `bot_server.py` scheduled scans but still works.
- `*.pine` files — TradingView Pine Script versions of earlier strategies, for chart
  visualization only, not executed by the bot.
