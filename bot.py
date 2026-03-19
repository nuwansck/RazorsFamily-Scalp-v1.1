"""Main orchestrator for RF Scalp Bot v1.0

Runs the 5-minute scalping cycle for XAU/USD, applies session and risk controls,
places orders through OANDA, and persists runtime state.

Strategy: EMA 9/21 crossover + Opening Range Breakout (ORB) + CPR bias filter.
Signal timeframe: M5 candles.

Note: break-even SL mover is disabled (`breakeven_enabled: false`). The SL is
fixed at entry via pct_based mode (0.15%) and does not move after the trade opens.

Position sizing — values are read from settings.json:
  score 5-6  ->  position_full_usd    (default $100)
  score 3-4  ->  position_partial_usd (default $66)
  score < 4  ->  no trade

Active trading windows (SGT):
  London:    16:00-20:59  (max 4 trades)
  US:        21:00-00:59  (max 4 trades)
  Dead Zone: 01:00-15:59  (no new entries -- existing trades managed)
  Asian session disabled -- insufficient XAU/USD volatility for scalp setups.
"""

import json
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path

import pytz

from calendar_fetcher import run_fetch as refresh_calendar
from config_loader import DATA_DIR, get_bool_env, load_settings
from database import Database
from logging_utils import configure_logging, get_logger
from news_filter import NewsFilter
from oanda_trader import OandaTrader
from signals import SignalEngine, score_to_position_usd
from startup_checks import run_startup_checks
from state_utils import (
    RUNTIME_STATE_FILE, SCORE_CACHE_FILE, OPS_STATE_FILE, TRADE_HISTORY_FILE,
    update_runtime_state, load_json, save_json, parse_sgt_timestamp,
)
from telegram_alert import TelegramAlert
from telegram_templates import (
    msg_signal_update, msg_trade_opened, msg_breakeven, msg_trade_closed,
    msg_news_block, msg_news_penalty, msg_cooldown_started, msg_daily_cap,
    msg_spread_skip, msg_order_failed, msg_error, msg_friday_cutoff,
    msg_margin_adjustment, msg_new_day_resume, msg_session_open,
)
from reconcile_state import reconcile_runtime_state, startup_oanda_reconcile

configure_logging()
log = get_logger(__name__)

SGT          = pytz.timezone("Asia/Singapore")
INSTRUMENT   = "XAU_USD"

# v1.0 — startup reconcile runs exactly once per process (not every 5-min cycle)
_startup_reconcile_done: bool = False
ASSET        = "XAUUSD"
HISTORY_FILE = TRADE_HISTORY_FILE
HISTORY_DAYS = 90
# Removed: ARCHIVE_FILE — archival removed; 90-day rolling window stored in trade_history.json

# Session schedule (SGT):
#   00:00 – 00:59   US Window (NY morning continuation)
#   01:00 – 15:59   Dead zone — no new entries
#   16:00 – 20:59   London Window (08:00–13:00 GMT)
#   21:00 – 23:59   US Window (13:00–16:00 EDT)
SESSIONS = [
    ("US Window",     "US",      0,  0, 3),   # 00:00–00:59 SGT
    ("London Window", "London", 16, 20, 4),   # 16:00–20:59 SGT
    ("US Window",     "US",     21, 23, 4),   # 21:00–23:59 SGT
]

SESSION_BANNERS = {
    "London": "🇬🇧 LONDON",
    "US":     "🗽 US",
}


def get_trading_day(now_sgt: datetime, day_start_hour: int = 8) -> str:
    """Return the trading-day string (YYYY-MM-DD) for a given SGT datetime.

    v2.4 — The trading day resets at day_start_hour (default 08:00) SGT, not
    at calendar midnight.  Any time before 08:00 SGT belongs to the previous
    calendar day's cap bucket.  This prevents losses at 01:00 SGT (still in
    the previous day's US overnight window) from counting against today's cap.

    Example:
      03:45 SGT on 2026-03-20 → trading day is 2026-03-19
      10:00 SGT on 2026-03-20 → trading day is 2026-03-20
    """
    if now_sgt.hour < day_start_hour:
        return (now_sgt - timedelta(days=1)).strftime("%Y-%m-%d")
    return now_sgt.strftime("%Y-%m-%d")


def _clean_reason(text: str) -> str:
    text = (text or "").strip()
    if not text:
        return "No reason available"
    for part in reversed([p.strip() for p in text.split("|") if p.strip()]):
        plain = re.sub(r"^[^A-Za-z0-9]+", "", part).strip()
        if plain:
            return plain[:120]
    return text[:120]


def _build_signal_checks(score: int, direction: str, rr_ratio: float | None = None, tp_pct: float | None = None,
                         spread_pips: int | None = None, spread_limit: int | None = None, session_ok: bool = True,
                         news_ok: bool = True, open_trade_ok: bool = True, margin_ok: bool | None = None,
                         cooldown_ok: bool = True):
    mandatory_checks = [
        ("Score >= 3", score >= 3 and direction != "NONE", f"{score}/6"),
        ("RR >= 2", None if rr_ratio is None else rr_ratio >= 2.0, "n/a" if rr_ratio is None else f"{rr_ratio:.2f}"),
    ]
    quality_checks = [
        ("TP >= 0.35%", None if tp_pct is None else tp_pct >= 0.35, "n/a" if tp_pct is None else f"{tp_pct:.2f}%"),
    ]
    execution_checks = [
        ("Session active", session_ok, "active" if session_ok else "inactive"),
        ("News clear", news_ok, "clear" if news_ok else "blocked"),
        ("Cooldown clear", cooldown_ok, "clear" if cooldown_ok else "active"),
        ("No open trade", open_trade_ok, "ready" if open_trade_ok else "existing position"),
        ("Spread OK", None if spread_pips is None or spread_limit is None else spread_pips <= spread_limit, "n/a" if spread_pips is None or spread_limit is None else f"{spread_pips}/{spread_limit} pips"),
        ("Margin OK", margin_ok, "n/a" if margin_ok is None else ("pass" if margin_ok else "insufficient")),
    ]
    return mandatory_checks, quality_checks, execution_checks




def _signal_payload(**kwargs):
    mandatory_checks, quality_checks, execution_checks = _build_signal_checks(**kwargs)
    return {
        "mandatory_checks": mandatory_checks,
        "quality_checks": quality_checks,
        "execution_checks": execution_checks,
    }
# ── Settings ───────────────────────────────────────────────────────────────────

def validate_settings(settings: dict) -> dict:
    # v1.1: max_trades_day and max_losing_trades_day removed from required list —
    # they are retained as optional keys for reports/alerts but are no longer
    # enforced as hard-stop caps.
    required = [
        "spread_limits",
        "sl_mode",
        "tp_mode",
        "rr_ratio",
    ]
    missing = [k for k in required if k not in settings]
    if missing:
        raise ValueError(f"Missing required settings keys: {missing}")

    settings.setdefault("signal_threshold",             4)   # v1.0: raised from 3
    settings.setdefault("position_full_usd",            100)
    settings.setdefault("position_partial_usd",         66)
    settings.setdefault("account_balance_override",     0)
    settings.setdefault("enabled",                      True)
    settings.setdefault("atr_sl_multiplier",            0.3)    # scalp: tighter ATR mult
    settings.setdefault("sl_min_usd",                   2.0)    # scalp: tighter floor
    settings.setdefault("sl_max_usd",                   8.0)    # scalp: tighter ceiling
    settings.setdefault("fixed_sl_usd",                 5.0)
    settings.setdefault("breakeven_trigger_usd",        5.0)
    settings.setdefault("trading_day_start_hour_sgt",   8)   # v1.0: day resets at 08:00 SGT
    settings.setdefault("max_losing_trades_session",    2)   # v1.0: per-session sub-cap
    settings.setdefault("exhaustion_atr_mult",          2.0) # v1.0: trend exhaustion threshold
    settings.setdefault("sl_pct",                  0.0015)  # scalp: 0.15% SL
    settings.setdefault("tp_pct",                  0.0035)  # scalp: 0.35% TP
    settings.setdefault("margin_safety_factor",     0.6)
    settings.setdefault("margin_retry_safety_factor", 0.4)
    settings.setdefault("xau_margin_rate_override",  0.05)
    settings.setdefault("auto_scale_on_margin_reject", True)
    settings.setdefault("telegram_show_margin", True)
    settings.setdefault("friday_cutoff_hour_sgt",   23)
    settings.setdefault("friday_cutoff_minute_sgt", 0)
    settings.setdefault("news_lookahead_min",        120)
    settings.setdefault("news_medium_penalty_score", -1)
    settings.setdefault("fixed_tp_usd",             None)  # used when tp_mode = "fixed_usd"
    settings.setdefault("loss_streak_cooldown_min",  30)

    cooldown_min = int(settings.get("loss_streak_cooldown_min", 30))
    if cooldown_min < 0:
        raise ValueError("loss_streak_cooldown_min must be >= 0 (set to 0 to disable)")

    return settings


