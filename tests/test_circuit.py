"""Tests for In-Process AI Gateway Circuit Breaker and Semantic Cache (SinapsAI model)."""
from __future__ import annotations

import time
import pytest
from autoharness.circuit import (
    CircuitBreaker,
    CircuitBreakerOpenError,
    CircuitBreakerRegistry,
    CircuitState,
    LRUTtlCache,
)


def test_circuit_breaker_transitions_to_open():
    breaker = CircuitBreaker("test-srv", failure_threshold=2, cooldown_seconds=0.1)
    assert breaker.state == CircuitState.CLOSED
    assert breaker.can_execute() is True

    # 1st failure: still closed
    with pytest.raises(ValueError):
        def fail():
            raise ValueError("boom")
        breaker.execute(fail)
    assert breaker.state == CircuitState.CLOSED

    # 2nd failure: trips open
    with pytest.raises(ValueError):
        def fail2():
            raise ValueError("boom 2")
        breaker.execute(fail2)
    assert breaker.state == CircuitState.OPEN
    assert breaker.can_execute() is False

    # 3rd call: fails fast with CircuitBreakerOpenError without calling inner function
    called = []
    with pytest.raises(CircuitBreakerOpenError) as exc_info:
        breaker.execute(lambda: called.append(1))
    assert not called
    assert "is OPEN" in str(exc_info.value)


def test_circuit_breaker_half_open_recovery():
    breaker = CircuitBreaker("recovery-srv", failure_threshold=1, cooldown_seconds=0.05)
    with pytest.raises(RuntimeError):
        breaker.execute(lambda: (_ for _ in ()).throw(RuntimeError("down")))
    assert breaker.state == CircuitState.OPEN

    # Wait for cooldown to expire
    time.sleep(0.06)
    assert breaker.state == CircuitState.HALF_OPEN
    assert breaker.can_execute() is True

    # Canary call succeeds -> back to CLOSED
    res = breaker.execute(lambda: "healthy")
    assert res == "healthy"
    assert breaker.state == CircuitState.CLOSED
    assert breaker.to_dict()["failures"] == 0


def test_circuit_breaker_fallback():
    breaker = CircuitBreaker(
        "fallback-srv",
        failure_threshold=1,
        cooldown_seconds=1.0,
        fallback=lambda *args, **kwargs: "cached-fallback",
    )
    # Trip the breaker
    res = breaker.execute(lambda: (_ for _ in ()).throw(Exception("error")))
    assert res == "cached-fallback"
    assert breaker.state == CircuitState.OPEN

    # Next call executes fallback immediately
    res2 = breaker.execute(lambda: "primary")
    assert res2 == "cached-fallback"


def test_circuit_breaker_registry():
    registry = CircuitBreakerRegistry(default_threshold=2, default_cooldown=10.0)
    b1 = registry.get("srv1")
    b2 = registry.get("srv2")
    assert b1.name == "srv1"
    assert b2.name == "srv2"
    assert b1 is registry.get("srv1")

    states = registry.all_states()
    assert "srv1" in states
    assert "srv2" in states
    assert states["srv1"]["state"] == "closed"

    # Trip srv1 and reset
    b1.record_failure("error 1")
    b1.record_failure("error 2")
    assert b1.state == CircuitState.OPEN
    registry.reset("srv1")
    assert b1.state == CircuitState.CLOSED


def test_lru_ttl_cache():
    cache = LRUTtlCache(max_size=3, default_ttl=0.05)
    cache.set("a", 1)
    cache.set("b", 2)
    assert cache.get("a") == 1
    assert cache.get("b") == 2
    assert cache.get("c") is None

    # Test TTL expiration (margin > Windows 15.6ms timer tick)
    time.sleep(0.12)
    assert cache.get("a") is None

    # Test LRU eviction
    cache.set("k1", "v1", ttl=10.0)
    cache.set("k2", "v2", ttl=10.0)
    cache.set("k3", "v3", ttl=10.0)
    cache.set("k4", "v4", ttl=10.0)  # Evicts k1
    assert cache.get("k1") is None
    assert cache.get("k4") == "v4"

    stats = cache.stats()
    assert stats["size"] == 3
    assert stats["max_size"] == 3
