from __future__ import annotations

import pytest

import translator.llm_api as llm_api
from translator.llm_api import RateLimiter


def test_rate_limiter_reserves_slots_across_callers(monkeypatch):
    clock = [100.0]
    sleeps = []

    monkeypatch.setattr(llm_api.time, "monotonic", lambda: clock[0])

    def sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(llm_api.time, "sleep", sleep)
    limiter = RateLimiter(60)
    limiter.acquire()
    limiter.acquire()
    limiter.acquire()
    assert sleeps == [1.0, 1.0]


def test_rate_limiter_rejects_non_positive_rate():
    with pytest.raises(ValueError):
        RateLimiter(0)