def is_friday_cutoff(now_sgt: datetime, settings: dict) -> bool:
    if now_sgt.weekday() != 4:
        return False
    cutoff_hour   = int(settings.get("friday_cutoff_hour_sgt", 23))
    cutoff_minute = int(settings.get("friday_cutoff_minute_sgt", 0))
    return now_sgt.hour > cutoff_hour or (
        now_sgt.hour == cutoff_hour and now_sgt.minute >= cutoff_minute
    )


# ── Trade history helpers ──────────────────────────────────────────────────────

def load_history() -> list:
    if not HISTORY_FILE.exists():
        return []
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save_history(history: list):
    save_json(HISTORY_FILE, history)


# atomic_json_write — canonical implementation is save_json() in state_utils.
# Kept as a thin alias so call sites within this file need no change.
def atomic_json_write(path: Path, data):
    save_json(path, data)


def prune_old_trades(history: list) -> list:
    """Drop trades older than HISTORY_DAYS from the active history.

    No archive file is written. The 90-day rolling window in
    trade_history.json is sufficient for all daily/weekly/monthly reports.
    Trades simply expire after 90 days.
    """
    cutoff = datetime.now(SGT) - timedelta(days=HISTORY_DAYS)
    active = []
    pruned = 0
    for trade in history:
        ts = trade.get("timestamp_sgt", "")
        try:
            dt = SGT.localize(datetime.strptime(ts, "%Y-%m-%d %H:%M:%S"))
            if dt < cutoff:
                pruned += 1
            else:
                active.append(trade)
        except Exception:
            active.append(trade)
    if pruned:
        log.info("Pruned %d trade(s) older than %d days | Active: %d", pruned, HISTORY_DAYS, len(active))
    return active


# ── Session helpers ────────────────────────────────────────────────────────────

def get_session(now: datetime, settings: dict = None):
    h = now.hour
    session_thresholds = (settings or {}).get("session_thresholds", {})
    for name, macro, start, end, fallback_thr in SESSIONS:
        if start <= h <= end:
            thr = int(session_thresholds.get(macro, fallback_thr))
            return name, macro, thr
    return None, None, None


def is_dead_zone_time(now_sgt: datetime) -> bool:
    """Dead zone: 01:00–15:59 SGT — no new entries."""
    return 1 <= now_sgt.hour <= 15


def get_window_key(session_name: str | None) -> str | None:
    if session_name == "London Window":
        return "London"
    if session_name == "US Window":
        return "US"
    return None


def get_window_trade_cap(window_key: str | None, settings: dict) -> int | None:
    if window_key == "London":
        return int(settings.get("max_trades_london", 4))
    if window_key == "US":
        return int(settings.get("max_trades_us", 4))
    return None


def window_trade_count(history: list, today_str: str, window_key: str) -> int:
    aliases = {
        "London": {"London", "London Window"},
        "US":     {"US", "US Window"},
    }
    valid = aliases.get(window_key, {window_key})
    count = 0
    for t in history:
        if not t.get("timestamp_sgt", "").startswith(today_str):
            continue
        if t.get("status") != "FILLED":
            continue
        trade_window = t.get("window") or t.get("session") or t.get("macro_session")
        if trade_window in valid:
            count += 1
    return count


def session_losses(history: list, today_str: str, macro: str) -> int:
    """Count losing FILLED trades for a specific macro-session today.

    v2.4 — Used for the per-session loss sub-cap.  A session is identified by
    its macro name (e.g. "London" or "US").  Matching is broad so legacy window
    labels also qualify.
    """
    aliases = {
        "London": {"London", "London Window"},
        "US":     {"US", "US Window"},
    }
    valid = aliases.get(macro, {macro})
    losses = 0
    for t in history:
        if not t.get("timestamp_sgt", "").startswith(today_str):
            continue
        if t.get("status") != "FILLED":
            continue
        trade_macro = t.get("macro_session") or t.get("window") or t.get("session") or ""
        if trade_macro not in valid:
            continue
        pnl = t.get("realized_pnl_usd")
        if isinstance(pnl, (int, float)) and pnl < 0:
            losses += 1
    return losses


# ── Risk / daily cap helpers ───────────────────────────────────────────────────

def daily_totals(history: list, today_str: str, trader=None, instrument: str = INSTRUMENT):
    pnl, count, losses = 0.0, 0, 0
    for t in history:
        if t.get("timestamp_sgt", "").startswith(today_str) and t.get("status") == "FILLED":
            count += 1
            p = t.get("realized_pnl_usd")
            if isinstance(p, (int, float)):
                pnl += p
                if p < 0:
                    losses += 1
    if trader is not None:
        try:
            position = trader.get_position(instrument)
            if position:
                unrealized = trader.check_pnl(position)
                pnl += unrealized
                # v1.0 fix: count an open losing position as a loss so the cap
                # fires before the position closes, preventing the 4/3 overshoot
                # where backfill_pnl records the loss one cycle too late.
                if unrealized < 0:
                    losses += 1
        except Exception as e:
            log.warning("Could not fetch unrealized P&L for daily cap: %s", e)
    return pnl, count, losses


def get_closed_trade_records_today(history: list, today_str: str) -> list:
    closed = []
    for t in history:
        if not t.get("timestamp_sgt", "").startswith(today_str):
            continue
        if t.get("status") != "FILLED":
            continue
        if isinstance(t.get("realized_pnl_usd"), (int, float)):
            closed.append(t)
    closed.sort(key=lambda t: t.get("closed_at_sgt") or t.get("timestamp_sgt") or "")
    return closed


def consecutive_loss_streak_today(history: list, today_str: str) -> int:
    streak = 0
    for t in reversed(get_closed_trade_records_today(history, today_str)):
        pnl = t.get("realized_pnl_usd")
        if not isinstance(pnl, (int, float)):
            continue
        if pnl < 0:
            streak += 1
        else:
            break
    return streak


# _parse_sgt_timestamp — canonical implementation lives in state_utils.parse_sgt_timestamp.
# Alias kept so call sites within this file need no change.
_parse_sgt_timestamp = parse_sgt_timestamp


def maybe_start_loss_cooldown(history: list, today_str: str, now_sgt: datetime, settings: dict):
    cooldown_min = int(settings.get("loss_streak_cooldown_min", 30))
    if cooldown_min <= 0:
        return None, None, 0
    streak = consecutive_loss_streak_today(history, today_str)
    if streak < 2:
        return None, None, streak
    closed = get_closed_trade_records_today(history, today_str)
    if len(closed) < 2:
        return None, None, streak
    trigger_trade  = closed[-1]
    trigger_marker = (
        trigger_trade.get("trade_id")
        or trigger_trade.get("closed_at_sgt")
        or trigger_trade.get("timestamp_sgt")
    )
    runtime_state = load_json(RUNTIME_STATE_FILE, {})
    if runtime_state.get("loss_cooldown_trigger") == trigger_marker:
        cooldown_until = _parse_sgt_timestamp(runtime_state.get("cooldown_until_sgt"))
        return cooldown_until, trigger_marker, streak
    cooldown_until = now_sgt + timedelta(minutes=cooldown_min)
    save_json(
        RUNTIME_STATE_FILE,
        {
            **runtime_state,
            "loss_cooldown_trigger": trigger_marker,
            "cooldown_until_sgt":   cooldown_until.strftime("%Y-%m-%d %H:%M:%S"),
            "cooldown_reason":      f"{streak} consecutive losses",
            "updated_at_sgt":       now_sgt.strftime("%Y-%m-%d %H:%M:%S"),
        },
    )
    return cooldown_until, trigger_marker, streak


