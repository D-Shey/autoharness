"""In-Process AI Gateway Circuit Breaker and Semantic Cache (inspired by SinapsAI).

Pure Python standard library implementation (zero runtime dependencies).
Provides cascading failure protection, exponential backoff, provider failover,
and thread-safe in-memory TTL caching for MCP tool dispatch and external APIs.
"""
from __future__ import annotations

import collections
import enum
import threading
import time
from typing import Any, Callable

from autoharness.env import HarnessError


class CircuitState(str, enum.Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreakerOpenError(HarnessError):
    """Raised when a tool or upstream request is rejected due to an open circuit breaker."""

    def __init__(self, name: str, reason: str, cooldown_remaining: float):
        self.name = name
        self.reason = reason
        self.cooldown_remaining = max(0.0, cooldown_remaining)
        super().__init__(
            f"Circuit breaker for {name!r} is OPEN ({reason or 'consecutive failures'}). "
            f"Failing fast for {self.cooldown_remaining:.1f}s to prevent cascading failure."
        )


class CircuitBreaker:
    """In-process Circuit Breaker for MCP servers and upstream API calls.

    States:
      - CLOSED: Requests pass normally. Failures increment failure counter.
      - OPEN: Failures exceeded threshold. Calls fail fast or execute fallback.
      - HALF_OPEN: Cooldown expired. Probe requests test if upstream has recovered.
    """

    def __init__(
        self,
        name: str,
        failure_threshold: int = 3,
        cooldown_seconds: float = 30.0,
        half_open_success_threshold: int = 1,
        fallback: Callable[..., Any] | None = None,
    ):
        self.name = name
        self.failure_threshold = max(1, failure_threshold)
        self.cooldown_seconds = max(0.01, cooldown_seconds)
        self.half_open_success_threshold = max(1, half_open_success_threshold)
        self.fallback = fallback

        self._state = CircuitState.CLOSED
        self._failures = 0
        self._half_open_successes = 0
        self._last_failure_time = 0.0
        self._last_failure_reason = ""
        self._last_state_change = time.monotonic()
        self._total_calls = 0
        self._total_failures = 0
        self._lock = threading.RLock()

    @property
    def state(self) -> CircuitState:
        with self._lock:
            now = time.monotonic()
            if self._state == CircuitState.OPEN:
                if now - self._last_failure_time >= self.cooldown_seconds:
                    self._state = CircuitState.HALF_OPEN
                    self._half_open_successes = 0
                    self._last_state_change = now
            return self._state

    def can_execute(self) -> bool:
        return self.state in (CircuitState.CLOSED, CircuitState.HALF_OPEN)

    def record_success(self) -> None:
        with self._lock:
            current = self.state
            if current == CircuitState.HALF_OPEN:
                self._half_open_successes += 1
                if self._half_open_successes >= self.half_open_success_threshold:
                    self._state = CircuitState.CLOSED
                    self._failures = 0
                    self._half_open_successes = 0
                    self._last_state_change = time.monotonic()
            elif current == CircuitState.CLOSED:
                self._failures = 0

    def record_failure(self, reason: str = "") -> None:
        with self._lock:
            now = time.monotonic()
            self._total_failures += 1
            self._last_failure_time = now
            self._last_failure_reason = reason
            current = self.state
            if current == CircuitState.HALF_OPEN:
                self._state = CircuitState.OPEN
                self._last_state_change = now
            elif current == CircuitState.CLOSED:
                self._failures += 1
                if self._failures >= self.failure_threshold:
                    self._state = CircuitState.OPEN
                    self._last_state_change = now

    def execute(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        with self._lock:
            self._total_calls += 1
            current = self.state
            if current == CircuitState.OPEN:
                remaining = self.cooldown_seconds - (time.monotonic() - self._last_failure_time)
                if self.fallback is not None:
                    return self.fallback(*args, **kwargs)
                raise CircuitBreakerOpenError(self.name, self._last_failure_reason, remaining)

        try:
            result = func(*args, **kwargs)
            self.record_success()
            return result
        except Exception as e:
            self.record_failure(str(e))
            if self.fallback is not None:
                return self.fallback(*args, **kwargs)
            raise

    def reset(self) -> None:
        with self._lock:
            self._state = CircuitState.CLOSED
            self._failures = 0
            self._half_open_successes = 0
            self._last_failure_time = 0.0
            self._last_failure_reason = ""
            self._last_state_change = time.monotonic()

    def to_dict(self) -> dict[str, Any]:
        with self._lock:
            curr = self.state
            cooldown_rem = 0.0
            if curr == CircuitState.OPEN:
                cooldown_rem = max(0.0, self.cooldown_seconds - (time.monotonic() - self._last_failure_time))
            return {
                "name": self.name,
                "state": curr.value,
                "failures": self._failures,
                "failure_threshold": self.failure_threshold,
                "cooldown_seconds": self.cooldown_seconds,
                "cooldown_remaining": round(cooldown_rem, 2),
                "last_failure_reason": self._last_failure_reason,
                "total_calls": self._total_calls,
                "total_failures": self._total_failures,
            }


class CircuitBreakerRegistry:
    """Registry managing CircuitBreaker instances across MCP servers and gateways."""

    def __init__(self, default_threshold: int = 3, default_cooldown: float = 30.0):
        self.default_threshold = default_threshold
        self.default_cooldown = default_cooldown
        self._breakers: dict[str, CircuitBreaker] = {}
        self._lock = threading.RLock()

    def get(
        self,
        name: str,
        threshold: int | None = None,
        cooldown: float | None = None,
        fallback: Callable[..., Any] | None = None,
    ) -> CircuitBreaker:
        with self._lock:
            if name not in self._breakers:
                self._breakers[name] = CircuitBreaker(
                    name=name,
                    failure_threshold=threshold or self.default_threshold,
                    cooldown_seconds=cooldown or self.default_cooldown,
                    fallback=fallback,
                )
            return self._breakers[name]

    def all_states(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return {name: b.to_dict() for name, b in sorted(self._breakers.items())}

    def reset_all(self) -> None:
        with self._lock:
            for b in self._breakers.values():
                b.reset()

    def reset(self, name: str) -> bool:
        with self._lock:
            b = self._breakers.get(name)
            if b:
                b.reset()
                return True
            return False


class LRUTtlCache:
    """Thread-safe LRU in-process cache with per-item TTL (SinapsAI model)."""

    def __init__(self, max_size: int = 256, default_ttl: float = 60.0):
        self.max_size = max(1, max_size)
        self.default_ttl = max(0.0, default_ttl)
        self._cache: collections.OrderedDict[str, tuple[float, Any]] = collections.OrderedDict()
        self._lock = threading.RLock()
        self._hits = 0
        self._misses = 0

    def get(self, key: str) -> Any | None:
        with self._lock:
            item = self._cache.get(key)
            if item is None:
                self._misses += 1
                return None
            expires_at, value = item
            if time.monotonic() > expires_at:
                self._cache.pop(key, None)
                self._misses += 1
                return None
            self._cache.move_to_end(key)
            self._hits += 1
            return value

    def set(self, key: str, value: Any, ttl: float | None = None) -> None:
        effective_ttl = self.default_ttl if ttl is None else ttl
        expires_at = time.monotonic() + effective_ttl
        with self._lock:
            if key in self._cache:
                self._cache.pop(key)
            elif len(self._cache) >= self.max_size:
                self._cache.popitem(last=False)
            self._cache[key] = (expires_at, value)

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()
            self._hits = 0
            self._misses = 0

    def stats(self) -> dict[str, Any]:
        with self._lock:
            now = time.monotonic()
            valid = sum(1 for exp, _ in self._cache.values() if exp > now)
            return {
                "size": len(self._cache),
                "active_valid": valid,
                "max_size": self.max_size,
                "hits": self._hits,
                "misses": self._misses,
            }
