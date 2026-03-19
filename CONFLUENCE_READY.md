# RF Scalp v1.0 — Technical Specification & Operations Wiki

**Bot Name:** RF Scalp v1.0
**Instrument:** XAU/USD (Gold)
**Exchange:** OANDA (practice & live)
**Deployment:** Railway (PaaS)
**Signal Timeframe:** M5 (5-minute candles)
**Cycle Interval:** Every 5 minutes
**Status:** Demo mode (switch `demo_mode: false` for live)

---

## 1. Purpose & Scope

RF Scalp v1.0 is a fully automated 5-minute scalping bot for XAU/USD (Gold).
It was forked from RF v2.4 (CPR breakout) and replaces the signal engine with a
three-layer scalping approach: EMA crossover, Opening Range Breakout, and CPR bias.
All infrastructure — Railway deployment, OANDA order execution, Telegram alerts,
news filtering, database, and reporting — is inherited from RF v2.4 without change.

This document covers strategy logic, signal engine internals, configuration, deployment,
operations, and troubleshooting.

---

## 2. Architecture Overview

```
scheduler.py  (APScheduler — every 5 min)
      |
      v
bot.py  run_bot_cycle()
      |
      +---> session check (London / US / dead zone)
      +---> news_filter.py (economic calendar)
      +---> signals.py  SignalEngine.analyze()   <--- CHANGED from v2.4
      |         |
      |         +-- Layer 1: EMA 9/21 crossover on M5
      |         +-- Layer 2: ORB session range breakout
      |         +-- Layer 3: CPR daily pivot bias
      |
      +---> risk checks (caps, cooldown, spread, margin)
      +---> oanda_trader.py (place order)
      +---> database.py (record trade/signal)
      +---> telegram_templates.py (send alert)
```

---

## 3. Signal Engine — signals.py

### 3.1 Data Inputs

| Data | Source | Granularity | Count | Cache |
|---|---|---|---|---|
| M5 candles (closes/highs/lows) | OANDA REST API | M5 | 40 candles | None (live each cycle) |
| M15 candles (ORB detection) | OANDA REST API | M15 | 12 candles + timestamps | `orb_cache.json` per session/day |
| Daily candles (CPR) | OANDA REST API | D | 3 candles | `cpr_cache.json` per SGT day |

### 3.2 Layer 1 — EMA 9/21 Crossover

**Implementation:**
- EMA series computed on completed M5 candles only (live candle excluded)
- Uses exponential smoothing formula: `EMA = price × k + prev_EMA × (1 − k)` where `k = 2 / (period + 1)`
- `ema_fast_series` = EMA9 on `m5_closes[:-1]`
- `ema_slow_series` = EMA21 on `m5_closes[:-1]`
- Fresh cross detected by comparing `[-1]` and `[-2]` values of both series

**Scoring logic:**

```python
fresh_bull = (ema_fast_now > ema_slow_now) and (ema_fast_prev <= ema_slow_prev)
fresh_bear = (ema_fast_now < ema_slow_now) and (ema_fast_prev >= ema_slow_prev)
```

| Condition | Direction | Score |
|---|---|---|
| Fresh bull cross | BUY | +3 |
| Fresh bear cross | SELL | +3 |
| EMA9 > EMA21 (aligned, no cross) | BUY | +1 |
| EMA9 < EMA21 (aligned, no cross) | SELL | +1 |
| EMA9 == EMA21 | NONE | 0 — exit |

**Why EMA over SMA:** EMA reacts faster to recent price action, making it
better suited for 5-minute scalping on a volatile asset like gold.

### 3.3 Layer 2 — Opening Range Breakout (ORB)

**ORB definition:**
The ORB is the high and low of the first completed M15 candle at or after the session open.

| Session | Open (SGT) | Open (GMT) | ORB candle |
|---|---|---|---|
| London | 16:00 | 08:00 | First completed M15 candle ≥ 16:00 SGT |
| US | 21:00 | 13:00 | First completed M15 candle ≥ 21:00 SGT |