def active_cooldown_until(now_sgt: datetime):
    runtime_state  = load_json(RUNTIME_STATE_FILE, {})
    cooldown_until = _parse_sgt_timestamp(runtime_state.get("cooldown_until_sgt"))
    if cooldown_until and now_sgt < cooldown_until:
        return cooldown_until
    return None


# ── Position sizing (v2.0) ─────────────────────────────────────────────────────

def compute_sl_usd(levels: dict, settings: dict) -> float:
    """Derive SL in USD.

    Priority:
      1. Use signal-engine structural recommendation when present.
      2. Fall back to the configured sl_mode logic.

    Fallback modes:
      pct_based  : SL = entry_price × sl_pct
      fixed_usd  : SL = fixed_sl_usd
      atr_based  : SL = ATR × atr_sl_multiplier, clamped to [sl_min, sl_max]
    """
    recommended = levels.get("sl_usd_rec")
    if recommended is not None:
        try:
            recommended = round(float(recommended), 2)
            if recommended > 0:
                log.debug("Using signal-recommended SL: $%.2f (%s)", recommended, levels.get("sl_source", "unknown"))
                return recommended
        except (TypeError, ValueError):
            pass

    sl_mode = str(settings.get("sl_mode", "pct_based")).lower()

    if sl_mode == "fixed_usd":
        return float(settings.get("fixed_sl_usd", 5.0))

    if sl_mode == "pct_based":
        entry  = levels.get("entry") or levels.get("current_price", 0)
        sl_pct = float(settings.get("sl_pct", 0.0015))
        if entry and entry > 0 and sl_pct > 0:
            sl_usd = round(entry * sl_pct, 2)
            log.debug("Pct SL fallback: %.2f × %.4f%% = $%.2f", entry, sl_pct * 100, sl_usd)
            return sl_usd
        fallback = float(settings.get("fixed_sl_usd", 5.0))
        log.warning("pct_based SL fallback: no valid entry price — fallback $%.2f", fallback)
        return fallback

    # atr_based
    current_atr = levels.get("atr")
    if not current_atr or current_atr <= 0:
        fallback = float(settings.get("sl_min_usd", 4.0))
        log.warning("ATR not available — using fallback SL of $%.2f", fallback)
        return fallback
    multiplier = float(settings.get("atr_sl_multiplier", 0.5))
    sl_min     = float(settings.get("sl_min_usd", 4.0))
    sl_max     = float(settings.get("sl_max_usd", 20.0))
    raw_sl     = current_atr * multiplier
    sl_usd     = max(sl_min, min(sl_max, raw_sl))
    log.debug("ATR SL fallback: ATR=%.2f x %.2f = %.2f → clamped $%.2f", current_atr, multiplier, raw_sl, sl_usd)
    return round(sl_usd, 2)


def compute_tp_usd(levels: dict, sl_usd: float, settings: dict) -> float:
    """Derive TP in USD.

    Priority:
      1. Use signal-engine structural recommendation when present.
      2. Fall back to fixed_usd or rr_multiple settings.
    """
    recommended = levels.get("tp_usd_rec")
    if recommended is not None:
        try:
            recommended = round(float(recommended), 2)
            if recommended > 0:
                log.debug("Using signal-recommended TP: $%.2f (%s)", recommended, levels.get("tp_source", "unknown"))
                return recommended
        except (TypeError, ValueError):
            pass

    tp_mode = str(settings.get("tp_mode", "rr_multiple")).lower()
    if tp_mode == "fixed_usd":
        return float(settings.get("fixed_tp_usd", sl_usd * 3))
    return round(sl_usd * float(settings.get("rr_ratio", 2.5)), 2)


def derive_rr_ratio(levels: dict, sl_usd: float, tp_usd: float, settings: dict) -> float:
    try:
        rr = float(levels.get("rr_ratio"))
        if rr > 0:
            return rr
    except (TypeError, ValueError):
        pass
    if sl_usd > 0 and tp_usd > 0:
        return round(tp_usd / sl_usd, 2)
    return float(settings.get("rr_ratio", 2.5))


# Note: compute_atr_sl_usd alias removed — no external callers exist in this codebase

def calculate_units_from_position(position_usd: int, sl_usd: float) -> float:
    """Convert score-based position risk to OANDA units.

    units = position_usd / sl_usd
    e.g. $66 risk at $6 SL = 11 units of XAU_USD
    """
    if sl_usd <= 0 or position_usd <= 0:
        return 0.0
    return round(position_usd / sl_usd, 2)


def apply_margin_guard(
    trader,
    instrument: str,
    requested_units: float,
    entry_price: float,
    free_margin: float,
    settings: dict,
) -> tuple[float, dict]:
    """Floor requested units against available margin before order placement."""
    margin_safety = float(settings.get("margin_safety_factor", 0.6))
    margin_retry_safety = float(settings.get("margin_retry_safety_factor", 0.4))
    specs = trader.get_instrument_specs(instrument)
    configured_floor = float(settings.get("xau_margin_rate_override", 0.20) or 0.20) if instrument == "XAU_USD" else 0.0
    margin_rate = max(float(specs.get("marginRate", 0.05) or 0.05), configured_floor)
    normalized_requested = trader.normalize_units(instrument, requested_units)
    required_margin_requested = trader.estimate_required_margin(instrument, normalized_requested, entry_price)

    if free_margin <= 0 or entry_price <= 0 or margin_rate <= 0:
        return 0.0, {
            "status": "SKIP",
            "reason": "invalid_margin_context",
            "free_margin": float(free_margin or 0),
            "required_margin": required_margin_requested,
            "requested_units": normalized_requested,
            "final_units": 0.0,
        }

    max_units_by_margin = (free_margin * margin_safety) / (entry_price * margin_rate)
    normalized_capped = trader.normalize_units(instrument, min(normalized_requested, max_units_by_margin))
    required_margin_final = trader.estimate_required_margin(instrument, normalized_capped, entry_price)
    status = "NORMAL" if abs(normalized_capped - normalized_requested) < 1e-9 else "ADJUSTED"
    reason = "margin_guard" if status == "ADJUSTED" else "ok"

    if normalized_capped <= 0:
        retry_units = trader.normalize_units(
            instrument,
            (free_margin * margin_retry_safety) / (entry_price * margin_rate),
        )
        required_retry = trader.estimate_required_margin(instrument, retry_units, entry_price)
        if retry_units > 0:
            return retry_units, {
                "status": "ADJUSTED",
                "reason": "margin_retry_floor",
                "free_margin": float(free_margin),
                "required_margin": required_retry,
                "requested_units": normalized_requested,
                "final_units": retry_units,
            }
        return 0.0, {
            "status": "SKIP",
            "reason": "insufficient_margin",
            "free_margin": float(free_margin),
            "required_margin": required_margin_requested,
            "requested_units": normalized_requested,
            "final_units": 0.0,
        }

    return normalized_capped, {
        "status": status,
        "reason": reason,
        "free_margin": float(free_margin),
        "required_margin": required_margin_final,
        "requested_units": normalized_requested,
        "final_units": normalized_capped,
    }


def compute_sl_tp_pips(sl_usd: float, tp_usd: float):
    pip = 0.01
    return round(sl_usd / pip), round(tp_usd / pip)


def compute_sl_tp_prices(entry: float, direction: str, sl_usd: float, tp_usd: float):
    """Return (sl_price, tp_price) based on direction and dollar distances."""
    if direction == "BUY":
        return round(entry - sl_usd, 2), round(entry + tp_usd, 2)
    return round(entry + sl_usd, 2), round(entry - tp_usd, 2)


def get_effective_balance(balance: float | None, settings: dict) -> float:
    override = settings.get("account_balance_override")
    if override is not None:
        try:
            v = float(override)
            if v > 0:
                return v
        except (TypeError, ValueError):
            pass
    return float(balance or 0)


# ── Score / cache helpers ─────────────────────────────────────────────────────

