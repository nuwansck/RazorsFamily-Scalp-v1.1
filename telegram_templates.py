"""Telegram message templates for RF Scalp Bot v1.0

Session schedule (SGT):
  00:00 - 00:59   US Window (NY morning continuation)
  01:00 - 15:59   Dead zone -- no new entries
  16:00 - 20:59   London Window (08:00-13:00 GMT)
  21:00 - 23:59   US Window (13:00-16:00 EDT)
"""

from __future__ import annotations

_DIV = "─" * 22


def _position_label(position_usd: int) -> str:
    if position_usd >= 100:
        return f"${position_usd} 🟢 Full"
    if position_usd >= 66:
        return f"${position_usd} 🟡 Partial"
    return "No trade"


# ── 1. Signal update ──────────────────────────────────────────────────────────

def _check_line(label: str, ok: bool | None, detail: str = "") -> str:
    icon = "✅" if ok is True else ("❌" if ok is False else "•")
    spacer = " " * max(1, 14 - len(label))
    suffix = f"  {detail}" if detail else ""
    return f"{icon} {label}{spacer}{suffix}"


def _render_check_section(title: str, checks: list[tuple[str, bool | None, str]] | None) -> str:
    if not checks:
        return f"{title}\n• None\n"
    body = "\n".join(_check_line(*c) for c in checks)
    return f"{title}\n{body}\n"


def msg_signal_update(
    banner: str,
    session: str,
    direction: str,
    score: int,
    position_usd: int,
    cpr_width_pct: float,
    detail_lines: list[str],
    news_penalty: int = 0,
    raw_score: int | None = None,
    decision: str = "WATCHING",
    reason: str = "Watching for valid breakout",
    mandatory_checks: list[tuple[str, bool | None, str]] | None = None,
    quality_checks: list[tuple[str, bool | None, str]] | None = None,
    execution_checks: list[tuple[str, bool | None, str]] | None = None,
    cycle_minutes: int = 5,
) -> str:
    news_line = f"📰 News penalty active ({news_penalty})\n" if news_penalty else ""
    score_str = f"{score}/6"
    if raw_score is not None and news_penalty:
        score_str += f"  (raw {raw_score}, news {news_penalty:+d})"
    details = "\n".join(f"  {r}" for r in detail_lines[:4]) or "  No signal notes"
    mandatory_text = _render_check_section("Mandatory checks", mandatory_checks)
    quality_text   = _render_check_section("Quality checks",   quality_checks)
    execution_text = _render_check_section("Execution checks", execution_checks)
    return (
        f"{banner} SESSION\n"
        f"📊 Scalp Signal Update\n{_DIV}\n"
        f"Window:    {session}\n"
        f"Bias:      {direction}\n"
        f"Score:     {score_str}\n"
        f"Position:  {_position_label(position_usd)}\n"
        f"Strategy:  EMA9/21 + ORB | CPR bias: {cpr_width_pct:.2f}%\n"
        f"Decision:  {decision}\n"
        f"Reason:    {reason}\n"
        f"{_DIV}\n"
        f"{mandatory_text}"
        f"{_DIV}\n"
        f"{quality_text}"
        f"{_DIV}\n"
        f"{execution_text}"
        f"{_DIV}\n"
        f"Signal notes\n"
        f"{details}\n"
        f"{_DIV}\n"
        f"{news_line}"
        f"Next cycle in {cycle_minutes} min"
    )


# ── 2. New trade opened ───────────────────────────────────────────────────────

