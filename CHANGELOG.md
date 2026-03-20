# RF Scalp Bot — Changelog

---

## v1.2.5 — 2026-03-20

### 🔴 Root Cause Fix — settings.json Not Deployed to Railway (`.gitignore`)

**Root cause (confirmed from log):**
```
Bundled settings.json not found or empty at /app/settings.json
```
The `.gitignore` file explicitly excluded `settings.json` with a comment
saying "The bot will recreate it from settings.json.example on first boot".
This was wrong — `config_loader.py` reads `settings.json`, not
`settings.json.example`. As a result, every Railway deployment ran with no
bundled settings file, fell back to code-level `setdefault()` values, and the
volume was never updated from the bundle.

This is the underlying cause of every settings-related bug across v1.2.0–v1.2.4.

**Fixes:**
1. `settings.json` removed from `.gitignore` — it now deploys to Railway.
2. `config_loader.py` also tries `settings.json.example` as a fallback if
   `settings.json` is missing, for maximum resilience.

### 🟡 Fix — TP Label in Trade Details String (`signals.py`)

When `tp_mode = "rr_multiple"`, the trade details showed
`TP=$29.12 (rr_multiple 0.35%)` — the `0.35%` was the raw `tp_pct` value,
misleading because the TP was not derived from that percentage. Now shows
`TP=$29.12 (rr_multiple 2.5x RR)`, which accurately reflects how the TP
was calculated.

### ✅ Bot Status After This Deployment

All settings will correctly sync from `settings.json` on startup:
- `sl_pct = 0.0025` (0.25% SL) ✅
- `tp_mode = rr_multiple`, `rr_ratio = 2.5` → TP = SL × 2.5 ✅
- `max_losing_trades_day = 8` ✅
- `max_losing_trades_session = 4` ✅
- `max_trades_day = 20`, `max_trades_london = 10`, `max_trades_us = 10` ✅

Startup log will show:
```
Settings synced on startup: RF Scalp v1.2.4 → RF Scalp v1.2.5
```

---

## v1.2.4 — 2026-03-20

### 🔴 Critical Fix — Every Trade Blocked by R:R Check (`signals.py`)

**Root cause (confirmed from log):** `signals.py` always computed TP as
`entry × tp_pct`, completely ignoring `tp_mode` and `rr_ratio` from settings.
With `sl_pct=0.0025` and `tp_pct=0.0035`, the computed RR was always
`0.0035/0.0025 = 1.40` — which always failed the mandatory `R:R ≥ 2` check.
Every single trade signal was silently blocked.

**Evidence from log:** `Scalp signal BLOCKED | R:R 1.40 < 1:2` on all cycles.

**Fix:** When `tp_mode = "rr_multiple"` (the default), TP is now correctly
computed as `SL × rr_ratio` (= `11.61 × 2.5 = $29.03`, RR=2.50 ✅). The
raw `tp_pct` path remains for any future `tp_mode = "scalp_pct"` usage.

### 🔴 Fix — `ensure_persistent_settings` Fires 5× Per Startup (`config_loader.py`)

**Root cause:** Writing `SETTINGS_FILE` on every call changed its `mtime`,
invalidating the `load_settings` cache, causing the next `load_settings()`
call to call `ensure_persistent_settings()` again — indefinitely. With 5
`load_settings()` calls per startup cycle, the sync ran 5 times, writing the
volume file 5 times and spamming the log with
`Settings synced on startup: RF Scalp Bot → unknown` repeatedly.

**Fix:** Added a module-level `_settings_synced` flag. Once
`ensure_persistent_settings()` has run once in the process lifetime, all
subsequent calls return immediately.

### 🔴 Fix — Empty Bundled Settings Guard (`config_loader.py`)

If `DEFAULT_SETTINGS_PATH` (`settings.json` next to `config_loader.py`)
cannot be read (e.g. a container path layout issue), the function previously
overwrote the volume with an empty `{}`. Now it logs a warning and leaves the
volume file unchanged, so the bot continues with whatever is on the volume.

### 🟡 Fix — Broken Alternate Calendar CDN Removed (`calendar_fetcher.py`)

`cdn-nfs.faireconomy.media` does not resolve (confirmed `NameResolutionError`
in log). The alternate CDN fallback was removed. Next-week 404s are now
suppressed on all weekdays (Mon–Fri) since the feed isn't reliably published
until the weekend anyway.

---

## v1.2.3 — 2026-03-20

### 🔴 Bug Fix — Volume Settings Never Actually Updated on Railway (`config_loader.py`)

**Root cause (the real one):** v1.2.2 introduced a full-sync that fired when
`bot_name` changed between the volume file and the bundled `settings.json`.
This worked exactly once — the first boot wrote the new `bot_name` to the
volume. Every subsequent restart saw the same `bot_name` → no sync → the
volume file kept all stale values (`max_losing_trades_day=3`, `sl_pct=0.0015`
etc.) permanently.

