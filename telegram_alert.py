"""
Telegram Alert System — RF Scalp Bot v1.0

Retries up to 3 times on 5xx errors with exponential backoff.
HTTP 429 (rate-limit) respects the Retry-After header.
4xx errors (bad token, bad chat_id) are NOT retried — they are config errors.
"""
import logging
import time

import requests

from config_loader import load_secrets

log = logging.getLogger(__name__)

_MAX_RETRIES  = 3
_RETRY_DELAYS = (2, 5)   # seconds between attempt 1→2 and 2→3


class TelegramAlert:
    def __init__(self):
        secrets      = load_secrets()
        self.token   = secrets.get("TELEGRAM_TOKEN", "")
        self.chat_id = secrets.get("TELEGRAM_CHAT_ID", "")

    def send(self, message: str) -> bool:
        if not self.token or not self.chat_id:
            log.warning("Telegram not configured.")
            return False

        url  = f"https://api.telegram.org/bot{self.token}/sendMessage"
        text = f"🤖 RF Scalp v1.0\n{'─' * 22}\n{message}"

        for attempt in range(_MAX_RETRIES):
            try:
                r = requests.post(
                    url,
                    data={"chat_id": self.chat_id, "text": text},
                    timeout=10,
                )
                if r.status_code == 200:
                    if attempt:
                        log.info("Telegram sent (attempt %d).", attempt + 1)
                    else:
                        log.info("Telegram sent!")
                    return True

                # 429 — rate limited: respect Retry-After header and retry
                if r.status_code == 429:
                    retry_after = int(r.headers.get("Retry-After", 5))
                    log.warning(
                        "Telegram rate-limited (429) — waiting %ds before retry (attempt %d/%d).",
                        retry_after, attempt + 1, _MAX_RETRIES,
                    )
                    time.sleep(retry_after)
                    continue  # retry this attempt

                # Other 4xx = config/auth error — no point retrying
                if r.status_code < 500:
                    log.warning(
                        "Telegram %s (no retry): %s",
                        r.status_code, r.text[:200],
                    )
                    return False

                # 5xx = transient server error — retry with backoff
                log.warning(
                    "Telegram 5xx (attempt %d/%d): HTTP %s",
                    attempt + 1, _MAX_RETRIES, r.status_code,
                )
                if attempt < len(_RETRY_DELAYS):
                    time.sleep(_RETRY_DELAYS[attempt])

            except requests.RequestException as exc:
                log.warning(
                    "Telegram network error (attempt %d/%d): %s",
                    attempt + 1, _MAX_RETRIES, exc,
                )
                if attempt < len(_RETRY_DELAYS):
                    time.sleep(_RETRY_DELAYS[attempt])

        log.error("Telegram failed after %d attempts.", _MAX_RETRIES)
        return False