def msg_trade_opened(
    banner: str,
    direction: str,
    setup: str,
    session: str,
    fill_price: float,
    signal_price: float,
    sl_price: float,
    tp_price: float,
    sl_usd: float,
    tp_usd: float,
    units: float,
    position_usd: int,
    rr_ratio: float,
    cpr_width_pct: float,
    spread_pips: int,
    score: int,
    balance: float,
    demo: bool,
    news_penalty: int = 0,
    raw_score: int | None = None,
    free_margin: float | None = None,
    required_margin: float | None = None,
    margin_mode: str = "NORMAL",
    margin_usage_pct: float | None = None,
) -> str:
    slip     = fill_price - signal_price
    slip_str = f"  (signal ${signal_price:.2f}, slip ${slip:+.2f})" if abs(slip) > 0.005 else ""
    score_str = f"{score}/6"
    if raw_score is not None and news_penalty:
        score_str += f"  (raw {raw_score}, news {news_penalty:+d})"
    mode = "DEMO" if demo else "LIVE"
    return (
        f"{banner} 🥇 New Trade — {direction}\n{_DIV}\n"
        f"Setup:    {setup}\n"
        f"Window:   {session}\n"
        f"Fill:     ${fill_price:.2f}{slip_str}\n"
        f"SL:       ${sl_price:.2f}  (-${sl_usd:.2f})\n"
        f"TP:       ${tp_price:.2f}  (+${tp_usd:.2f})\n"
        f"Units:    {units}\n"
        f"Position: {_position_label(position_usd)}  (1:{rr_ratio:.0f})\n"
        f"EMA:      CPR bias={cpr_width_pct:.2f}% | Spread: {spread_pips} pips\n"
        f"Score:    {score_str}\n"
        f"Balance:  ${balance:.2f}\n"
        f"Mode:     {mode}"
    )


# ── 3. Break-even activated ───────────────────────────────────────────────────

def msg_breakeven(
    trade_id: str | int,
    direction: str,
    entry: float,
    trigger_price: float,
    trigger_usd: float,
    current_price: float,
    unrealized_pnl: float,
    demo: bool,
) -> str:
    mode = "DEMO" if demo else "LIVE"
    return (
        f"🔒 Break-Even Activated\n{_DIV}\n"
        f"Trade ID:  {trade_id}\n"
        f"Direction: {direction}\n"
        f"Entry:     ${entry:.2f}\n"
        f"Trigger:   ${trigger_price:.2f} (+${trigger_usd:.2f} move)\n"
        f"Price now: ${current_price:.2f}\n"
        f"PnL now:   ${unrealized_pnl:+.2f}\n"
        f"SL moved → entry (${entry:.2f})\n"
        f"Mode:      {mode}"
    )


# ── 4. Trade closed ───────────────────────────────────────────────────────────

def msg_trade_closed(
    trade_id: str | int,
    direction: str,
    setup: str,
    entry: float,
    close_price: float,
    pnl: float,
    session: str,
    demo: bool,
    duration_str: str = "",
) -> str:
    outcome = "WIN" if pnl > 0 else ("LOSS" if pnl < 0 else "BREAKEVEN")
    icon    = "✅" if pnl > 0 else ("❌" if pnl < 0 else "➡️")
    duration_line = f"Duration:  {duration_str}\n" if duration_str else ""
    mode    = "DEMO" if demo else "LIVE"
    return (
        f"{icon} Trade Closed — {outcome}\n{_DIV}\n"
        f"Trade ID:  {trade_id}\n"
        f"Direction: {direction}\n"
        f"Setup:     {setup}\n"
        f"Entry:     ${entry:.2f}\n"
        f"Close:     ${close_price:.2f}\n"
        f"PnL:       ${pnl:+.2f}\n"
        f"{duration_line}"
        f"Session:   {session}\n"
        f"Mode:      {mode}"
    )


# ── 5. News hard block ────────────────────────────────────────────────────────

def msg_news_block(event_name: str, event_time_sgt: str, before_min: int, after_min: int) -> str:
    return (
        f"📰 News Block Active\n{_DIV}\n"
        f"Event:   {event_name}\n"
        f"Time:    {event_time_sgt} SGT\n"
        f"Window:  -{before_min}min → +{after_min}min\n"
        f"Action:  Hard block — no new entries\n"
        f"{_DIV}\n"
        f"⏳ Resuming {after_min} min after event"
    )


# ── 6. News soft penalty ──────────────────────────────────────────────────────