**Cache:** `data/orb_cache.json` — key format: `YYYY-MM-DD_London` / `YYYY-MM-DD_US`

**ORB detection flow:**
1. Check `orb_cache.json` — return cached values if `formed: true`
2. If not cached: check `minutes_since_open >= 15` (M15 candle needs time to complete)
3. Fetch last 12 M15 candles with timestamps
4. Iterate candles — find first with `candle_time >= session_open_utc`
5. Record that candle's high/low as ORB, save to cache

**Scoring logic:**

```python
if direction == "BUY"  and current_close > orb_high:  score += 2
if direction == "SELL" and current_close < orb_low:   score += 2
```

If ORB has not formed yet: scores 0, no block.

### 3.4 Layer 3 — CPR Pivot Bias

**CPR levels** (calculated from previous day OANDA candle):

```
Pivot = (PDH + PDL + PDC) / 3
BC    = (PDH + PDL) / 2
TC    = (Pivot - BC) + Pivot
R1    = (2 × Pivot) − PDL
S1    = (2 × Pivot) − PDH
```

**Only the pivot is used for scoring in RF Scalp v1.0.**
TC, BC, R1, S1, R2, S2 are calculated and cached but not used for signal decisions.

**Scoring logic:**

```python
if direction == "BUY"  and current_close > pivot:  score += 1
if direction == "SELL" and current_close < pivot:  score += 1
```

**Cache:** `data/cpr_cache.json` — validated on load, re-fetched if stale or invalid.

### 3.5 Exhaustion Penalty

Prevents chasing overextended moves on fast M5 timeframes.

```python
ema_mid  = (ema_fast_now + ema_slow_now) / 2
stretch  = abs(current_close - ema_mid) / atr_val
if stretch > exhaustion_atr_mult (default 2.0):
    score = max(score - 1, 0)
```

Set `exhaustion_atr_mult: 0` in settings to disable.

### 3.6 Score-to-Decision Flow

```
score >= 5  →  $100 full position
score >= 3  →  $66  partial position
score < 4   →  No trade (below threshold)

Mandatory blockers applied after scoring:
  R:R < 2.0             →  BLOCKED
  Spread > session limit →  BLOCKED
  News within ±30 min   →  BLOCKED
  Daily cap reached     →  BLOCKED
  Session cap reached   →  BLOCKED
  Active cooldown       →  BLOCKED
  Friday cutoff         →  BLOCKED
```

---

## 4. SL / TP Logic

### 4.1 Stop Loss

Default mode: `pct_based`

```
SL_USD = entry_price × sl_pct
       = entry_price × 0.0015    (0.15%)
```

At gold $2,400: SL ≈ $3.60

Fallback priority:
1. `pct_based` (default) — uses `sl_pct` from settings
2. `fixed_usd` — uses `fixed_sl_usd` (default $5.00)
3. `atr_based` — `ATR(14) × atr_sl_multiplier (0.3)`, clamped $2–$8

### 4.2 Take Profit

Default mode: `rr_multiple`

```
TP_USD = SL_USD × rr_ratio
       = SL_USD × 2.5
```

At gold $2,400: TP ≈ $9.00 (0.375% of entry)

Mandatory guard: `TP_USD / SL_USD >= 2.0` — trade is blocked if not met.

### 4.3 Comparison to RF v2.4

| Parameter | RF v2.4 | RF Scalp v1.0 | Ratio |
|---|---|---|---|
| SL % | 0.25% | 0.15% | 0.6× tighter |
| TP % | 0.75% | ~0.375% | 0.5× tighter |
| RR | 3.0 | 2.5 | Slightly lower |
| Trade duration | Minutes to hours | Seconds to minutes | Faster exits |

---

## 5. Session & Risk Controls

### 5.1 Session Windows

| Window | SGT | GMT | Threshold | Cap |
|---|---|---|---|---|
| London | 16:00 – 20:59 | 08:00 – 13:00 | Score ≥ 3 | 4 trades |
| US | 21:00 – 00:59 | 13:00 – 17:00 EDT | Score ≥ 3 | 4 trades |
| Dead Zone | 01:00 – 15:59 | — | N/A | 0 |