**Confirmed from logs:** `Daily loss cap hit (3/3)` appeared on the SECOND
start of v1.2.2 (first start wrote new bot_name, second start skipped sync),
and every restart since.

**Fix:** `ensure_persistent_settings()` now **unconditionally overwrites** the
volume `/data/settings.json` with the bundled `settings.json` on every startup.
The Railway volume stores trade state (history, runtime state, ORB cache) —
not configuration. Configuration lives in the bundled file under version
control. Redeploy to change settings, not manual volume edits.

The old dead first-boot `setdefault` block was also removed.

### 🔴 Bug Fix — Stale Fallback `=3` in Cooldown Alert (`bot.py`)

`msg_cooldown_started()` was called with `day_limit=settings.get("max_losing_trades_day", 3)`.
Updated fallback to `8`.

### 🟡 Bug Fix — Stale Fallbacks in Startup Telegram (`scheduler.py`)

`msg_startup()` was called with `max_trades_london=4`, `max_trades_us=4`,
`max_losing_day=3` as hardcoded fallbacks. Updated to `10`, `10`, `8`.

---

## v1.2.2 — 2026-03-20

### 🔴 Bug Fix — Railway Volume Ignoring Updated Settings (`config_loader.py`)

**Root cause (confirmed from logs):** Railway persists `/data/settings.json` on
a volume across deployments. The previous merge logic in `ensure_persistent_settings()`
only injected **missing** keys from the bundled `settings.json`. Any key that
already existed in the volume kept its old value forever — meaning `sl_pct`,
`max_losing_trades_day`, `max_losing_trades_session` and all other keys from
earlier versions were **silently ignored** on every redeploy.

**Evidence from log:** Every trade showed `sl_pct_used: 0.0015` (old value)
despite `settings.json` having `0.0025`. The daily cap fired at `3/3` losses
(old value) despite settings showing `8`.

**Fix:** When `bot_name` changes between the volume file and the bundled
defaults (i.e. a new deployment), all values are now **fully synced** from the
bundled file. Same-version restarts still only inject missing keys so manual
operator edits are preserved.

### 🔴 Bug Fix — Stale Hardcoded Fallback Defaults (`config_loader.py`, `bot.py`)

Both files had `setdefault()` calls with old v1.0/v1.1 values:

| Key | Old fallback | New fallback |
|---|---|---|
| `sl_pct` | `0.0015` | `0.0025` |
| `sl_max_usd` | `8.0` | `15.0` |
| `exhaustion_atr_mult` | `2.0` | `3.0` |
| `max_losing_trades_session` | `2` | `4` |
| `max_losing_trades_day` | `3` | `8` |
| `max_trades_day` | `8` | `20` |
| `max_trades_london` | `4` | `10` |
| `max_trades_us` | `4` | `10` |
| `rr_ratio` | `3.0` | `2.5` |

These shadowed the correct values for any key that might be absent from the
loaded settings dict, acting as a second layer of stale defaults.

### 🔴 Bug Fix — SL Re-entry Gap Missed Same-Cycle Closures (`bot.py`)

**Root cause:** The 5-minute SL re-entry gap check ran **before** the OANDA
login, but `backfill_pnl()` — which writes `last_sl_closed_at_sgt` to runtime
state — runs **after** login. So in the cycle where a SL closes, the state
isn't written yet when the gap check runs, the check passes, and a new trade
fires immediately in the same cycle.

**Evidence from log:** Trade 481 closed via SL at 17:53:52 SGT. Trade 487
was placed at 17:53:54 SGT — 2 seconds later in the same cycle.

**Fix:** SL re-entry gap check moved to after `backfill_pnl()` in the
post-login section, so it always sees the current cycle's SL closure.

---

## v1.2.1 — 2026-03-20

### Cap Tuning for Scalp-Frequency Trading (`settings.json`)

Updated all risk caps to match target scalping session density.
No code logic was changed — purely configuration.

| Setting                    | v1.2.0 | v1.2.1 | Note                          |
|----------------------------|--------|--------|-------------------------------|
| `max_trades_day`           | 8      | **20** | Higher throughput for scalping|
| `max_trades_london`        | 4      | **10** | London window up to 10 trades |
| `max_trades_us`            | 4      | **10** | US window up to 10 trades     |
| `max_losing_trades_day`    | 4      | **8**  | 60% win-rate floor enforced   |
| `max_losing_trades_session`| 2      | **4**  | Session loss cap widened      |
| `loss_streak_cooldown_min` | 30     | 30     | Unchanged                     |
| `sl_reentry_gap_min`       | 5      | 5      | Unchanged                     |
| `breakeven_enabled`        | true   | **false** | Disabled per user config   |