def msg_news_penalty(
    event_names: list[str],
    penalty: int,
    score_after: int,
    score_before: int,
    position_after: int,
    position_before: int,
) -> str:
    names = ", ".join(event_names) if event_names else "Medium event"
    count = len(event_names) if event_names else 1
    pos_change = (
        f"${position_before} → ${position_after}"
        if position_before != position_after
        else f"${position_after} (unchanged)"
    )
    return (
        f"📰 Soft News Penalty Active\n{_DIV}\n"
        f"Events:   {names}\n"
        f"Count:    {count} medium event(s)\n"
        f"Penalty:  {penalty} applied to score\n"
        f"Score:    {score_before}/6 → {score_after}/6\n"
        f"Position: {pos_change}\n"
        f"{_DIV}\n"
        f"{'⚠️ Trading continues with reduced size' if position_after > 0 else '⏳ Score below minimum — watching'}"
    )


# ── 7. Loss cooldown started ──────────────────────────────────────────────────

def msg_cooldown_started(
    streak: int,
    cooldown_until_sgt: str,
    session_name: str = "",
    day_losses: int = 0,
    day_limit: int = 3,
) -> str:
    remaining = max(0, day_limit - day_losses)
    session_line   = f"Session:  {session_name}\n"               if session_name else ""
    remaining_line = (
        f"Day stop: {remaining} more loss triggers full day block\n"
        if remaining == 1
        else f"Day stop: {remaining} more losses trigger full day block\n"
    )
    return (
        f"🧊 Cooldown Started\n{_DIV}\n"
        f"Reason:   {streak} consecutive losses\n"
        f"{session_line}"
        f"Paused:   New entries only\n"
        f"Resumes:  {cooldown_until_sgt} SGT\n"
        f"{remaining_line}"
        f"{_DIV}\n"
        f"Existing trades continue to be managed"
    )


# ── 8. Daily / window cap reached — enriched ─────────────────────────────────

def msg_daily_cap(
    cap_type: str,
    count: int,
    limit: int,
    window: str = "",
    daily_pnl: float | None = None,
    session_name: str = "",
    last_loss_time_sgt: str = "",
    reset_time_sgt: str = "",
) -> str:
    if cap_type == "losing_trades":
        label  = "Max losing trades"
        action = "No new entries this trading day"
        footer = "Bot resumes next trading day"
    elif cap_type == "total_trades":
        label  = "Max trades/day"
        action = "No new entries this trading day"
        footer = "Bot resumes next trading day"
    else:
        label  = f"{window} window cap"
        action = f"No new entries in {window} window"
        footer = "Entries resume next window"

    pnl_line        = f"Day P&L:   ${daily_pnl:+.2f}\n"          if daily_pnl is not None else ""
    session_line    = f"Session:   {session_name}\n"               if session_name else ""
    last_loss_line  = f"Last loss: {last_loss_time_sgt} SGT\n"     if last_loss_time_sgt else ""
    window_line     = "Window:    16:00 → 01:00 SGT (London + US)\n"
    reset_line      = f"Resets:    {reset_time_sgt}\n"             if reset_time_sgt else ""

    return (
        f"🛑 Daily Cap Reached\n{_DIV}\n"
        f"Type:    {label}\n"
        f"Count:   {count}/{limit}\n"
        f"{pnl_line}"
        f"{session_line}"
        f"{last_loss_line}"
        f"{window_line}"
        f"{reset_line}"
        f"Action:  {action}\n"
        f"{_DIV}\n"
        f"{footer}"
    )


# ── 8b. New trading day — loss cap reset ──────────────────────────────────────

def msg_new_day_resume(
    prev_day_pnl: float | None = None,
    prev_day_trades: int = 0,
    london_open_sgt: str = "16:00",
) -> str:
    prev_line = ""
    if prev_day_trades > 0 and prev_day_pnl is not None:
        prev_line = f"Yesterday: {prev_day_trades} trade(s)  ${prev_day_pnl:+.2f}\n"
    return (
        f"✅ New Trading Day\n{_DIV}\n"
        f"Daily limits reset\n"
        f"{prev_line}"
        f"Next session: London {london_open_sgt} SGT\n"
        f"Day reset:    08:00 SGT\n"
        f"{_DIV}\n"
        f"Bot resuming — monitoring for setups"
    )


# ── 8c. Session loss sub-cap hit ──────────────────────────────────────────────

