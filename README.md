# RF Scalp v1.0 — XAU/USD 5-Minute Scalping Bot

> **Deployed on Railway · OANDA API · Telegram Alerts**

RF Scalp v1.0 is an automated gold scalping bot for XAU/USD built on top of the RF v2.4 infrastructure.
It replaces the CPR breakout strategy with a three-layer scalping signal engine:
**EMA 9/21 crossover** as the primary trigger, **Opening Range Breakout (ORB)** as momentum confirmation,
and **CPR pivot bias** as a directional filter.

---

## Table of Contents

1. [Strategy Overview](#strategy-overview)
2. [Signal Scoring](#signal-scoring)
3. [Trading Sessions](#trading-sessions)
4. [Risk Management](#risk-management)
5. [Settings Reference](#settings-reference)
6. [Railway Deployment](#railway-deployment)
7. [Environment Variables](#environment-variables)
8. [File Structure](#file-structure)
9. [Telegram Alerts](#telegram-alerts)
10. [Differences from RF v2.4](#differences-from-rf-v24)

---

## Strategy Overview

RF Scalp v1.0 operates on **M5 (5-minute) candles** and runs a 5-minute cycle.
Every cycle the signal engine evaluates three independent components and scores them.
A trade is only placed when the combined score reaches the configured threshold (default: 4/6).

### The Three Layers

**Layer 1 — EMA 9/21 Crossover (Primary Signal)**
- Uses Exponential Moving Averages on completed M5 candles (excludes the live candle)
- A **fresh cross** (EMA9 just crossed EMA21 in the last two candles) scores +3
- An **aligned but no fresh cross** (EMA9 above/below EMA21) scores +1
- Direction is set entirely by this layer — BUY when EMA9 above EMA21, SELL when below

**Layer 2 — Opening Range Breakout (Momentum Filter)**
- The ORB is defined as the first completed M15 candle at or after session open
- London ORB: first M15 candle from 16:00 SGT (08:00 GMT)
- US ORB: first M15 candle from 21:00 SGT (13:00 GMT)
- Price breaking above ORB high (BUY) or below ORB low (SELL) scores +2
- ORB is cached per session per SGT day in `orb_cache.json`
- If ORB has not yet formed (within 15 minutes of session open), this layer scores 0

**Layer 3 — CPR Pivot Bias (Directional Filter)**
- Daily CPR (Central Pivot Range) levels are fetched from OANDA and cached per SGT day
- Price above CPR pivot on a BUY signal scores +1
- Price below CPR pivot on a SELL signal scores +1
- CPR is a bias filter only — it cannot block a trade, only reduce the score

---

## Signal Scoring

### Scoring Table

| Component | Condition | Score |
|---|---|---|
| EMA Cross | Fresh EMA9 cross above/below EMA21 | +3 |
| EMA Cross | EMA9/21 aligned but no fresh cross | +1 |
| ORB | Price breaks session ORB high (BUY) / low (SELL) | +2 |
| CPR Bias | Price on correct side of daily pivot | +1 |
| Exhaustion | Price stretched >2× ATR from EMA midpoint | −1 |

**Maximum score: 6**

### Score-to-Action Table

| Score | Action | Position Size |
|---|---|---|
| 5 – 6 | Trade — Full size | $100 |
| 3 – 4 | Trade — Partial size | $66 |
| < 4 | No trade — Watch | $0 |

### Mandatory Blockers (trade is skipped regardless of score)

- Score below session threshold (default: 4)
- R:R ratio < 1:2
- Spread above session limit (London: 130 pips, US: 130 pips)
- News event within ±30 minutes
- Daily loss cap reached (3 losses/day)
- Session loss sub-cap reached (2 losses/session)
- Active cooldown period (30 min after loss streak)
- Friday cutoff (23:00 SGT)

---

## Trading Sessions

| Session | SGT Time | GMT Equivalent | Max Trades |
|---|---|---|---|
| London Window | 16:00 – 20:59 | 08:00 – 13:00 | 4 |
| US Window | 21:00 – 00:59 | 13:00 – 17:00 EDT | 4 |
| Dead Zone | 01:00 – 15:59 | — | 0 (no new entries) |

The Asian session is **disabled** — XAU/USD does not produce sufficient momentum for
scalp-quality ORB breakouts or clean EMA crosses during Asian hours.

---

## Risk Management

### Stop Loss
- Mode: `pct_based` (default)
- SL = 0.15% of entry price
- At gold price of ~$2,400: SL ≈ $3.60 per unit
- Clamped: min $2.00, max $8.00 (ATR mode)

### Take Profit
- Mode: `rr_multiple` (default)
- TP = SL × RR ratio (default 2.5)
- Effective TP ≈ 0.35% of entry
- At gold price of ~$2,400: TP ≈ $8.40 per unit

### Comparision: RF v2.4 vs RF Scalp v1.0

| Parameter | RF v2.4 | RF Scalp v1.0 |
|---|---|---|
| SL % | 0.25% | 0.15% |
| TP % | 0.75% | 0.35% |
| RR ratio | 3.0 | 2.5 |
| Signal timeframe | M15 | M5 |
| Strategy | CPR breakout | EMA + ORB + CPR bias |

### Daily Limits (same as RF v2.4)

| Limit | Value |
|---|---|
| Max trades per day | 8 |
| Max losing trades per day | 3 |
| Max losing trades per session | 2 |
| Loss streak cooldown | 30 minutes |
| Max concurrent trades | 1 |

---

## Settings Reference

All settings live in `settings.json`. Key scalp-specific values:

```json
{
  "bot_name":            "RF Scalp v1.0",
  "sl_pct":              0.0015,
  "tp_pct":              0.0035,
  "rr_ratio":            2.5,
  "sl_min_usd":          2.0,
  "sl_max_usd":          8.0,
  "atr_sl_multiplier":   0.3,
  "signal_threshold":    4,
  "cycle_minutes":       5,
  "exhaustion_atr_mult": 2.0,
  "session_thresholds":  { "London": 3, "US": 3 }
}
```

Full settings reference:

| Key | Default | Description |
|---|---|---|
| `bot_name` | RF Scalp v1.0 | Display name in Telegram |
| `demo_mode` | true | true = OANDA practice, false = live |
| `signal_threshold` | 4 | Minimum score to place a trade |
| `sl_pct` | 0.0015 | Stop loss as % of entry (0.15%) |
| `tp_pct` | 0.0035 | Take profit as % of entry (0.35%) |
| `rr_ratio` | 2.5 | TP multiplier when tp_mode = rr_multiple |
| `sl_min_usd` | 2.0 | Minimum SL in USD (ATR mode floor) |
| `sl_max_usd` | 8.0 | Maximum SL in USD (ATR mode ceiling) |
| `atr_sl_multiplier` | 0.3 | ATR multiplier when sl_mode = atr_based |
| `exhaustion_atr_mult` | 2.0 | Stretch penalty threshold (×ATR from EMA midpoint) |
| `cycle_minutes` | 5 | Bot cycle interval in minutes |
| `max_trades_day` | 8 | Max trades per SGT trading day |
| `max_losing_trades_day` | 3 | Max losses before day halt |
| `max_losing_trades_session` | 2 | Max losses per session block |
| `max_trades_london` | 4 | Trade cap for London window |
| `max_trades_us` | 4 | Trade cap for US window |
| `loss_streak_cooldown_min` | 30 | Cooldown minutes after consecutive losses |
| `max_spread_pips` | 150 | Global spread ceiling in pips |
| `spread_limits.London` | 130 | London-specific spread ceiling |
| `spread_limits.US` | 130 | US-specific spread ceiling |
| `news_filter_enabled` | true | Enable economic calendar filter |
| `news_block_before_min` | 30 | Minutes before high-impact news to pause |
| `news_block_after_min` | 30 | Minutes after high-impact news to pause |
| `session_only` | true | Only trade during defined session windows |
| `breakeven_enabled` | false | SL move to breakeven (disabled) |
| `friday_cutoff_hour_sgt` | 23 | Stop new trades after this hour on Friday (SGT) |

---

## Railway Deployment

### First Deploy

1. Push the `RF Scalp v1.0` folder to a new GitHub repository
2. In Railway: **New Project → Deploy from GitHub Repo**
3. Select the repository
4. Add all environment variables (see below)
5. Railway auto-detects `Procfile` and starts the bot

### Procfile

```
web: python scheduler.py
```

### railway.json

```json
{
  "$schema": "https://railway.app/railway.schema.json",
  "build": { "builder": "NIXPACKS" },
  "deploy": { "startCommand": "python scheduler.py", "restartPolicyType": "ON_FAILURE" }
}
```

### Health Check

The bot exposes a health endpoint at `GET /health` (port 8080 by default).
Railway can poll this for uptime monitoring. Returns:

```json
{
  "status": "ok",
  "scheduler_running": true,
  "last_cycle_started": "2025-01-01 16:05:00",
  "last_cycle_status": "COMPLETED",
  "oanda_failures": 0,
  "uptime_s": 3600
}
```

---

## Environment Variables

Set these in Railway → Variables:

| Variable | Required | Description |
|---|---|---|
| `OANDA_API_KEY` | Yes | OANDA API key (practice or live) |
| `OANDA_ACCOUNT_ID` | Yes | OANDA account ID |
| `TELEGRAM_BOT_TOKEN` | Yes | Telegram bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | Yes | Telegram chat/channel ID |
| `TRADINGECONOMICS_API_KEY` | Recommended | Economic calendar API key |
| `PORT` | No | Health server port (default 8080) |

---

## File Structure

```
RF Scalp v1.0/
├── signals.py              ← EMA + ORB + CPR scalp signal engine (CHANGED)
├── bot.py                  ← Main orchestrator (M5 timeframe, updated refs)
├── scheduler.py            ← APScheduler — runs bot cycle every 5 min
├── settings.json           ← All tunable parameters (scalp values)
├── settings.json.example   ← Template copy of settings.json
├── version.py              ← Bot name: RF Scalp | version: 1.0
├── oanda_trader.py         ← OANDA REST API wrapper
├── telegram_templates.py   ← All Telegram message formats (updated labels)
├── telegram_alert.py       ← Telegram send helper
├── news_filter.py          ← Economic calendar news filter
├── calendar_fetcher.py     ← TradingEconomics calendar fetch
├── database.py             ← SQLite trade + signal database
├── reporting.py            ← Daily / weekly / monthly reports
├── reconcile_state.py      ← OANDA vs local state reconciliation
├── config_loader.py        ← Settings + secrets loader
├── state_utils.py          ← JSON state file helpers
├── analyze_trades.py       ← CLI trade analysis tool
├── logging_utils.py        ← Structured logging setup
├── startup_checks.py       ← Env var and config validation
├── Procfile                ← Railway start command
├── railway.json            ← Railway project config
├── requirements.txt        ← Python dependencies
├── README.md               ← This file
└── CONFLUENCE_READY.md     ← Full Confluence wiki page
```

### Key Data Files (auto-created at runtime)

```
data/
├── cpr_cache.json      ← Daily CPR levels (refreshed each SGT day)
├── orb_cache.json      ← ORB high/low per session per day (NEW)
├── orb_cache.json      ← Session Opening Range Breakout levels
├── trade_history.json  ← Rolling 90-day trade log
├── signal_cache.json   ← Signal dedup cache
├── ops_state.json      ← Operational state (session, cooldown, etc.)
└── runtime_state.json  ← Cycle health state
```

---

## Telegram Alerts

RF Scalp v1.0 sends the following Telegram notifications:

| Alert | Trigger |
|---|---|
| Startup | Bot starts / Railway redeploys |
| Session Open | London (16:00 SGT) and US (21:00 SGT) open |
| Scalp Signal Update | Every 5-min cycle during active session |
| Trade Opened | New order placed |
| Trade Closed | TP hit, SL hit, or manually closed |
| News Block | High-impact news event pausing trading |
| Spread Skip | Spread too wide to trade |
| Daily Cap | Max trades or losses for the day reached |
| Session Cap | Max trades or losses for the session reached |
| Cooldown Started | Loss streak cooldown activated |
| Friday Cutoff | No new trades after 23:00 SGT on Friday |
| Daily Report | Mon–Fri at 15:30 SGT |
| Weekly Report | Every Monday at 08:15 SGT |
| Monthly Report | First Monday of each month at 08:00 SGT |

---

## Differences from RF v2.4

| Area | RF v2.4 | RF Scalp v1.0 |
|---|---|---|
| **Strategy** | CPR breakout | EMA 9/21 + ORB + CPR bias |
| **Signal timeframe** | M15 candles | M5 candles |
| **Primary signal** | Price breaks CPR/PDH/R1 | EMA9 crosses EMA21 |
| **Secondary signal** | SMA20/SMA50 alignment | ORB session range breakout |
| **Tertiary filter** | CPR width | CPR pivot bias |
| **SL %** | 0.25% | 0.15% |
| **TP %** | 0.75% | 0.35% |
| **RR ratio** | 3.0 | 2.5 |
| **New cache file** | — | `orb_cache.json` |
| **Bot name** | CPR Gold Bot v2.4 | RF Scalp v1.0 |
| **Sessions / caps** | Identical | Identical |
| **Risk limits** | Identical | Identical |
| **Infrastructure** | Identical | Identical |

---

*RF Scalp v1.0 — Built on RF v2.4 infrastructure · EMA + ORB + CPR scalping engine*