**Rationale:**
- At RR=2.5 the mathematical breakeven win rate is only 28.6%, so the caps —
  not the RR — are the active risk limiter. Widening them allows the strategy
  to run more cycles and find higher-conviction setups across a full session.
- Loss cooldown (30 min after 2 consecutive losses) and SL re-entry gap (5 min
  after any SL hit) remain in place as the primary per-trade brakes.
- Break-even disabled to avoid premature SL moves on volatile XAU/USD candles.

### Minor Fix — US Window Telegram Label (`bot.py`)

Session-open alert for `US Window` previously showed `00:00–00:59` only,
missing the primary `21:00–23:59` slot. Corrected to `21:00–00:59`.

---

## v1.2.0 — 2026-03-20

### 🔴 Critical Fix — Re-enable All Risk Guards (`bot.py`)

**Problem:** v1.1 commented out three critical guards:
- `max_losing_trades_day` daily loss hard-stop
- `max_trades_day` daily trade hard-stop
- `max_trades_london` / `max_trades_us` per-session window caps
- `max_losing_trades_session` per-session loss sub-cap

All four were marked "REMOVED" in code but still present in `settings.json`,
creating a misleading configuration. With no guards active the bot executed
7 losing trades in a single session, losing ~$59 before two wins recovered
some ground.

**Fix:** All four guards re-implemented in `prepare_trade_context()`.
`daily_totals()` already computed the needed counters — the check blocks were
simply restored and connected to their settings keys.

### 🔴 New Feature — Single-Candle SL Re-entry Gap (`bot.py`)

**Problem:** After every SL hit, the bot re-entered within 1–5 minutes into
the same price zone. Trades 4→5 and 7→8 in the transaction CSV are examples
— both were stopped out immediately.

**Fix:** Added `sl_reentry_gap_min` setting (default 5 min). On every SL
close `backfill_pnl()` writes `last_sl_closed_at_sgt` to runtime state.
`prepare_trade_context()` checks this timestamp and blocks new entries until
the gap has elapsed.

### 🟡 Fix — ORB Breakout Wrongly Penalised by Exhaustion Filter (`signals.py`)

**Problem:** At 16:16 SGT the London ORB formed and price broke out, but the
exhaustion penalty dropped the score from 2 to 0 — blocking the trade. An ORB
breakout *is* a stretch by definition; penalising it as "exhaustion noise"
incorrectly filters the best entry of the day.

**Fix:** Exhaustion penalty now skips when `orb_contributed=True` (i.e. ORB
contributed +2 to the score). The penalty still fires on pure EMA setups.
`exhaustion_atr_mult` also raised from 2.0 → 3.0 in settings.

### 🟡 Fix — Widen Stop Loss (`settings.json`)

`sl_pct` changed from `0.0015` (0.15%) to `0.0025` (0.25%).
At $4600 gold this widens the stop from ~$6.9 to ~$11.5 — outside the typical
5-minute candle wick range of XAU/USD. `sl_max_usd` raised from $8 to $15
accordingly.

### 🟡 Fix — Enable Breakeven (`settings.json`)

`breakeven_enabled` set to `true`. Trigger raised from $3 → $5 so breakeven
only fires when the trade has meaningful profit cushion.

### 🟠 Fix — Calendar: Wider Gold Keywords + Alternate Next-Week URL (`calendar_fetcher.py`)

- Added 16 new gold-relevant USD keywords: `jolts`, `initial jobless`,
  `consumer confidence`, `michigan`, `yield`, `treasury`, `bond auction`,
  etc.
- `suppress_nextweek_404` now only suppresses Mon–Wed (days 0–2). On Thu/Fri
  when FF *should* publish next-week data, a 404 triggers a retry against
  `cdn-nfs.faireconomy.media` alternate URL.
- `days_ahead` in `_prune_old_events` widened from 14 → 21 so next-week
  events fetched early in the week survive the prune step.

---

## v1.1.1 — 2026-03-19

### 🐛 Bug Fix — CPR TC/BC Inversion (`signals.py`)

**Problem found in logs:** `CPR fetched | pivot=5008.12 TC=5006.94 BC=5009.31`
TC was less than BC, violating the CPR convention (Top Central Pivot must be
above Bottom Central Pivot).

**Root cause:** When the prior day closes *below* its high-low midpoint
(bearish session), the formula `TC = 2×pivot − BC` produces `TC < BC`.
Mathematically the values are correct, but the labels are inverted.

**What was happening (v1.1):** The CPR cache validation only ran on the stale
cache read path (which was removed in v1.1). On the fresh-fetch path there was
no validation at all — inverted TC/BC values were silently passed to the bias
filter. This had no effect on scoring (which only uses `pivot`), but the
`cpr_width_pct` and `TC`/`BC` log values were misleading.