def msg_session_cap(
    session_name: str,
    session_losses: int,
    session_limit: int,
    day_losses: int,
    day_limit: int,
    next_session: str,
) -> str:
    icon           = "🇬🇧" if "London" in session_name else "🗽"
    remaining_day  = max(0, day_limit - day_losses)
    next_icon      = "🗽" if "US" in next_session else "🇬🇧"
    remaining_line = (
        f"{remaining_day} loss remaining today before full day stop"
        if remaining_day == 1
        else f"{remaining_day} losses remaining today before full day stop"
    )
    return (
        f"🔶 Session Cap — {session_name}\n{_DIV}\n"
        f"{icon} Session losses: {session_losses}/{session_limit}  (session paused)\n"
        f"📊 Day losses:     {day_losses}/{day_limit}  ({remaining_line})\n"
        f"{_DIV}\n"
        f"Next session: {next_icon} {next_session}\n"
        f"Existing trades continue to be managed"
    )


# ── 9. Session window opened ──────────────────────────────────────────────────

def msg_session_open(
    session_name: str,
    session_hours_sgt: str,
    trade_cap: int,
    trades_today: int,
    daily_pnl: float,
) -> str:
    icon = "🇬🇧" if "London" in session_name else "🗽"
    return (
        f"{icon} {session_name} Open\n{_DIV}\n"
        f"Hours:     {session_hours_sgt} SGT\n"
        f"Cap:       {trade_cap} trades this window\n"
        f"Today:     {trades_today} trade(s) so far  ${daily_pnl:+.2f}\n"
        f"{_DIV}\n"
        f"Scanning for EMA + ORB scalp setups..."
    )


# ── 10. Spread too wide ───────────────────────────────────────────────────────

def msg_spread_skip(banner: str, session_label: str, spread_pips: int, limit_pips: int) -> str:
    excess = spread_pips - limit_pips
    return (
        f"⚠️ Spread Too Wide — Skipping\n{_DIV}\n"
        f"Session:  {session_label}\n"
        f"Spread:   {spread_pips} pips\n"
        f"Limit:    {limit_pips} pips  (+{excess} over)\n"
        f"{_DIV}\n"
        f"Waiting for spread to normalise"
    )


# ── 11. Order placement failed ────────────────────────────────────────────────

def msg_order_failed(
    direction: str,
    instrument: str,
    units: float,
    error: str,
    free_margin: float | None = None,
    required_margin: float | None = None,
    retry_attempted: bool = False,
) -> str:
    margin_line = (
        f"Margin:    free=${free_margin:.2f}  req=${required_margin:.2f}\n"
        if free_margin is not None and required_margin is not None else ""
    )
    return (
        f"❌ Order Failed\n{_DIV}\n"
        f"Direction: {direction}\n"
        f"Pair:      {instrument}\n"
        f"Units:     {units}\n"
        f"Error:     {error}\n"
        f"{margin_line}"
        f"Retry:     {'attempted' if retry_attempted else 'not attempted'}\n"
        f"{_DIV}\n"
        f"Check OANDA account and logs"
    )


# ── 11b. Margin auto-scale / skip ─────────────────────────────────────────────

def msg_margin_adjustment(
    instrument: str,
    requested_units: float,
    adjusted_units: float,
    free_margin: float,
    required_margin: float,
    reason: str,
) -> str:
    action = "Skipping trade" if adjusted_units <= 0 else "Using smaller size"
    return (
        f"⚠️ Margin Protection\n{_DIV}\n"
        f"Pair:      {instrument}\n"
        f"Requested: {requested_units}\n"
        f"Adjusted:  {adjusted_units}\n"
        f"Free Mgn:  ${free_margin:.2f}\n"
        f"Req Mgn:   ${required_margin:.2f}\n"
        f"Reason:    {reason}\n"
        f"{_DIV}\n"
        f"{action}"
    )


# ── 12. System errors ─────────────────────────────────────────────────────────

def msg_error(error_type: str, detail: str = "") -> str:
    detail_line = f"Detail:  {detail}\n" if detail else ""
    return (
        f"❌ System Error\n{_DIV}\n"
        f"Type:    {error_type}\n"
        f"{detail_line}"
        f"{_DIV}\n"
        f"Check logs for full trace"
    )


# ── 13. Friday cutoff ─────────────────────────────────────────────────────────

