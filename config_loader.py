from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get('DATA_DIR', '/data')).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_SETTINGS_PATH = BASE_DIR / 'settings.json'
SETTINGS_FILE = DATA_DIR / 'settings.json'
SECRETS_JSON_PATH = BASE_DIR / 'secrets.json'

# v1.2.4: run-once guard — ensure_persistent_settings only syncs once per
# process lifetime.  Previously it re-ran on every load_settings() call
# because writing SETTINGS_FILE changed its mtime and invalidated the cache.
_settings_synced: bool = False


def _read_json(path: Path, default: Any = None) -> Any:
    try:
        if path.exists():
            with path.open('r', encoding='utf-8') as f:
                return json.load(f)
    except Exception as exc:
        logger.warning('Failed to read %s: %s', path, exc)
    return default


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    with tmp.open('w', encoding='utf-8') as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp, path)


def ensure_persistent_settings() -> Path:
    global _settings_synced
    if _settings_synced:
        return SETTINGS_FILE  # already ran this process — skip

    # Always read the bundled defaults shipped with the code.
    default_settings = _read_json(DEFAULT_SETTINGS_PATH, {})
    if not isinstance(default_settings, dict):
        default_settings = {}

    # v1.2.4 safety: if the bundled settings.json couldn't be read (e.g. path
    # resolution issue in some Railway container layouts), do NOT overwrite the
    # volume with an empty dict.  Log a warning and leave the volume as-is so
    # the bot continues to run with whatever is on the volume.
    if not default_settings:
        logger.warning(
            'Bundled settings.json not found or empty at %s — '
            'volume settings left unchanged.',
            DEFAULT_SETTINGS_PATH,
        )
        _settings_synced = True
        return SETTINGS_FILE

    # v1.2.3/v1.2.4: ALWAYS overwrite the volume settings with the bundled
    # settings.json on every startup (first time only per process).
    # The Railway volume stores trade state — not configuration.
    if SETTINGS_FILE.exists():
        old_settings = _read_json(SETTINGS_FILE, {})
        old_name = old_settings.get('bot_name', 'unknown') if isinstance(old_settings, dict) else 'unknown'
    else:
        old_name = 'none'

    _write_json(SETTINGS_FILE, default_settings)
    _settings_synced = True
    new_name = default_settings.get('bot_name', 'unknown')
    if old_name != new_name:
        logger.info('Settings synced on startup: %s → %s', old_name, new_name)
    else:
        logger.info('Settings synced on startup: %s (refreshed from bundle)', new_name)
    return SETTINGS_FILE


# ── load_settings cache (M-06 fix) ────────────────────────────────────────────
# Avoids re-reading disk on every call. Cache is invalidated when the file's
# modification time changes — so manual edits to settings.json take effect
# on the very next cycle without restarting the bot.
_settings_cache: dict = {}
_settings_mtime: float = 0.0


def load_settings() -> dict:
    global _settings_cache, _settings_mtime
    ensure_persistent_settings()

    try:
        mtime = SETTINGS_FILE.stat().st_mtime
    except OSError:
        mtime = 0.0

    if _settings_cache and mtime == _settings_mtime:
        return _settings_cache  # file unchanged — skip disk read

    settings = _read_json(SETTINGS_FILE, {})
    if not isinstance(settings, dict):
        settings = {}

    original_keys = set(settings.keys())

    settings.setdefault('bot_name', 'RF Scalp Bot')
    settings.setdefault('enabled', True)
    settings.setdefault('cycle_minutes', 5)
    settings.setdefault('db_retention_days', 90)
    settings.setdefault('db_cleanup_hour_sgt', 0)
    settings.setdefault('db_cleanup_minute_sgt', 15)
    settings.setdefault('db_vacuum_weekly', True)
    settings.setdefault('calendar_fetch_interval_min', 60)
    settings.setdefault('calendar_retry_after_min', 15)

    # ── Keys required by validate_settings() in bot.py ──────────────────────
    # Guard against old persistent settings.json files that pre-date these
    # fields being made mandatory.  Setting defaults here ensures the file is
    # patched on the very first load after a deployment, so the bot never
    # crashes with "Missing required settings keys" regardless of how old the
    # volume's settings.json is.
    settings.setdefault('spread_limits', {'London': 130, 'US': 130})
    settings.setdefault('max_trades_day', 20)
    settings.setdefault('max_losing_trades_day', 8)
    settings.setdefault('max_trades_london', 10)
    settings.setdefault('max_trades_us', 10)
    settings.setdefault('max_losing_trades_session', 4)
    settings.setdefault('sl_mode', 'pct_based')
    settings.setdefault('tp_mode', 'rr_multiple')
    settings.setdefault('rr_ratio', 2.5)

    if set(settings.keys()) != original_keys:
        _write_json(SETTINGS_FILE, settings)

    _settings_cache = settings
    _settings_mtime = mtime
    return settings


def save_settings(settings: dict) -> None:
    _write_json(SETTINGS_FILE, settings)
    logger.info('Saved settings -> %s', SETTINGS_FILE)


def load_secrets() -> dict:
    """Load secrets with environment variables taking priority over secrets.json."""
    file_secrets: dict = {}
    if SECRETS_JSON_PATH.exists():
        loaded = _read_json(SECRETS_JSON_PATH, {})
        if isinstance(loaded, dict):
            file_secrets = loaded

    return {
        'OANDA_API_KEY':    os.environ.get('OANDA_API_KEY')    or file_secrets.get('OANDA_API_KEY',    ''),
        'OANDA_ACCOUNT_ID': os.environ.get('OANDA_ACCOUNT_ID') or file_secrets.get('OANDA_ACCOUNT_ID', ''),
        'TELEGRAM_TOKEN':   os.environ.get('TELEGRAM_TOKEN')   or file_secrets.get('TELEGRAM_TOKEN',   ''),
        'TELEGRAM_CHAT_ID': os.environ.get('TELEGRAM_CHAT_ID') or file_secrets.get('TELEGRAM_CHAT_ID', ''),
        'DATA_DIR':         str(DATA_DIR),
    }


def get_bool_env(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {'1', 'true', 'yes', 'on'}
