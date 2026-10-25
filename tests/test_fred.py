"""FRED parsing and HTTP behaviour, with no real network calls."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from ballast.market_data import fred

PAYLOAD = {
    "observation_start": "2024-07-01",
    "count": 4,
    "observations": [
        {"realtime_start": "2026-01-01", "realtime_end": "2026-01-01", "date": "2024-07-03", "value": "4.36"},
        {"realtime_start": "2026-01-01", "realtime_end": "2026-01-01", "date": "2024-07-04", "value": "."},
        {"realtime_start": "2026-01-01", "realtime_end": "2026-01-01", "date": "2024-07-05", "value": "4.28"},
        {"realtime_start": "2026-01-01", "realtime_end": "2026-01-01", "date": "2024-07-08", "value": "NaN"},
    ],
}


def test_parse_skips_missing_values():
    assert fred.parse_observations(PAYLOAD) == [
        (date(2024, 7, 3), Decimal("4.36")),
        (date(2024, 7, 5), Decimal("4.28")),
    ]


def test_parse_reports_api_error_message():
    with pytest.raises(fred.FredError, match="Bad Request"):
        fred.parse_observations({"error_code": 400, "error_message": "Bad Request. Variable api_key is not set."})


class FakeResponse:
    def __init__(self, status_code, payload=None, body=""):
        self.status_code = status_code
        self._payload = payload
        self.text = body

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, params, timeout):
        self.calls.append(params)
        return self.responses.pop(0)


def test_retries_on_rate_limit_then_succeeds(monkeypatch):
    monkeypatch.setattr(fred.time, "sleep", lambda s: None)
    session = FakeSession([FakeResponse(429), FakeResponse(200, PAYLOAD)])
    out = fred.fetch_series("DGS10", "k" * 32, date(2024, 7, 1), session=session)
    assert len(out) == 2
    assert len(session.calls) == 2
    assert session.calls[0]["file_type"] == "json"
    assert session.calls[0]["observation_start"] == "2024-07-01"


def test_error_message_never_contains_api_key():
    key = "secretkey" + "x" * 23
    session = FakeSession([FakeResponse(400, body="Bad Request")])
    with pytest.raises(fred.FredError) as exc:
        fred.fetch_series("DGS10", key, date(2024, 7, 1), session=session)
    assert key not in str(exc.value)


def test_missing_key_fails_fast():
    with pytest.raises(fred.FredError, match="FRED_API_KEY"):
        fred.fetch_series("DGS10", "", date(2024, 7, 1))