def msg_friday_cutoff(cutoff_hour_sgt: int) -> str:
    return (
        f"📅 Friday Cutoff Active\n{_DIV}\n"
        f"Time:    After {cutoff_hour_sgt:02d}:00 SGT Friday\n"
        f"Action:  No new entries\n"
        f"Reason:  Low gold liquidity end-of-week\n"
        f"{_DIV}\n"
        f"Bot resumes Monday 16:00 SGT (London open)"
    )


# ── 14. Bot startup — includes session schedule ───────────────────────────────

def msg_startup(
    version: str,
    mode: str,
    balance: float,
    min_score: int,
    cycle_minutes: int = 5,
    max_trades_london: int = 4,
    max_trades_us: int = 4,
    max_losing_day: int = 3,
    trading_day_start_hour: int = 8,
) -> str:
    return (
        f"🚀 Bot Started — {version}\n{_DIV}\n"
        f"Mode:      {mode}\n"
        f"Balance:   ${balance:.2f}\n"
        f"Min score: {min_score}/6 to trade\n"
        f"Pair:      XAU/USD (M5 scalp)\n"
        f"Sizes:     $66 (score 4) | $100 (score 5–6)\n"
        f"{_DIV}\n"
        f"Session schedule (SGT)\n"
        f"  🗽 00:00–00:59  US cont.     cap {max_trades_us}\n"
        f"  💤 01:00–15:59  Dead zone\n"
        f"  🇬🇧 16:00–20:59  London       cap {max_trades_london}\n"
        f"  🗽 21:00–23:59  US session   cap {max_trades_us}\n"
        f"{_DIV}\n"
        f"Window:    16:00 → 01:00 SGT (London + US)\n"
        f"Day reset: {trading_day_start_hour:02d}:00 SGT\n"
        f"Daily loss cap: {max_losing_day} losing trades\n"
        f"Cycle: every {cycle_minutes} min ✅"
    )


# ── 15. Daily performance report ─────────────────────────────────────────────

def _pnl_icon(pnl: float) -> str:
    return "🟢" if pnl > 0 else ("🔴" if pnl < 0 else "⬜")


def _mini_stats(stats: dict) -> str:
    if stats["count"] == 0:
        return "No closed trades"
    return (
        f"{stats['count']} trades  {stats['wins']}W/{stats['losses']}L"
        f"  ${stats['net_pnl']:+.2f}  WR {stats['win_rate']:.0f}%"
    )


def msg_daily_report(
    day_label: str,
    day_stats: dict,
    wtd_stats: dict,
    mtd_stats: dict,
    open_count: int,
    report_time: str,
    blocked_spread: int = 0,
    blocked_news: int = 0,
    blocked_signal: int = 0,
) -> str:
    icon       = _pnl_icon(day_stats["net_pnl"]) if day_stats["count"] > 0 else "📋"
    r_line     = f"  Avg R:    {day_stats['avg_r']}R\n" if day_stats.get("avg_r") is not None else ""
    open_line  = f"Open now:  {open_count} position(s)\n" if open_count > 0 else ""
    pf_val     = day_stats.get("profit_factor")
    pf_line    = f"  P.Factor: {pf_val}\n" if pf_val is not None else ""
    best       = day_stats.get("best_trade")
    worst      = day_stats.get("worst_trade")
    best_line  = f"  Best:     ${best['pnl']:+.2f}  ({best['time']} SGT)\n"  if best  else ""
    worst_line = f"  Worst:    ${worst['pnl']:+.2f}  ({worst['time']} SGT)\n" if worst else ""

    # Blocked cycles breakdown
    blocked_parts = []
    if blocked_spread:
        blocked_parts.append(f"{blocked_spread} spread")
    if blocked_news:
        blocked_parts.append(f"{blocked_news} news")
    if blocked_signal:
        blocked_parts.append(f"{blocked_signal} signal-only")
    blocked_line = f"Blocked:   {', '.join(blocked_parts)}\n" if blocked_parts else ""

    prev_cap_line = "⚠️ Yesterday hit daily loss cap\n" if day_stats.get("ended_on_loss_cap") else ""

    return (
        f"{icon} Daily Report — {day_label}\n{_DIV}\n"
        f"{prev_cap_line}"
        f"Yesterday\n"
        f"  Trades:   {day_stats['count']}  ({day_stats['wins']}W / {day_stats['losses']}L)\n"
        f"  Net PnL:  ${day_stats['net_pnl']:+.2f}\n"
        f"{pf_line}"
        f"{r_line}"
        f"{best_line}"
        f"{worst_line}"
        f"{blocked_line}"
        f"{_DIV}\n"
        f"Week-to-date\n"
        f"  {_mini_stats(wtd_stats)}\n"
        f"{_DIV}\n"
        f"Month-to-date\n"
        f"  {_mini_stats(mtd_stats)}\n"
        f"{_DIV}\n"
        f"{open_line}"
        f"London session opens at 16:00 SGT\n"
        f"Report: {report_time}"
    )


