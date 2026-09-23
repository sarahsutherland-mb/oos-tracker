"""The cold-start retry on brand-page fetches.

Anthropologie is authoritative — its brand page IS the stock signal for all
25 SKUs, with no PDP fallback and no manual-sheet row behind it. So a single
dropped fetch doesn't show up as an error on a dashboard; it shows up as 25
products quietly missing from the run. These tests pin the retry that stops
that, using a mock transport so they never touch a real retailer.

Run with: .venv/bin/python -m pytest tests/ -q
"""
from __future__ import annotations

import httpx
import pytest

from src import brand_pages as bp


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    """Backoff is real seconds in production and dead time in a test."""
    monkeypatch.setattr(bp.time, "sleep", lambda _s: None)


def client_returning(*statuses: int) -> tuple[httpx.Client, list[int]]:
    """A client whose Nth request gets the Nth status, last one repeating."""
    seen: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        status = statuses[min(len(seen), len(statuses) - 1)]
        seen.append(status)
        return httpx.Response(status, text="<html></html>")

    return httpx.Client(transport=httpx.MockTransport(handler)), seen


def test_cold_start_403_is_retried_and_succeeds():
    """The measured Anthropologie behaviour: first request challenged, rest fine."""
    client, seen = client_returning(403, 200)
    r = bp._get_with_retry(client, "https://example.test/brands/megababe")
    assert r.status_code == 200
    assert seen == [403, 200]


def test_a_clean_fetch_makes_exactly_one_request():
    client, seen = client_returning(200)
    r = bp._get_with_retry(client, "https://example.test/brands/megababe")
    assert r.status_code == 200
    assert seen == [200]


def test_a_persistent_block_gives_up_and_reports_the_real_status():
    """Three attempts, then hand back the 403 so raise_for_status() is honest.

    Reporting the block is the point — the caller turns it into a skip, which
    is what keeps a genuine outage visible instead of silently empty.
    """
    client, seen = client_returning(403)
    r = bp._get_with_retry(client, "https://example.test/brands/megababe")
    assert r.status_code == 403
    assert len(seen) == 3


def test_rate_limits_and_server_errors_are_retried_too():
    for status in (429, 500, 502, 503, 504):
        client, seen = client_returning(status, 200)
        assert bp._get_with_retry(client, "https://example.test/x").status_code == 200
        assert seen == [status, 200], f"{status} should have been retried"


def test_a_404_is_not_retried():
    """A missing page is an answer, not a challenge. Retrying wastes the run."""
    client, seen = client_returning(404)
    r = bp._get_with_retry(client, "https://example.test/gone")
    assert r.status_code == 404
    assert seen == [404]


def test_a_timeout_is_retried_then_raised():
    """ASOS's failure mode. Still raises, so the scraper records the error."""
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        raise httpx.ReadTimeout("read timed out", request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(httpx.ReadTimeout):
        bp._get_with_retry(client, "https://example.test/slow")
    assert attempts["n"] == 3


def test_a_timeout_that_clears_on_retry_returns_the_page():
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise httpx.ReadTimeout("read timed out", request=request)
        return httpx.Response(200, text="<html>ok</html>")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert bp._get_with_retry(client, "https://example.test/slow").status_code == 200
    assert attempts["n"] == 2