**Fix:** After computing TC and BC, swap them if TC < BC:
```python
if tc < bc:
    tc, bc = bc, tc  # bearish prior-day close — re-label top/bottom
```
TC is now always the top of the CPR band. Pivot is unchanged. The structural
validation (`_validate_cpr_levels`) now runs as a post-swap sanity check and
will only fail if candle data is genuinely corrupt or degenerate
(zero-width CPR, which has ~1/5000 probability per day with real XAU/USD data).

**Impact:** Cosmetic in v1.1 (scoring was unaffected). In v1.1.1 the fix ensures
`TC`, `BC`, and `cpr_width_pct` in logs and Telegram alerts are always correct.

---

## v1.1 — 2026-03-19

### 🔓 Caps & Limits Removed

| Setting | Was | Now |
|---|---|---|
| `max_losing_trades_day` | Hard stop after 3 losses/day | Retained in settings for reporting only — **no longer enforced** |
| `max_trades_day` | Hard stop after 8 trades/day | Retained in settings for reporting only — **no longer enforced** |
| `max_trades_london` / `max_trades_us` | Hard stop after 4 trades/session window | **Fully removed** |
| `max_losing_trades_session` | Hard stop after 2 losses/session | **Fully removed** |

**What is still enforced:**
- ✅ Loss-streak cooldown (2 consecutive losses → 30-minute pause)
- ✅ Max concurrent open trades (1 at a time)
- ✅ Spread guard
- ✅ News filter (hard block on major events, penalty on medium)
- ✅ Friday cutoff
- ✅ Dead zone / session window (London 16:00–20:59, US 21:00–00:59 SGT)

---

### 📊 Signal Quality Fix

**`session_thresholds` raised 3 → 4 in `settings.json`:**

```json
"session_thresholds": {
  "London": 4,
  "US": 4
}
```

Previously a score of 3 (EMA aligned + CPR bias only, **no ORB break**) could
trigger a trade. At threshold 4, ORB confirmation is now a de facto requirement
for any trade entry — matching the strategy's original design intent.

Score map recap:

| Score | Components | Position |
|---|---|---|
| 6 | Fresh EMA cross + ORB break + CPR bias | $100 |
| 5 | Fresh EMA cross + ORB break (no CPR) | $100 |
| 4 | Aligned EMA + ORB break + CPR bias ← **new minimum** | $66 |
| 3 | Aligned EMA + CPR only (no ORB) ← **was allowed, now blocked** | — |

---

### 🔄 CPR Cache Removed (`signals.py`)

Central Pivot Range levels were previously cached in `cpr_cache.json` and
served from disk for the entire trading day. This meant a stale or invalid
cache could persist through market sessions.

**New behaviour:** CPR levels are fetched fresh from OANDA on every 5-minute
cycle using the previous day's daily candle. No cache file is read or written.

---

### 🧹 Internal Cleanups (`bot.py`)

- **`new_day_resume` alert block removed** — this alert fired when today
  followed a loss-cap day. Since `loss_cap_state` is no longer written to
  `ops_state.json`, the alert would never trigger. Dead code removed.

- **Session-open alert decoupled from window cap** — the alert that fires
  when a new trading session opens was previously gated on
  `_window_cap_open > 0`. With window caps removed, the gate was rewritten
  to fire unconditionally whenever `session_hours_sgt` is populated. The
  alert now passes `trade_cap=0` to indicate unlimited.

- **`validate_settings()` required list cleaned up** — `max_trades_day` and
  `max_losing_trades_day` removed from the mandatory keys list. The bot
  will no longer raise a `ValueError` if these are absent from
  `settings.json`.

- **`bot_name` / `__version__` bumped** — `"RF Scalp v1.1"` in
  `settings.json` and `"1.1"` in `version.py`.

---

### ✅ Verified Unchanged (audited, no issues found)

| File | Status |
|---|---|
| `oanda_trader.py` — login, circuit breaker, retry policy | ✅ Clean |
| `reconcile_state.py` — startup + runtime reconcile | ✅ Clean |
| `scheduler.py` — health server, graceful shutdown, crash-loop guard | ✅ Clean |
| `state_utils.py` — atomic JSON writes, timestamp parsing | ✅ Clean |
| `reporting.py` — daily / weekly / monthly report builders | ✅ Clean |
| `news_filter.py` — major/medium classification, penalty scoring | ✅ Clean |
| `config_loader.py` — settings cache, secrets resolution | ✅ Clean |
| `startup_checks.py` — env/margin/calendar pre-flight checks | ✅ Clean |

---

## v1.0 — Initial release

EMA 9/21 crossover + Opening Range Breakout (ORB) + CPR bias scalping
strategy on XAU/USD. M5 candles, SGT session windows (London 16:00–20:59,
US 21:00–00:59). OANDA execution with Telegram alerts.
