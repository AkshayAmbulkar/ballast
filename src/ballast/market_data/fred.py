"""FRED (Federal Reserve Bank of St. Louis) client for interest-rate series.

API reference: https://fred.stlouisfed.org/docs/api/fred/series_observations.html
A free API key is required: https://fred.stlouisfed.org/docs/api/api_key.html
"""

from __future__ import annotations

import time
from datetime import date
from decimal import Decimal, InvalidOperation

import requests

FRED_OBSERVATIONS_URL = "https://api.stlouisfed.org/fred/series/observations"
_RETRY_STATUSES = {429, 500, 502, 503, 504}


class FredError(RuntimeError):
    pass


def parse_observations(payload: dict) -> list[tuple[date, Decimal]]:
    """Turn a FRED observations JSON payload into (date, value) pairs.

    FRED marks days with no observation (e.g. bond-market holidays) with a
    non-numeric placeholder such as ".". Those days are skipped rather than
    stored as zero or NULL, so a missing day can never be mistaken for a 0% rate.
    """
    if "observations" not in payload:
        raise FredError(f"Unexpected FRED response: {payload.get('error_message', payload)}")

    out: list[tuple[date, Decimal]] = []
    for obs in payload["observations"]:
        try:
            value = Decimal(obs["value"])
        except (InvalidOperation, KeyError, TypeError):
            continue
        if not value.is_finite():
            continue
        out.append((date.fromisoformat(obs["date"]), value))
    return out


def fetch_series(
    series_id: str,
    api_key: str,
    start: date,
    end: date | None = None,
    session: requests.Session | None = None,
    max_attempts: int = 4,
) -> list[tuple[date, Decimal]]:
    """Download one FRED series between start and end (inclusive)."""
    if not api_key:
        raise FredError("FRED_API_KEY is not set. Get a free key and add it to your .env file.")

    params = {
        "series_id": series_id,
        "api_key": api_key,
        "file_type": "json",
        "observation_start": start.isoformat(),
    }
    if end is not None:
        params["observation_end"] = end.isoformat()

    http = session or requests.Session()
    for attempt in range(1, max_attempts + 1):
        try:
            resp = http.get(FRED_OBSERVATIONS_URL, params=params, timeout=30)
        except requests.RequestException as exc:
            # requests puts the full URL, including api_key, into its error messages.
            # Raise a clean error and suppress the original (`from None`) so the key
            # never appears in a traceback, a log, or a screenshot of the terminal.
            raise FredError(
                f"Could not reach FRED for {series_id} ({type(exc).__name__}). Check your internet connection."
            ) from None
        if resp.status_code in _RETRY_STATUSES and attempt < max_attempts:
            time.sleep(2 ** attempt)  # exponential backoff: 2s, 4s, 8s
            continue
        if resp.status_code != 200:
            body = redact(resp.text[:200], api_key)
            raise FredError(f"FRED returned HTTP {resp.status_code} for {series_id}: {body}")
        try:
            payload = resp.json()
        except ValueError:
            raise FredError(f"FRED returned a non-JSON response for {series_id}") from None
        try:
            return parse_observations(payload)
        except FredError as exc:
            raise FredError(redact(str(exc), api_key)) from None

    raise FredError(f"FRED request for {series_id} failed after {max_attempts} attempts")


def redact(text: str, secret: str) -> str:
    """Replace every occurrence of `secret` in `text`."""
    return text.replace(secret, "***") if secret else text