def load_signal_cache() -> dict:
    """Load signal dedup cache (score, direction, last_signal_msg)."""
    if not SCORE_CACHE_FILE.exists():
        return {}
    try:
        with open(SCORE_CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_signal_cache(cache: dict):
    atomic_json_write(SCORE_CACHE_FILE, cache)


def load_ops_state() -> dict:
    """Load ops state cache (ops_state, last_session)."""
    if not OPS_STATE_FILE.exists():
        return {}
    try:
        with open(OPS_STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_ops_state(state: dict):
    atomic_json_write(OPS_STATE_FILE, state)


# Keep backward-compat aliases so nothing outside bot.py needs touching
load_score_cache = load_signal_cache
save_score_cache = save_signal_cache


def send_once_per_state(alert, cache: dict, key: str, value: str, message: str):
    if cache.get(key) != value:
        alert.send(message)
        cache[key] = value
        save_ops_state(cache)


# ── Break-even management ──────────────────────────────────────────────────────

def check_breakeven(history: list, trader, alert, settings: dict):
    demo        = settings.get("demo_mode", True)
    trigger_usd = float(settings.get("breakeven_trigger_usd", 5.0))
    changed     = False

    for trade in history:
        if trade.get("status") != "FILLED":
            continue
        if trade.get("breakeven_moved"):
            continue
        trade_id  = trade.get("trade_id")
        entry     = trade.get("entry")
        direction = trade.get("direction", "")
        if not trade_id or not entry or direction not in ("BUY", "SELL"):
            continue

        open_trade = trader.get_open_trade(str(trade_id))
        if open_trade is None:
            continue

        mid, bid, ask = trader.get_price(INSTRUMENT)
        if mid is None:
            continue

        current_price = bid if direction == "BUY" else ask
        trigger_price = (
            entry + trigger_usd if direction == "BUY" else entry - trigger_usd
        )
        triggered = (
            (direction == "BUY"  and current_price >= trigger_price) or
            (direction == "SELL" and current_price <= trigger_price)
        )
        if not triggered:
            continue

        result = trader.modify_sl(str(trade_id), float(entry))
        if result.get("success"):
            trade["breakeven_moved"] = True
            changed = True
            try:
                unrealized_pnl = float(open_trade.get("unrealizedPL", 0))
            except Exception:
                unrealized_pnl = 0
            alert.send(msg_breakeven(
                trade_id=trade_id,
                direction=direction,
                entry=entry,
                trigger_price=trigger_price,
                trigger_usd=trigger_usd,
                current_price=current_price,
                unrealized_pnl=unrealized_pnl,
                demo=demo,
            ))
        else:
            log.warning("Break-even move failed for trade %s: %s", trade_id, result.get("error"))

    if changed:
        save_history(history)


# ── PnL backfill ───────────────────────────────────────────────────────────────

def backfill_pnl(history: list, trader, alert, settings: dict) -> list:
    changed = False
    demo = settings.get("demo_mode", True)
    for trade in history:
        if trade.get("status") == "FILLED" and trade.get("realized_pnl_usd") is None:
            trade_id = trade.get("trade_id")
            if trade_id:
                pnl = trader.get_trade_pnl(str(trade_id))
                if pnl is not None:
                    trade["realized_pnl_usd"] = pnl
                    trade["closed_at_sgt"] = datetime.now(SGT).strftime("%Y-%m-%d %H:%M:%S")
                    changed = True
                    log.info("Back-filled P&L trade %s: $%.2f", trade_id, pnl)
                    if not trade.get("closed_alert_sent"):
                        try:
                            _cp  = trade.get("tp_price") if pnl > 0 else trade.get("sl_price")
                            _dur = ""
                            _t1s = trade.get("timestamp_sgt", "")
                            _t2s = trade.get("closed_at_sgt", "")
                            if _t1s and _t2s:
                                _d = int(
                                    (datetime.strptime(_t2s, "%Y-%m-%d %H:%M:%S") -
                                     datetime.strptime(_t1s, "%Y-%m-%d %H:%M:%S")).total_seconds() // 60
                                )
                                _dur = f"{_d // 60}h {_d % 60}m" if _d >= 60 else f"{_d}m"
                            alert.send(msg_trade_closed(
                                trade_id=trade_id,
                                direction=trade.get("direction", ""),
                                setup=trade.get("setup", ""),
                                entry=float(trade.get("entry", 0)),
                                close_price=float(_cp or 0),
                                pnl=float(pnl),
                                session=trade.get("session", ""),
                                demo=demo,
                                duration_str=_dur,
                            ))
                            trade["closed_alert_sent"] = True
                        except Exception as _e:
                            log.warning("Could not send trade_closed alert: %s", _e)
    if changed:
        save_history(history)
    return history


# ── Logging helper ─────────────────────────────────────────────────────────────

def log_event(code: str, message: str, level: str = "info", **extra):
    logger_fn = getattr(log, level, log.info)
    payload   = {"event": code}
    payload.update(extra)
    logger_fn(f"[{code}] {message}", extra=payload)


# ── Main cycle ─────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────────
# Cycle phases
#
# run_bot_cycle() is the thin public entry point called by the scheduler.
# It delegates to three private helpers, each with a single responsibility:
#
#   _guard_phase()      — all pre-trade checks: calendar, login, caps, session,
#                         news, cooldowns, spread.  Returns a populated ctx dict
#                         on success, or None to abort the cycle.
#   _signal_phase()     — Scalp signal evaluation (EMA+ORB+CPR bias), position sizing, margin guard.
#                         Returns ctx with execution-ready parameters, or None.
#   _execution_phase()  — places the order and persists the trade record.
# ─────────────────────────────────────────────────────────────────────────────



def _next_day_reset_sgt(now_sgt: datetime, day_start_hour: int = 8) -> str:
    """Return the next trading-day reset time as a human-readable string."""
    if now_sgt.hour < day_start_hour:
        reset = now_sgt.replace(hour=day_start_hour, minute=0, second=0, microsecond=0)
    else:
        reset = (now_sgt + timedelta(days=1)).replace(hour=day_start_hour, minute=0, second=0, microsecond=0)
    return reset.strftime("%Y-%m-%d %H:%M SGT")

def _guard_phase(db, run_id, settings, alert, history, now_sgt, today, demo) -> dict | None:
    """All pre-trade guards.  Returns a populated context dict (including trader) or None."""

    # ops_state cache: deduplicates operational Telegram alerts (session changes,
    # news blocks, cooldowns, caps). Stored in ops_state.json — separate from
    # signal_cache.json which tracks score/direction dedup.
    ops = load_ops_state()

    warnings = run_startup_checks()
    for warning in warnings:
        log.warning(warning, extra={"run_id": run_id})

    log.info(
        "=== %s | %s SGT ===",
        settings.get("bot_name", "RF Scalp v1.0"),
        now_sgt.strftime("%Y-%m-%d %H:%M"),
        extra={"run_id": run_id, "pair": INSTRUMENT},
    )
    update_runtime_state(
        last_cycle_started=now_sgt.strftime("%Y-%m-%d %H:%M:%S"),
        last_run_id=run_id,
        status="RUNNING",
    )
    db.upsert_state("last_cycle_started", {
        "run_id": run_id,
        "started_at_sgt": now_sgt.strftime("%Y-%m-%d %H:%M:%S"),
    })

    if not settings.get("enabled", True) or get_bool_env("TRADING_DISABLED", False):
        log.warning("Trading disabled.", extra={"run_id": run_id})
        send_once_per_state(alert, ops, "ops_state", "disabled", "⏸️ Trading disabled by configuration.")
        update_runtime_state(last_cycle_finished=now_sgt.strftime("%Y-%m-%d %H:%M:%S"), status="SKIPPED_DISABLED")
        db.finish_cycle(run_id, status="SKIPPED", summary={"stage": "enabled_check", "reason": "disabled"})
        return None

    history[:] = prune_old_trades(history)
    save_history(history)

    weekday = now_sgt.weekday()
    if weekday == 5:
        log.info("Saturday — market closed.", extra={"run_id": run_id})
        update_runtime_state(last_cycle_finished=now_sgt.strftime("%Y-%m-%d %H:%M:%S"), status="SKIPPED_MARKET_CLOSED")
        db.finish_cycle(run_id, status="SKIPPED", summary={"stage": "market_guard", "reason": "Saturday"})
        return None
    if weekday == 6:
        log.info("Sunday — waiting for Monday open.", extra={"run_id": run_id})
        update_runtime_state(last_cycle_finished=now_sgt.strftime("%Y-%m-%d %H:%M:%S"), status="SKIPPED_MARKET_CLOSED")
        db.finish_cycle(run_id, status="SKIPPED", summary={"stage": "market_guard", "reason": "Sunday"})
        return None
    if weekday == 0 and now_sgt.hour < 8:
        log.info("Monday pre-open (before 08:00 SGT) — skipping.", extra={"run_id": run_id})
        update_runtime_state(last_cycle_finished=now_sgt.strftime("%Y-%m-%d %H:%M:%S"), status="SKIPPED_MARKET_CLOSED")
        db.finish_cycle(run_id, status="SKIPPED", summary={"stage": "market_guard", "reason": "Monday pre-open"})
        return None

    if settings.get("news_filter_enabled", True):
        try:
            refresh_calendar()
        except Exception as e:
            log.warning("Calendar refresh failed (using cached): %s", e, extra={"run_id": run_id})

    # ── v1.1: Daily cap resume and hard-stop cap checks REMOVED ──────────────
    # loss_cap_state is no longer written; new_day_resume will never fire.
    # max_losing_trades_day and max_trades_day are retained in settings.json
    # for reporting/reference but are not enforced.

    cooldown_started_until, _, cooldown_streak = maybe_start_loss_cooldown(history, today, now_sgt, settings)
    if cooldown_started_until and now_sgt < cooldown_started_until:
        _cd_sess_name  = get_session(now_sgt, settings)[0] or ""
        _cd_day_pnl, _cd_day_trades, _cd_day_losses = daily_totals(history, today)
        send_once_per_state(
            alert, ops, "cooldown_started_state",
            f"cooldown_started:{cooldown_started_until.strftime('%Y-%m-%d %H:%M:%S')}",
            msg_cooldown_started(
                streak=cooldown_streak,
                cooldown_until_sgt=cooldown_started_until.strftime("%H:%M"),
                session_name=_cd_sess_name,
                day_losses=_cd_day_losses,
                day_limit=int(settings.get("max_losing_trades_day", 3)),
            ),
        )
        log_event("COOLDOWN_STARTED", f"Cooldown until {cooldown_started_until.strftime('%Y-%m-%d %H:%M:%S')} SGT.", run_id=run_id)

    session, macro, threshold = get_session(now_sgt, settings)

    if is_friday_cutoff(now_sgt, settings):
        log_event("FRIDAY_CUTOFF", "Friday cutoff active.", run_id=run_id)
        send_once_per_state(alert, ops, "ops_state",
            f"friday_cutoff:{now_sgt.strftime('%Y-%m-%d')}",
            msg_friday_cutoff(int(settings.get("friday_cutoff_hour_sgt", 23))),
        )
        update_runtime_state(last_cycle_finished=now_sgt.strftime("%Y-%m-%d %H:%M:%S"), status="SKIPPED_FRIDAY_CUTOFF")
        db.finish_cycle(run_id, status="SKIPPED", summary={"stage": "friday_cutoff"})
        return None

    if settings.get("session_only", True):
        if session is None:
            if is_dead_zone_time(now_sgt):
                log_event("DEAD_ZONE_SKIP", "Dead zone — entry blocked, management active.", run_id=run_id)
            else:
                log.info("Outside all sessions — skipping.", extra={"run_id": run_id})
            if ops.get("last_session") is not None:
                send_once_per_state(alert, ops, "ops_state", "outside_session", "⏸️ Outside active session — no trade.")
                ops["last_session"] = None
                save_ops_state(ops)
            update_runtime_state(last_cycle_finished=now_sgt.strftime("%Y-%m-%d %H:%M:%S"), status="SKIPPED_OUTSIDE_SESSION")
            db.finish_cycle(run_id, status="SKIPPED", summary={"stage": "session_check", "reason": "outside_session"})
            return None
    else:
        if session is None:
            session, macro = "All Hours", "London"
        threshold = int(settings.get("signal_threshold", 4))

    threshold = threshold or int(settings.get("signal_threshold", 4))
    banner    = SESSION_BANNERS.get(macro, "📊")
    log.info("Session: %s (%s)", session, macro, extra={"run_id": run_id})

    if ops.get("last_session") != session:
        # Fire a session-open alert when entering a new trading window
        if session is not None:
            _hours_map = {
                "US Window":     "00:00–00:59",
                "London Window": "16:00–20:59",
            }
            _sess_hours = _hours_map.get(session, "")
            if _sess_hours:
                _day_pnl_open, _day_cnt_open, _ = daily_totals(history, today)
                send_once_per_state(
                    alert, ops,
                    "session_open_state", f"session_open:{session}:{today}",
                    msg_session_open(
                        session_name=session,
                        session_hours_sgt=_sess_hours,
                        trade_cap=0,          # v1.1: window caps removed; 0 = unlimited
                        trades_today=_day_cnt_open,
                        daily_pnl=_day_pnl_open,
                    ),
                )
        ops["last_session"] = session
        ops.pop("ops_state", None)
        save_ops_state(ops)

    # ── News filter ────────────────────────────────────────────────────────────
    news_penalty = 0
    news_status  = {}
    if settings.get("news_filter_enabled", True):
        nf = NewsFilter(
            before_minutes=int(settings.get("news_block_before_min", 30)),
            after_minutes=int(settings.get("news_block_after_min", 30)),
            lookahead_minutes=int(settings.get("news_lookahead_min", 120)),
            medium_penalty=int(settings.get("news_medium_penalty_score", -1)),
        )
        news_status  = nf.get_status_now()
        blocked      = bool(news_status.get("blocked"))
        reason       = str(news_status.get("reason", "No blocking news"))
        news_penalty = int(news_status.get("penalty", 0))
        lookahead    = news_status.get("lookahead", [])
        if lookahead:
            la_summary = " | ".join(
                f"{e['name']} in {e['mins_away']}min ({e['severity']})"
                for e in lookahead[:3]
            )
            log.info("Upcoming news: %s", la_summary, extra={"run_id": run_id})
        if blocked:
            _evt       = news_status.get("event", {})
            _block_msg = msg_news_block(
                event_name=_evt.get("name", reason),
                event_time_sgt=_evt.get("time_sgt", ""),
                before_min=int(settings.get("news_block_before_min", 30)),
                after_min=int(settings.get("news_block_after_min", 30)),
            )
            send_once_per_state(alert, ops, "ops_state", f"news:{reason}", _block_msg)
            db.upsert_state("last_news_block", {"blocked": True, "reason": reason, "checked_at_sgt": now_sgt.strftime("%Y-%m-%d %H:%M:%S")})
            update_runtime_state(last_cycle_finished=now_sgt.strftime("%Y-%m-%d %H:%M:%S"), status="SKIPPED_NEWS_BLOCK", reason=reason)
            db.finish_cycle(run_id, status="SKIPPED", summary={"stage": "news_filter", "reason": reason})
            return None
        db.upsert_state("last_news_block", {
            "blocked": False, "reason": reason if news_penalty else None,
            "penalty": news_penalty, "checked_at_sgt": now_sgt.strftime("%Y-%m-%d %H:%M:%S"),
        })

    # ── Lazy OandaTrader construction + circuit breaker ───────────────────────
    # Only constructed here — after the early loss-cap guard — so cooldown days
    # never produce "OANDA | Mode: DEMO / Account: ..." noise in every cycle.
    #
    # Circuit breaker: if login_with_summary() has been failing consecutively,
    # suppress per-cycle error alerts and only re-alert every 12 failures (~1 hour).
    trader          = OandaTrader(demo=demo)
    account_summary = trader.login_with_summary()
    _cb_state       = load_json(RUNTIME_STATE_FILE, {})
    _cb_failures    = int(_cb_state.get("oanda_consecutive_failures", 0))

    if account_summary is None:
        _cb_failures += 1
        save_json(RUNTIME_STATE_FILE, {**_cb_state, "oanda_consecutive_failures": _cb_failures})
        # Alert on first failure and every 12th thereafter (~1 hour at 5-min cycles)
        if _cb_failures == 1 or _cb_failures % 12 == 0:
            alert.send(msg_error(
                "OANDA login failed",
                f"Consecutive failures: {_cb_failures}. Check OANDA_API_KEY and OANDA_ACCOUNT_ID.",
            ))
        log.warning("OANDA login failed (consecutive=%d)", _cb_failures)
        db.finish_cycle(run_id, status="FAILED", summary={"stage": "oanda_login", "reason": "login_failed", "consecutive_failures": _cb_failures})
        update_runtime_state(last_cycle_finished=now_sgt.strftime("%Y-%m-%d %H:%M:%S"), status="FAILED_LOGIN")
        return None

    # Login succeeded — reset circuit breaker counter
    if _cb_failures > 0:
        save_json(RUNTIME_STATE_FILE, {**_cb_state, "oanda_consecutive_failures": 0})
        if _cb_failures >= 3:
            alert.send(f"✅ OANDA connection restored after {_cb_failures} failed attempt(s).")

    balance = account_summary["balance"]
    if balance <= 0:
        alert.send(msg_error("Cannot fetch balance", "OANDA account returned $0 or invalid"))
        db.finish_cycle(run_id, status="FAILED", summary={"stage": "oanda_login", "reason": "invalid_balance"})
        update_runtime_state(last_cycle_finished=now_sgt.strftime("%Y-%m-%d %H:%M:%S"), status="FAILED_LOGIN")
        return None

    reconcile = reconcile_runtime_state(trader, history, INSTRUMENT, now_sgt, alert=alert)
    if reconcile.get("recovered_trade_ids") or reconcile.get("backfilled_trade_ids"):
        save_history(history)
    db.upsert_state("last_reconciliation", {**reconcile, "checked_at_sgt": now_sgt.strftime("%Y-%m-%d %H:%M:%S")})

    # Backfill PnL for any FILLED trades missing realized_pnl. Requires an
    # active OANDA connection — placed here after login so trader is available.
    # Break-even SL mover is intentionally disabled: SL is fixed at 0.25% via
    # pct_based mode and does not move after entry. (`breakeven_enabled: false`)
    history[:] = backfill_pnl(history, trader, alert, settings)
    if settings.get("breakeven_enabled", False):
        check_breakeven(history, trader, alert, settings)

    # ── Daily caps REMOVED ─────────────────────────────────────────────────────
    # max_losing_trades_day and max_trades_day hard-stops are disabled.
    # daily_pnl/daily_trades still computed for reference by other code.
    daily_pnl, daily_trades, daily_losses = daily_totals(history, today, trader=trader)

    cooldown_until = active_cooldown_until(now_sgt)
    if cooldown_until:
        remaining_min = max(1, int((cooldown_until - now_sgt).total_seconds() // 60))
        msg = f"🧊 Cooldown active — new entries paused for {remaining_min} more minute(s)."
        send_once_per_state(alert, ops, "cooldown_guard_state", f"cooldown:{cooldown_until.strftime('%Y-%m-%d %H:%M:%S')}", msg)
        update_runtime_state(last_cycle_finished=now_sgt.strftime("%Y-%m-%d %H:%M:%S"), status="SKIPPED_COOLDOWN")
        db.finish_cycle(run_id, status="SKIPPED", summary={"stage": "cooldown_guard"})
        return None

    # ── Window cap REMOVED ─────────────────────────────────────────────────────
    # max_trades_london / max_trades_us per-session trade caps are disabled.

    # ── Per-session loss sub-cap REMOVED ──────────────────────────────────────
    # max_losing_trades_session (2-loss per session limit) is disabled.

    open_count     = trader.get_open_trades_count(INSTRUMENT)
    max_concurrent = int(settings.get("max_concurrent_trades", 1))
    if open_count >= max_concurrent:
        msg = f"⏸️ Max concurrent trades reached ({open_count}/{max_concurrent}) — waiting."
        send_once_per_state(alert, ops, "open_cap_state", f"open_cap:{open_count}:{max_concurrent}", msg)
        update_runtime_state(last_cycle_finished=now_sgt.strftime("%Y-%m-%d %H:%M:%S"), status="SKIPPED_OPEN_TRADE_CAP")
        db.finish_cycle(run_id, status="SKIPPED", summary={"stage": "open_trade_guard"})
        return None

    return {
        "trader": trader,
        "balance": balance, "account_summary": account_summary,
        "session": session, "macro": macro, "threshold": threshold,
        "banner": banner, "ops": ops,
        "news_penalty": news_penalty, "news_status": news_status,
        "effective_balance": get_effective_balance(balance, settings),
    }


def _signal_phase(db, run_id, settings, alert, trader, history, now_sgt, today, demo, ctx) -> dict | None:
    """Scalp signal evaluation (EMA + ORB + CPR bias), sizing, and margin guard.
    Returns ctx extended with execution parameters, or None (cycle aborted)."""

    session      = ctx["session"]
    macro        = ctx["macro"]
    banner       = ctx["banner"]
    ops          = ctx["ops"]
    sig_cache    = load_signal_cache()
    news_penalty = ctx["news_penalty"]
    news_status  = ctx["news_status"]
    balance      = ctx["balance"]
    account_summary = ctx["account_summary"]

    # ── Signal ────────────────────────────────────────────────────────────────
    engine = SignalEngine(demo=demo)
    score, direction, details, levels, position_usd = engine.analyze(asset=ASSET, settings=settings)

    raw_score        = score
    raw_position_usd = position_usd

    if news_penalty:
        score        = max(score + news_penalty, 0)
        position_usd = score_to_position_usd(score, settings)
        details      = details + f" | ⚠️ News penalty applied ({news_penalty:+d})"
        _nev = news_status.get("events", [])
        if not _nev and news_status.get("event"):
            _nev = [news_status["event"]]
        send_once_per_state(
            alert, ops, "ops_state", f"news_penalty:{news_penalty}:{today}",
            msg_news_penalty(
                event_names=[e.get("name", "") for e in _nev],
                penalty=news_penalty,
                score_after=score,
                score_before=raw_score,
                position_after=position_usd,
                position_before=raw_position_usd,
            ),
        )

    db.record_signal(
        {"pair": INSTRUMENT, "timeframe": "M5", "side": direction,
         "score": score, "raw_score": raw_score,
         "news_penalty": news_penalty, "details": details, "levels": levels},
        timeframe="M5", run_id=run_id,
    )

    cpr_w = levels.get("cpr_width_pct", 0)

    def _send_signal_update(decision, reason, extra_payload=None):
        payload = _signal_payload(score=score, direction=direction, **(extra_payload or {}))
        msg = msg_signal_update(
            banner=banner, session=session, direction=direction,
            score=score, position_usd=position_usd, cpr_width_pct=cpr_w,
            detail_lines=details.split(" | "), news_penalty=news_penalty,
            raw_score=raw_score, decision=decision, reason=reason,
            cycle_minutes=int(settings.get("cycle_minutes", 5)),
            **payload,
        )
        if msg != sig_cache.get("last_signal_msg", ""):
            alert.send(msg)
            sig_cache.update({"score": score, "direction": direction, "last_signal_msg": msg})
            save_signal_cache(sig_cache)

    # ── No setup or below threshold ───────────────────────────────────────────
    # v1.0 fix: threshold was stored in ctx but never compared against score in
    # previous versions, meaning signal_threshold had no effect.  Explicit gate
    # added here — score must meet the session threshold (default 4) to proceed.
    if direction == "NONE" or position_usd <= 0:
        _send_signal_update("WATCHING", _clean_reason(details),
                            {"session_ok": True, "news_ok": True, "open_trade_ok": True})
        log.info("No trade. Score=%s dir=%s position=$%s", score, direction, position_usd, extra={"run_id": run_id})
        update_runtime_state(last_cycle_finished=now_sgt.strftime("%Y-%m-%d %H:%M:%S"), status="COMPLETED_NO_SIGNAL", score=score, direction=direction)
        db.finish_cycle(run_id, status="COMPLETED", summary={"signals": 1, "trades_placed": 0, "score": score, "direction": direction})
        return None

    _effective_threshold = int(ctx.get("threshold", settings.get("signal_threshold", 4)))
    if score < _effective_threshold:
        _send_signal_update(
            "WATCHING",
            f"Score {score}/6 below session threshold ({_effective_threshold})",
            {"session_ok": True, "news_ok": True, "open_trade_ok": True},
        )
        log.info("Score %s below threshold %s — watching", score, _effective_threshold, extra={"run_id": run_id})
        update_runtime_state(last_cycle_finished=now_sgt.strftime("%Y-%m-%d %H:%M:%S"), status="COMPLETED_BELOW_THRESHOLD", score=score, direction=direction)
        db.finish_cycle(run_id, status="COMPLETED", summary={"signals": 1, "trades_placed": 0, "score": score, "direction": direction, "reason": "below_threshold"})
        return None

    if not settings.get("trade_gold", True):
        update_runtime_state(last_cycle_finished=now_sgt.strftime("%Y-%m-%d %H:%M:%S"), status="SKIPPED_TRADE_GOLD_DISABLED")
        db.finish_cycle(run_id, status="SKIPPED", summary={"stage": "trade_switch"})
        return None

    # ── Position sizing ───────────────────────────────────────────────────────
    entry = levels.get("entry", 0)
    if entry <= 0:
        _, _, ask = trader.get_price(INSTRUMENT)
        entry = ask or 0

    sl_usd   = compute_sl_usd(levels, settings)
    tp_usd   = compute_tp_usd(levels, sl_usd, settings)
    rr_ratio = derive_rr_ratio(levels, sl_usd, tp_usd, settings)
    units    = calculate_units_from_position(position_usd, sl_usd)
    tp_pct   = (tp_usd / entry * 100) if entry > 0 else None

    if units <= 0:
        alert.send(msg_error("Position size = 0", f"position_usd=${position_usd} sl=${sl_usd:.2f}"))
        db.finish_cycle(run_id, status="SKIPPED", summary={"stage": "position_sizing", "reason": "zero_units"})
        update_runtime_state(last_cycle_finished=now_sgt.strftime("%Y-%m-%d %H:%M:%S"), status="SKIPPED_ZERO_UNITS")
        return None

    signal_blockers = list(levels.get("signal_blockers") or [])
    if signal_blockers:
        _send_signal_update("BLOCKED", signal_blockers[0],
                            {"rr_ratio": rr_ratio, "tp_pct": tp_pct, "session_ok": True, "news_ok": True, "open_trade_ok": True, "margin_ok": None})
        log.info("Signal blocked before execution: %s", signal_blockers[0], extra={"run_id": run_id})
        update_runtime_state(last_cycle_finished=now_sgt.strftime("%Y-%m-%d %H:%M:%S"), status="SKIPPED_SIGNAL_BLOCKED", reason=signal_blockers[0])
        db.finish_cycle(run_id, status="SKIPPED", summary={"stage": "signal_validation", "reason": signal_blockers[0]})
        return None

    # ── Margin guard ──────────────────────────────────────────────────────────
    # account_summary already fetched at login — no second OANDA call needed
    margin_available  = float(account_summary.get("margin_available", balance or 0) or 0)
    price_for_margin  = entry if entry > 0 else float(levels.get("current_price", entry) or 0)
    units, margin_info = apply_margin_guard(
        trader=trader, instrument=INSTRUMENT,
        requested_units=units, entry_price=price_for_margin,
        free_margin=margin_available, settings=settings,
    )
    if margin_info.get("status") == "ADJUSTED":
        log.warning(
            "Margin protection adjusted %.2f → %.2f units | free_margin=%.2f required=%.2f",
            float(margin_info.get("requested_units", 0)), float(margin_info.get("final_units", 0)),
            float(margin_info.get("free_margin", 0)), float(margin_info.get("required_margin", 0)),
        )
        alert.send(msg_margin_adjustment(
            instrument=INSTRUMENT,
            requested_units=float(margin_info.get("requested_units", 0)),
            adjusted_units=float(margin_info.get("final_units", 0)),
            free_margin=float(margin_info.get("free_margin", 0)),
            required_margin=float(margin_info.get("required_margin", 0)),
            reason=str(margin_info.get("reason", "margin_guard")),
        ))
    if units <= 0:
        _send_signal_update("BLOCKED", "Insufficient margin after safety checks",
                            {"rr_ratio": rr_ratio, "tp_pct": tp_pct, "session_ok": True, "news_ok": True, "open_trade_ok": True, "margin_ok": False})
        alert.send(msg_error(
            "Insufficient margin — trade skipped",
            f"free_margin=${margin_available:.2f} required=${float(margin_info.get('required_margin', 0)):.2f}",
        ))
        db.finish_cycle(run_id, status="SKIPPED", summary={"stage": "margin_cap", "reason": "insufficient_margin"})
        update_runtime_state(last_cycle_finished=now_sgt.strftime("%Y-%m-%d %H:%M:%S"), status="SKIPPED_MARGIN")
        return None

    stop_pips, tp_pips = compute_sl_tp_pips(sl_usd, tp_usd)
    reward_usd = round(units * tp_usd, 2)

    # ── Spread guard ──────────────────────────────────────────────────────────
    mid, bid, ask = trader.get_price(INSTRUMENT)
    if mid is None:
        alert.send(msg_error("Cannot fetch price", "OANDA pricing endpoint returned None"))
        db.finish_cycle(run_id, status="FAILED", summary={"stage": "pricing"})
        update_runtime_state(last_cycle_finished=now_sgt.strftime("%Y-%m-%d %H:%M:%S"), status="FAILED_PRICING")
        return None

    spread_pips  = round(abs(ask - bid) / 0.01)
    spread_limit = int(settings.get("spread_limits", {}).get(macro, settings.get("max_spread_pips", 150)))
    if spread_pips > spread_limit:
        _send_signal_update("BLOCKED", f"Spread too high ({spread_pips} > {spread_limit} pips)",
                            {"rr_ratio": rr_ratio, "tp_pct": tp_pct, "spread_pips": spread_pips,
                             "spread_limit": spread_limit, "session_ok": True, "news_ok": True, "open_trade_ok": True, "margin_ok": True})
        send_once_per_state(alert, ops, "spread_state", f"spread:{macro}:{spread_pips}",
                            msg_spread_skip(banner, session, spread_pips, spread_limit))
        db.finish_cycle(run_id, status="SKIPPED", summary={"stage": "spread_guard"})
        update_runtime_state(last_cycle_finished=now_sgt.strftime("%Y-%m-%d %H:%M:%S"), status="SKIPPED_SPREAD_GUARD")
        return None

    _send_signal_update("READY", "All must-pass checks satisfied",
                        {"rr_ratio": rr_ratio, "tp_pct": tp_pct, "spread_pips": spread_pips,
                         "spread_limit": spread_limit, "session_ok": True, "news_ok": True, "open_trade_ok": True, "margin_ok": True})

    ctx.update({
        "score": score, "raw_score": raw_score, "direction": direction,
        "details": details, "levels": levels, "position_usd": position_usd,
        "entry": entry, "sl_usd": sl_usd, "tp_usd": tp_usd,
        "rr_ratio": rr_ratio, "units": units, "stop_pips": stop_pips,
        "tp_pips": tp_pips, "reward_usd": reward_usd, "cpr_w": cpr_w,
        "spread_pips": spread_pips, "bid": bid, "ask": ask,
        "margin_available": margin_available, "price_for_margin": price_for_margin,
        "margin_info": margin_info,
    })
    return ctx


def _execution_phase(db, run_id, settings, alert, trader, history, now_sgt, today, demo, ctx):
    """Places the order and persists the trade record."""

    session     = ctx["session"]
    macro       = ctx["macro"]
    banner      = ctx["banner"]
    score       = ctx["score"]
    raw_score   = ctx["raw_score"]
    direction   = ctx["direction"]
    details     = ctx["details"]
    levels      = ctx["levels"]
    position_usd = ctx["position_usd"]
    entry       = ctx["entry"]
    sl_usd      = ctx["sl_usd"]
    tp_usd      = ctx["tp_usd"]
    rr_ratio    = ctx["rr_ratio"]
    units       = ctx["units"]
    stop_pips   = ctx["stop_pips"]
    tp_pips     = ctx["tp_pips"]
    reward_usd  = ctx["reward_usd"]
    cpr_w       = ctx["cpr_w"]
    spread_pips = ctx["spread_pips"]
    bid         = ctx["bid"]
    ask         = ctx["ask"]
    margin_available  = ctx["margin_available"]
    price_for_margin  = ctx["price_for_margin"]
    margin_info       = ctx["margin_info"]
    effective_balance = ctx["effective_balance"]
    news_penalty      = ctx["news_penalty"]

    sl_price, tp_price = compute_sl_tp_prices(entry, direction, sl_usd, tp_usd)

    record = {
        "timestamp_sgt":        now_sgt.strftime("%Y-%m-%d %H:%M:%S"),
        "mode":                 "DEMO" if demo else "LIVE",
        "instrument":           INSTRUMENT,
        "direction":            direction,
        "setup":                levels.get("setup", ""),
        "session":              session,
        "window":               get_window_key(session),
        "macro_session":        macro,
        "score":                score,
        "raw_score":            raw_score,
        "news_penalty":         news_penalty,
        "position_usd":         position_usd,
        "entry":                round(entry, 2),
        "sl_price":             sl_price,
        "tp_price":             tp_price,
        "size":                 units,
        "cpr_width_pct":        cpr_w,
        "sl_usd":               round(sl_usd, 2),
        "tp_usd":               round(tp_usd, 2),
        "estimated_risk_usd":   round(position_usd, 2),
        "estimated_reward_usd": round(reward_usd, 2),
        "spread_pips":          spread_pips,
        "stop_pips":            stop_pips,
        "tp_pips":              tp_pips,
        "levels":               levels,
        "details":              details,
        "trade_id":             None,
        "status":               "FAILED",
        "realized_pnl_usd":     None,
    }

    # ── Place order ───────────────────────────────────────────────────────────
    result = trader.place_order(
        instrument=INSTRUMENT, direction=direction,
        size=units, stop_distance=stop_pips, limit_distance=tp_pips,
        bid=bid, ask=ask,
    )

    if not result.get("success"):
        err = result.get("error", "Unknown")
        retry_attempted = False
        if settings.get("auto_scale_on_margin_reject", True) and "MARGIN" in str(err).upper():
            retry_attempted = True
            retry_safety     = float(settings.get("margin_retry_safety_factor", 0.4))
            retry_specs      = trader.get_instrument_specs(INSTRUMENT)
            retry_margin_rate = max(
                float(retry_specs.get("marginRate", 0.05) or 0.05),
                float(settings.get("xau_margin_rate_override", 0.20) or 0.20) if INSTRUMENT == "XAU_USD" else 0.0,
            )
            retry_units = trader.normalize_units(
                INSTRUMENT,
                (margin_available * retry_safety) / max(price_for_margin * retry_margin_rate, 1e-9),
            )
            if 0 < retry_units < units:
                alert.send(msg_margin_adjustment(
                    instrument=INSTRUMENT,
                    requested_units=units,
                    adjusted_units=retry_units,
                    free_margin=margin_available,
                    required_margin=trader.estimate_required_margin(INSTRUMENT, retry_units, price_for_margin),
                    reason="broker_margin_reject_retry",
                ))
                retry_result = trader.place_order(
                    instrument=INSTRUMENT, direction=direction,
                    size=retry_units, stop_distance=stop_pips, limit_distance=tp_pips,
                    bid=bid, ask=ask,
                )
                if retry_result.get("success"):
                    result = retry_result
                    units  = retry_units
                    record["size"] = units
                    record["estimated_reward_usd"] = round(units * tp_usd, 2)

        if not result.get("success"):
            err = result.get("error", "Unknown")
            alert.send(msg_order_failed(
                direction, INSTRUMENT, units, err,
                free_margin=margin_available,
                required_margin=trader.estimate_required_margin(INSTRUMENT, units, price_for_margin),
                retry_attempted=retry_attempted,
            ))
            log.error("Order failed: %s", err, extra={"run_id": run_id})

    if result.get("success"):
        record["trade_id"] = result.get("trade_id")
        record["status"]   = "FILLED"
        fill_price = result.get("fill_price")
        if fill_price and fill_price > 0:
            actual_entry           = fill_price
            record["entry"]        = round(actual_entry, 2)
            record["signal_entry"] = round(entry, 2)
            record["sl_price"]     = round(actual_entry - sl_usd if direction == "BUY" else actual_entry + sl_usd, 2)
            record["tp_price"]     = round(actual_entry + tp_usd if direction == "BUY" else actual_entry - tp_usd, 2)
        else:
            actual_entry = entry

        alert.send(msg_trade_opened(
            banner=banner, direction=direction, setup=levels.get("setup", ""),
            session=session, fill_price=record["entry"], signal_price=entry,
            sl_price=record["sl_price"], tp_price=record["tp_price"],
            sl_usd=sl_usd, tp_usd=tp_usd, units=units, position_usd=position_usd,
            rr_ratio=rr_ratio, cpr_width_pct=cpr_w, spread_pips=spread_pips,
            score=score, balance=effective_balance, demo=demo,
            news_penalty=news_penalty, raw_score=raw_score,
            free_margin=margin_info.get("free_margin"),
            required_margin=trader.estimate_required_margin(INSTRUMENT, units, price_for_margin),
            margin_mode=("RETRIED" if record["size"] != float(margin_info.get("final_units", record["size"])) else margin_info.get("status", "NORMAL")),
            margin_usage_pct=(
                (trader.estimate_required_margin(INSTRUMENT, units, price_for_margin) / float(margin_info.get("free_margin", 0)) * 100)
                if float(margin_info.get("free_margin", 0)) > 0 else None
            ),
        ))
        log.info("Trade placed: %s", record, extra={"run_id": run_id})

    history.append(record)
    save_history(history)
    db.record_trade_attempt(
        {"pair": INSTRUMENT, "timeframe": "M5", "side": direction, "score": score, **record},
        ok=bool(result.get("success")), note=result.get("error", "trade placed"),
        broker_trade_id=record.get("trade_id"), run_id=run_id,
    )
    db.upsert_state("last_trade_attempt", {
        "run_id": run_id, "success": bool(result.get("success")),
        "trade_id": record.get("trade_id"), "timestamp_sgt": record["timestamp_sgt"],
    })
    update_runtime_state(
        last_cycle_finished=now_sgt.strftime("%Y-%m-%d %H:%M:%S"),
        status="COMPLETED", score=score, direction=direction,
        trade_status=record["status"],
    )
    db.finish_cycle(run_id, status="COMPLETED", summary={
        "signals": 1, "trades_placed": int(bool(result.get("success"))),
        "score": score, "direction": direction, "trade_status": record["status"],
    })


def run_bot_cycle(alert: "TelegramAlert | None" = None):
    """Thin orchestrator — sets up shared objects and delegates to the three phases.

    alert — optional pre-constructed TelegramAlert singleton injected by scheduler.
             If None a fresh instance is created (supports direct script invocation).
    """
    global _startup_reconcile_done

    settings  = validate_settings(load_settings())
    db        = Database()
    demo      = settings.get("demo_mode", True)
    alert     = alert or TelegramAlert()
    history   = load_history()
    now_sgt   = datetime.now(SGT)
    # v1.0: use 08:00 SGT as trading-day boundary so overnight losses (01:xx SGT)
    # count against yesterday's cap, not today's.
    _day_start_hour = int(settings.get("trading_day_start_hour_sgt", 8))
    today     = get_trading_day(now_sgt, _day_start_hour)

    # ── Startup OANDA reconcile (once per process) ─────────────────────────
    # Runs on first cycle after a fresh process start to re-sync today's closed
    # trades before any cap logic fires.  _startup_reconcile_done prevents it
    # running on every cycle after a mid-day redeploy.
    if not _startup_reconcile_done:
        _startup_reconcile_done = True          # set before try — never retries on crash
        try:
            # Construct trader just for the reconcile; the main cycle will
            # construct its own instance inside _guard_phase if it needs one.
            _recon_trader = OandaTrader(demo=demo)
            recon = startup_oanda_reconcile(_recon_trader, history, INSTRUMENT, today, now_sgt)
            if recon["injected"] or recon["backfilled"]:
                save_history(history)
                log.info(
                    "Startup reconcile: injected=%s backfilled=%s — history saved",
                    recon["injected"], recon["backfilled"],
                )
                if recon["injected"]:
                    alert.send(
                        f"♻️ Startup reconcile injected {len(recon['injected'])} missing "
                        f"closed trade(s) into history before first cycle.\n"
                        f"Trade IDs: {', '.join(recon['injected'])}"
                    )
        except Exception as _recon_exc:
            log.warning("Startup reconcile failed (non-fatal): %s", _recon_exc)

    with db.cycle() as run_id:
        try:
            ctx = _guard_phase(db, run_id, settings, alert, history, now_sgt, today, demo)
            if ctx is None:
                return

            ctx = _signal_phase(db, run_id, settings, alert, ctx["trader"], history, now_sgt, today, demo, ctx)
            if ctx is None:
                return

            _execution_phase(db, run_id, settings, alert, ctx["trader"], history, now_sgt, today, demo, ctx)

        except Exception as exc:
            update_runtime_state(last_cycle_finished=now_sgt.strftime("%Y-%m-%d %H:%M:%S"), status="FAILED", error=str(exc))
            raise


def main():
    return run_bot_cycle()


if __name__ == "__main__":
    main()