# ── 16. Weekly performance report ────────────────────────────────────────────

def _ascii_bar(value: float, max_val: float, width: int = 10) -> str:
    if max_val <= 0:
        return "░" * width
    filled = int(round(value / max_val * width))
    return "█" * filled + "░" * (width - filled)


def msg_weekly_report(week_label: str, stats: dict, sessions: dict, setups: dict, report_time: str) -> str:
    if stats["count"] == 0:
        return f"📅 Weekly Report — {week_label}\n{_DIV}\nNo closed trades last week.\nReport: {report_time}"

    pf_str = f"{stats['profit_factor']}" if stats["profit_factor"] is not None else "n/a"
    r_line = f"Avg R:       {stats['avg_r']}R\n" if stats.get("avg_r") is not None else ""
    icon   = _pnl_icon(stats["net_pnl"])

    sess_lines = ""
    if sessions:
        max_wr = max(s["win_rate"] for s in sessions.values()) or 1
        for name, s in sessions.items():
            bar = _ascii_bar(s["win_rate"], max_wr)
            sess_lines += f"  {name:<8} {bar} {s['win_rate']:>5.1f}%  ${s['net_pnl']:+.2f}  ({s['count']}t)\n"

    setup_lines = ""
    if setups:
        max_wr = max(s["win_rate"] for s in setups.values()) or 1
        for name, s in setups.items():
            bar = _ascii_bar(s["win_rate"], max_wr)
            setup_lines += f"  {name[:18]:<18} {bar} {s['win_rate']:>5.1f}%\n"

    pf_val = stats["profit_factor"] or 0
    wr_val = stats["win_rate"]
    n      = stats["count"]
    if n < 10:
        verdict = f"⚠️ Small sample ({n} trades) — not enough for conclusions"
    elif pf_val >= 1.3 and wr_val >= 48:
        verdict = f"✅ Healthy week — PF {pf_val}  WR {wr_val}%"
    elif pf_val >= 1.0:
        verdict = f"🟡 Marginal — PF {pf_val}  WR {wr_val}%  Monitor closely"
    else:
        verdict = f"🔴 Negative week — PF {pf_val}  WR {wr_val}%  Review before next week"

    return (
        f"📅 Weekly Report — {week_label}\n{_DIV}\n"
        f"{icon} Overview\n"
        f"Trades:      {stats['count']}  ({stats['wins']}W / {stats['losses']}L)\n"
        f"Net PnL:     ${stats['net_pnl']:+.2f}\n"
        f"Win rate:    {stats['win_rate']}%\n"
        f"Prof factor: {pf_str}\n"
        f"{r_line}"
        f"Streaks:     {stats['max_win_streak']}W / {stats['max_loss_streak']}L max\n"
        + (f"Best trade:  ${stats['best_trade']['pnl']:+.2f}  ({stats['best_trade']['time']} SGT)\n" if stats.get("best_trade") else "")
        + (f"Worst trade: ${stats['worst_trade']['pnl']:+.2f}  ({stats['worst_trade']['time']} SGT)\n" if stats.get("worst_trade") else "")
        + f"{_DIV}\nBy Session\n{sess_lines}{_DIV}\nBy Setup\n{setup_lines}{_DIV}\n{verdict}\nReport: {report_time}"
    )


# ── 17. Monthly performance report ───────────────────────────────────────────