Session thresholds are configurable independently via `session_thresholds` in settings.

### 5.2 Daily Limits

| Limit | Value | Behaviour |
|---|---|---|
| Max trades/day | 8 | No new trades after 8 filled |
| Max losses/day | 3 | No new trades after 3 losses |
| Max losses/session | 2 | No new trades in current session |
| Loss cooldown | 30 min | Pause after consecutive loss streak |
| Friday cutoff | 23:00 SGT | No new entries on Friday night |

### 5.3 Spread Control

| Session | Spread Limit (pips) | Behaviour |
|---|---|---|
| London | 130 | Skip cycle, send Telegram alert |
| US | 130 | Skip cycle, send Telegram alert |
| Global max | 150 | Hard fallback ceiling |

### 5.4 News Filter

- Blocks ±30 minutes around **high-impact** news events
- Medium-impact events apply a score penalty of −1
- Calendar sourced from TradingEconomics API (`TRADINGECONOMICS_API_KEY`)
- Refreshed every 60 minutes, retry after 15 minutes on failure
- Lookahead window: 120 minutes ahead

---

## 6. Order Execution

All order execution is handled by `oanda_trader.py` (unchanged from RF v2.4).

- Instrument: `XAU_USD`
- Order type: Market order with attached SL and TP
- Units calculated from: `position_usd / sl_usd` (risk-per-unit sizing)
- Margin safety: `margin_safety_factor = 0.6` (never risk more than 60% of free margin)
- Auto-scale on margin reject: reduces position size and retries

---

## 7. Database & State Files

### SQLite Database (`data/rf_scalp.db`)
- Signals table: every cycle's score, direction, details, levels
- Trades table: all filled orders with entry, SL, TP, outcome
- Purge policy: 90-day rolling window, vacuum every Sunday

### JSON State Files

| File | Purpose |
|---|---|
| `data/cpr_cache.json` | Daily CPR levels, refreshed each SGT day |
| `data/orb_cache.json` | ORB high/low per session per SGT day (new in v1.0) |
| `data/signal_cache.json` | Dedup: prevents duplicate Telegram signal alerts |
| `data/ops_state.json` | Session state, cooldown, caps — persists across cycles |
| `data/runtime_state.json` | Cycle health — last started, last status, OANDA failures |
| `data/trade_history.json` | Rolling 90-day trade log |

---

## 8. Telegram Reporting

### 8.1 Real-time Alerts

| Message | When |
|---|---|
| Startup | Bot process starts |
| Session Open | London (16:00 SGT) / US (21:00 SGT) each day |
| Scalp Signal Update | Each 5-min cycle with active session |
| Trade Opened | New order filled |
| Trade Closed | TP or SL hit |
| News Block | High-impact event pausing trades |
| News Penalty | Medium-impact event reducing score |
| Spread Skip | Spread above limit |
| Daily Cap | Max daily trades or losses hit |
| Session Cap | Max per-session trades or losses hit |
| Cooldown | Loss streak cooldown activated |
| Friday Cutoff | No new trades alert |
| Margin Adjusted | Position scaled down due to margin |

### 8.2 Scheduled Reports

| Report | Schedule (SGT) |
|---|---|
| Daily Report | Mon–Fri 15:30 (30 min before London open) |
| Weekly Report | Every Monday 08:15 |
| Monthly Report | First Monday of month 08:00 |

---

## 9. Railway Deployment

### 9.1 Setup Steps

```
1. Create new GitHub repo, push RF Scalp v1.0 folder contents
2. Railway → New Project → Deploy from GitHub Repo
3. Select repo → Railway auto-detects Procfile
4. Add environment variables (Railway → Variables tab)
5. Deploy triggers automatically on push to main branch
```

### 9.2 Environment Variables

```
OANDA_API_KEY              = <your OANDA API key>
OANDA_ACCOUNT_ID           = <your OANDA account ID>
TELEGRAM_BOT_TOKEN         = <your Telegram bot token>
TELEGRAM_CHAT_ID           = <your Telegram chat ID>
TRADINGECONOMICS_API_KEY   = <TradingEconomics API key>
PORT                       = 8080  (optional, Railway sets this)
```

