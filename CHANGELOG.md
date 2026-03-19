# RF Scalp Bot — Changelog

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