def msg_monthly_report(
    month_label: str,
    stats: dict,
    sessions: dict,
    setups: dict,
    scores: dict,
    mom_delta: float | None,
    prior_month_pnl: float | None,
    report_time: str,
) -> str:
    if stats["count"] == 0:
        return f"📆 Monthly Report — {month_label}\n{_DIV}\nNo closed trades last month.\nReport: {report_time}"

    icon   = _pnl_icon(stats["net_pnl"])
    pf_str = f"{stats['profit_factor']}" if stats["profit_factor"] is not None else "n/a"
    r_line = f"Avg R:         {stats['avg_r']}R\n" if stats.get("avg_r") is not None else ""

    mom_line = ""
    if mom_delta is not None and prior_month_pnl is not None:
        delta_icon = "🟢" if mom_delta >= 0 else "🔴"
        mom_line = f"vs prior month: ${prior_month_pnl:+.2f}  →  {delta_icon} {mom_delta:+.2f}\n"

    sess_lines = ""
    if sessions:
        max_wr = max(s["win_rate"] for s in sessions.values()) or 1
        for name, s in sessions.items():
            bar = _ascii_bar(s["win_rate"], max_wr)
            sess_lines += f"  {name:<8} {bar} {s['win_rate']:>5.1f}%  ${s['net_pnl']:+.2f}  ({s['count']}t)\n"

    setup_lines = ""
    if setups:
        max_wr = max(s["win_rate"] for s in setups.values()) or 1
        for name, s in setups.items():
            bar = _ascii_bar(s["win_rate"], max_wr)
            setup_lines += f"  {name[:18]:<18} {bar} {s['win_rate']:>5.1f}%  ({s['count']}t)\n"

    score_lines = ""
    if scores:
        max_wr = max(s["win_rate"] for s in scores.values()) or 1
        for sc, s in scores.items():
            bar = _ascii_bar(s["win_rate"], max_wr)
            score_lines += f"  Score {sc}  {bar} {s['win_rate']:>5.1f}%  ({s['count']}t)\n"

    pf_val = stats["profit_factor"] or 0
    wr_val = stats["win_rate"]
    n      = stats["count"]
    if n < 20:
        verdict        = f"⚠️ Small sample ({n} trades) — collect more data before changes"
        recommendation = "Hold current settings. No changes yet."
    elif pf_val >= 1.3 and wr_val >= 48:
        verdict        = f"✅ Healthy month — PF {pf_val}  WR {wr_val}%"
        recommendation = "System performing well. No changes needed."
    elif pf_val >= 1.0:
        verdict        = f"🟡 Marginal month — PF {pf_val}  WR {wr_val}%"
        recommendation = "Consider raising signal_threshold by +1 or reducing position sizes."
    else:
        verdict        = f"🔴 Negative month — PF {pf_val}  WR {wr_val}%"
        recommendation = "Review session/setup breakdown above. Consider pausing worst session."

    return (
        f"📆 Monthly Report — {month_label}\n{_DIV}\n"
        f"{icon} Overview\n"
        f"Trades:        {stats['count']}  ({stats['wins']}W / {stats['losses']}L)\n"
        f"Net PnL:       ${stats['net_pnl']:+.2f}\n"
        f"{mom_line}"
        f"Win rate:      {wr_val}%\n"
        f"Prof factor:   {pf_str}\n"
        f"{r_line}"
        f"Gross P:       ${stats['gross_profit']:.2f}\n"
        f"Gross L:       ${stats['gross_loss']:.2f}\n"
        f"Streaks:       {stats['max_win_streak']}W / {stats['max_loss_streak']}L max\n"
        + (f"Best trade:    ${stats['best_trade']['pnl']:+.2f}  ({stats['best_trade']['time']} SGT)\n" if stats.get("best_trade") else "")
        + (f"Worst trade:   ${stats['worst_trade']['pnl']:+.2f}  ({stats['worst_trade']['time']} SGT)\n" if stats.get("worst_trade") else "")
        + f"{_DIV}\nBy Session\n{sess_lines}{_DIV}\nBy Setup\n{setup_lines}{_DIV}\nBy Score\n{score_lines}{_DIV}\n"
        f"{verdict}\n💡 {recommendation}\n{_DIV}\nReport: {report_time}"
    )