### 9.3 Health Monitoring

```
GET /health   →  {"status": "ok", "scheduler_running": true, ...}
GET /metrics  →  Prometheus-style plain text counters
```

Set Railway's health check URL to `/health` for automatic restart on failure.

### 9.4 Procfile

```
web: python scheduler.py
```

---

## 10. Going Live Checklist

Before switching `demo_mode` to `false`:

- [ ] Run minimum 2 weeks in demo mode
- [ ] Review weekly reports — win rate > 40%, average R:R > 1.8
- [ ] Confirm OANDA live credentials differ from practice credentials
- [ ] Update `OANDA_API_KEY` and `OANDA_ACCOUNT_ID` in Railway Variables
- [ ] Set `demo_mode: false` in `settings.json` and redeploy
- [ ] Monitor first 3 live trades manually via Telegram
- [ ] Confirm SL/TP prices match expectations at session open

---

## 11. Differences from RF v2.4

| Component | RF v2.4 | RF Scalp v1.0 |
|---|---|---|
| `signals.py` | CPR breakout engine | EMA + ORB + CPR bias scalp engine |
| Signal timeframe | M15 | M5 |
| Primary signal | Price breaks CPR/PDH/R1 | EMA9 crosses EMA21 |
| Secondary signal | SMA20/SMA50 alignment | ORB session breakout |
| Tertiary filter | CPR width (narrow = better) | CPR pivot bias (above/below) |
| SMA usage | SMA20 + SMA50 for trend | Not used |
| ORB logic | Not present | New — `_get_orb()` in signals.py |
| `orb_cache.json` | Not present | New state file |
| `sl_pct` | 0.0025 (0.25%) | 0.0015 (0.15%) |
| `tp_pct` | 0.0075 (0.75%) | 0.0035 (0.35%) |
| `rr_ratio` | 3.0 | 2.5 |
| `sl_max_usd` | 20.0 | 8.0 |
| `sl_min_usd` | 4.0 | 2.0 |
| `atr_sl_multiplier` | 0.5 | 0.3 |
| `version.py` | CPR Gold Bot v2.4 | RF Scalp v1.0 |
| `bot.py` docstring | CPR references | Scalp references |
| `telegram_templates.py` | CPR Signal Update | Scalp Signal Update |
| Session windows | Identical | Identical |
| Risk limits | Identical | Identical |
| Infrastructure | — | Identical (no changes) |

---

## 12. Troubleshooting

### Bot not trading during session hours

1. Check `data/ops_state.json` — look for `outside_session` or `daily_cap_reached`
2. Check Railway logs for `SKIPPED` cycle status
3. Confirm SGT time is correct in Railway region settings

### ORB not forming

1. Check `data/orb_cache.json` — confirm entry exists for today's session
2. ORB requires 15+ minutes after session open before it can be detected
3. If OANDA M15 candle data is missing, ORB will score 0 (not block trade)

### Low signal score (score < 4 every cycle)

1. EMA9 and EMA21 may be flat/converging — wait for clear trend on M5
2. ORB may not have formed yet (first 15 min of session)
3. CPR bias may be against direction — check CPR pivot vs current price
4. Check `exhaustion_atr_mult` — may be reducing score if market is extended

### CPR cache invalid warning in logs

The bot will automatically re-fetch CPR levels from OANDA and rebuild the cache.
This is a self-healing condition, no action required.

### R:R below 2.0 blocking trades

This can happen if `sl_pct` and `tp_pct` values produce an R:R < 2.0 at current price.
Verify: `tp_pct / sl_pct >= 2.0` must be true.
Default values (0.0035 / 0.0015 = 2.33) satisfy this condition.

---

*RF Scalp v1.0 — Technical Specification*
*Forked from RF v2.4 · EMA 9/21 + ORB + CPR bias scalping engine*
*Deployed on Railway · OANDA · Telegram*
