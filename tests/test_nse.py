"""NSE verdict logic with a fake session (no network)."""

import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
import requests

from finn import nse

TODAY = datetime(2026, 10, 31, 12, 0, 0)


class FakeResponse:
    def __init__(self, status=200, payload=None, bad_json=False):
        self.status_code = status
        self.payload = payload
        self.bad_json = bad_json

    def json(self):
        if self.bad_json:
            raise ValueError("not json")
        return self.payload


class FakeSession:
    """Returns queued responses in order; records the params used."""
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append(params)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def filing(date_text):
    return {"an_dt": date_text, "desc": "General Updates", "attchmntFile": "https://x/y.pdf"}


def test_nonempty_window():
    session = FakeSession([FakeResponse(200, [filing("30-Oct-2026 10:00:00")])])
    verdict = nse.classify_window(session, "kpittech", today=TODAY)
    assert verdict["verdict"] == "ok_nonempty"
    assert verdict["symbol"] == "KPITTECH"  # upper-cased
    assert session.calls[0]["symbol"] == "KPITTECH"


def test_empty_window_but_older_filings_is_valid_empty():
    session = FakeSession([FakeResponse(200, []), FakeResponse(200, [filing("19-Oct-2026 09:00:00"), filing("02-Oct-2026 09:00:00")])])
    verdict = nse.classify_window(session, "TCS", today=TODAY)
    assert verdict["verdict"] == "valid_empty_window"
    assert verdict["newest_filing_in_wide_window"] == "19-Oct-2026"


def test_empty_everywhere_is_ambiguous_not_valid():
    session = FakeSession([FakeResponse(200, []), FakeResponse(200, [])])
    verdict = nse.classify_window(session, "NOSUCHXYZ", today=TODAY)
    assert verdict["verdict"] == "ambiguous_empty"


def test_http_error_is_fetch_error_not_empty():
    session = FakeSession([FakeResponse(403, None)])
    verdict = nse.classify_window(session, "TCS", today=TODAY)
    assert verdict["verdict"] == "fetch_error"
    assert "403" in verdict["detail"]


def test_network_exception_is_fetch_error():
    session = FakeSession([requests.exceptions.ConnectionError("boom")])
    verdict = nse.classify_window(session, "TCS", today=TODAY)
    assert verdict["verdict"] == "fetch_error"


def test_html_body_is_parse_error():
    session = FakeSession([FakeResponse(200, None, bad_json=True)])
    verdict = nse.classify_window(session, "TCS", today=TODAY)
    assert verdict["verdict"] == "parse_error"


def test_non_list_json_is_parse_error():
    session = FakeSession([FakeResponse(200, {"error": "blocked"})])
    verdict = nse.classify_window(session, "TCS", today=TODAY)
    assert verdict["verdict"] == "parse_error"


def test_failure_while_widening_is_reported_not_called_valid():
    session = FakeSession([FakeResponse(200, []), FakeResponse(500, None)])
    verdict = nse.classify_window(session, "TCS", today=TODAY)
    assert verdict["verdict"] == "fetch_error"
    assert "widening" in verdict["detail"]


@pytest.mark.parametrize("bad", ["", "TC S", "../x", "A" * 40, "tcs;drop"])
def test_bad_symbols_rejected(bad):
    with pytest.raises(ValueError):
        nse.normalise_symbol(bad)
