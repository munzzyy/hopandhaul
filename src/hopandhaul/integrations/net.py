#!/usr/bin/env python3
"""
net.py - shared HTTP-JSON fetch for the provider adapters (duffel/geoapify/weather/providers).

One retry/backoff/rate-limit implementation instead of four near-identical copies. Before this
module, a single transient 429 or 5xx from any provider permanently downgraded a plan to
estimate-only for the rest of the process - there was no retry anywhere in the integrations
layer. `fetch_json()` fixes that: retries on 429/5xx and on connection-level errors (timeout,
reset, DNS), honors `Retry-After` when a provider sends one, and never retries a 4xx that isn't
429 (a bad request replayed unchanged just fails the same way three more times).

Also ships `TokenBucket` (client-side rate limiting so a burst of map clicks doesn't itself
trip a provider's own 429s) and `TTLCache` (one tested expiring-cache class instead of hand-
rolled dict+lock copies scattered per module).

Stdlib only: urllib, time, threading.

Usage: this module has no CLI of its own - it's a library import for the four adapters.
  python -m hopandhaul.integrations.net   # offline retry/backoff/rate-limit/cache selftest
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field


class FetchError(Exception):
    """Raised when fetch_json exhausts its retries. Wraps the last underlying error."""

    def __init__(self, message: str, *, status: int | None = None, cause: Exception | None = None):
        super().__init__(message)
        self.status = status
        self.cause = cause


# Status codes worth retrying: 429 (rate limited) and 5xx (upstream trouble). A plain 4xx
# (bad request, 401, 404) means retrying the identical request will fail identically - surface
# it immediately instead of burning the retry budget.
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


def fetch_json(url: str, *, data: bytes | None = None, headers: dict | None = None,
                method: str | None = None, timeout: float = 15.0,
                max_retries: int = 3, backoff_base: float = 0.5, backoff_cap: float = 8.0,
                sleep=time.sleep) -> dict:
    """GET/POST a URL, decode JSON, retrying transient failures with exponential backoff.

    Retries on: HTTP 429/5xx, and network-level errors (timeout, connection reset, DNS).
    Does NOT retry other HTTP errors (401/403/404/422/...) - those are real, stable failures.
    `sleep` is injectable so the selftest can run the full backoff ladder with zero wall-clock
    delay.
    """
    verb = method or ("POST" if data else "GET")
    last_exc: Exception | None = None
    last_status: int | None = None
    attempt = 0
    while attempt <= max_retries:
        req = urllib.request.Request(url, data=data, headers=headers or {}, method=verb)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            last_status = e.code
            last_exc = e
            if e.code not in _RETRYABLE_STATUS or attempt == max_retries:
                # cause=e keeps the body readable via e.cause.read() for callers that want detail
                raise FetchError(f"HTTP {e.code} from {_host(url)} after {attempt + 1} attempt(s)",
                                  status=e.code, cause=e) from e
            wait = _retry_after(e) or _backoff(attempt, backoff_base, backoff_cap)
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            last_exc = e
            if attempt == max_retries:
                raise FetchError(f"network error reaching {_host(url)}: {e}", cause=e) from e
            wait = _backoff(attempt, backoff_base, backoff_cap)
        except ValueError as e:  # malformed JSON body - not transient, don't retry
            raise FetchError(f"invalid JSON from {_host(url)}: {e}", cause=e) from e
        sleep(wait)
        attempt += 1
    # unreachable, but keeps type-checkers honest
    raise FetchError(f"exhausted retries against {_host(url)}", status=last_status, cause=last_exc)


def _host(url: str) -> str:
    return url.split("://", 1)[-1].split("/", 1)[0]


def _retry_after(e: urllib.error.HTTPError) -> float | None:
    """Honor a provider's Retry-After header (seconds, or an HTTP-date we don't bother parsing)."""
    val = e.headers.get("Retry-After") if e.headers else None
    if not val:
        return None
    try:
        return max(0.0, min(float(val), 30.0))  # clamp: never wait more than 30s on our say-so
    except ValueError:
        return None  # HTTP-date form - fall back to our own backoff


def _backoff(attempt: int, base: float, cap: float) -> float:
    """Exponential backoff, no jitter needed at this call volume (single-user localhost tool)."""
    return min(cap, base * (2 ** attempt))


@dataclass
class TokenBucket:
    """Simple thread-safe token-bucket rate limiter.

    `rate` tokens refill per second, up to `capacity`. `acquire()` blocks until a token is
    available. Sized for Duffel's real free-tier limit (120 req/60s -> rate=2.0) so a burst of
    concurrent gateway lookups throttles itself instead of tripping the provider's own 429s.
    """
    rate: float
    capacity: float
    _tokens: float = field(init=False)
    _last: float = field(init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    def __post_init__(self):
        self._tokens = self.capacity
        self._last = time.monotonic()

    def acquire(self, *, sleep=time.sleep) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                elapsed = now - self._last
                self._last = now
                self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                deficit = 1.0 - self._tokens
                wait = deficit / self.rate if self.rate > 0 else 0.05
            sleep(wait)


class TTLCache:
    """Thread-safe expiring cache. Evicts lazily (on get/set) instead of a background sweep.

    Replaces the hand-rolled per-module dict+lock caches: the old offer cache evicted by
    sorting all entries by expiry and dropping the oldest half (O(n log n) on every write,
    and it deletes live entries early just because the dict got big). This evicts only what's
    actually expired, then falls back to dropping the single oldest entry if still over
    capacity - O(n) worst case, O(1) typical.
    """

    def __init__(self, ttl_seconds: float, max_size: int = 512):
        self.ttl = ttl_seconds
        self.max_size = max_size
        self._store: dict = {}
        self._lock = threading.Lock()

    def get(self, key):
        with self._lock:
            hit = self._store.get(key)
            if hit is None:
                return None
            value, expires_at = hit
            if expires_at < time.monotonic():
                del self._store[key]
                return None
            return value

    def set(self, key, value) -> None:
        with self._lock:
            now = time.monotonic()
            self._store[key] = (value, now + self.ttl)
            if len(self._store) > self.max_size:
                self._evict(now)

    def _evict(self, now: float) -> None:
        """Caller holds _lock. Drop expired entries first; if still over capacity, drop the
        single oldest by expiry (not half the cache - the old scheme's real bug)."""
        expired = [k for k, (_, exp) in self._store.items() if exp < now]
        for k in expired:
            del self._store[k]
        while len(self._store) > self.max_size:
            oldest_key = min(self._store, key=lambda k: self._store[k][1])
            del self._store[oldest_key]

    def __len__(self):
        with self._lock:
            return len(self._store)


# --------------------------------------------------------------------------- self-test (offline)
def selftest():
    import unittest.mock as mock
    fails = []

    def check(name, cond):
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        if not cond:
            fails.append(name)

    # ---- backoff math ----
    check("backoff grows exponentially, capped",
          _backoff(0, 0.5, 8.0) == 0.5 and _backoff(1, 0.5, 8.0) == 1.0
          and _backoff(2, 0.5, 8.0) == 2.0 and _backoff(10, 0.5, 8.0) == 8.0)

    check("_host strips scheme and path",
          _host("https://api.duffel.com/air/offers?x=1") == "api.duffel.com")

    # ---- fetch_json retry behavior, fully offline via urlopen monkeypatch ----
    slept = []

    def fake_sleep(s):
        slept.append(s)

    def install(responder):
        return mock.patch("urllib.request.urlopen", side_effect=responder)

    class FakeResp:
        def __init__(self, body: bytes):
            self._body = body

        def read(self):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def responder_success_after_retries(n_failures, final_body=b'{"ok": true}'):
        state = {"i": 0}

        def _resp(req, timeout=None):
            state["i"] += 1
            if state["i"] <= n_failures:
                raise urllib.error.HTTPError(req.full_url, 503, "busy", {}, None)
            return FakeResp(final_body)
        return _resp

    with install(responder_success_after_retries(2)):
        out = fetch_json("https://api.duffel.com/x", max_retries=3, sleep=fake_sleep)
    check("retries transient 503 then succeeds", out == {"ok": True})
    check("sleeps between each retry (2 sleeps for 2 failures)", len(slept) == 2)

    slept.clear()

    def responder_always_429(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 429, "rate limited", {"Retry-After": "1"}, None)

    with install(responder_always_429):
        try:
            fetch_json("https://api.duffel.com/x", max_retries=2, sleep=fake_sleep)
            raised = False
        except FetchError as e:
            raised = True
            status = e.status
    check("exhausted retries raises FetchError with status", raised and status == 429)
    check("Retry-After header is honored over our own backoff", slept and slept[0] == 1.0)

    def responder_404(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 404, "not found", {}, None)

    calls_404 = {"n": 0}

    def counting_404(req, timeout=None):
        calls_404["n"] += 1
        return responder_404(req, timeout)

    with install(counting_404):
        try:
            fetch_json("https://api.duffel.com/x", max_retries=3, sleep=fake_sleep)
        except FetchError:
            pass
    check("non-retryable 404 fails on first attempt (no retry burned)", calls_404["n"] == 1)

    def responder_timeout(req, timeout=None):
        raise TimeoutError("timed out")

    slept.clear()
    with install(responder_timeout):
        try:
            fetch_json("https://api.duffel.com/x", max_retries=2, sleep=fake_sleep)
            raised2 = False
        except FetchError:
            raised2 = True
    check("network-level TimeoutError retries then raises FetchError", raised2 and len(slept) == 2)

    def responder_bad_json(req, timeout=None):
        return FakeResp(b"not json")

    with install(responder_bad_json):
        try:
            fetch_json("https://api.duffel.com/x", max_retries=3, sleep=fake_sleep)
            raised3 = False
        except FetchError:
            raised3 = True
    check("malformed JSON is not retried (fails immediately)", raised3)

    # ---- TokenBucket ---- (frozen clock: refill must be deterministic, not a real-time race)
    tb_now = {"t": 0.0}
    with mock.patch("time.monotonic", side_effect=lambda: tb_now["t"]):
        tb = TokenBucket(rate=1.0, capacity=2)
        waits = []

        def advance_and_record(s):
            waits.append(s)
            tb_now["t"] += s  # simulate time actually passing during the wait

        tb.acquire(sleep=advance_and_record)
        tb.acquire(sleep=advance_and_record)
        check("token bucket allows burst up to capacity with no wait", waits == [])
        tb.acquire(sleep=advance_and_record)
        check("token bucket makes the caller wait once capacity is exhausted", len(waits) == 1)

    # ---- TTLCache ----
    fake_now = {"t": 0.0}
    with mock.patch("time.monotonic", side_effect=lambda: fake_now["t"]):
        c = TTLCache(ttl_seconds=10, max_size=3)
        c.set("a", 1)
        check("cache returns a fresh value", c.get("a") == 1)
        fake_now["t"] = 11
        check("cache expires a stale value", c.get("a") is None)

        c2 = TTLCache(ttl_seconds=100, max_size=2)
        fake_now["t"] = 0
        c2.set("a", 1)
        fake_now["t"] = 1
        c2.set("b", 2)
        fake_now["t"] = 2
        c2.set("c", 3)  # over capacity -> evict oldest (a), not half the cache
        check("cache evicts only the single oldest entry over capacity",
              len(c2) == 2 and c2.get("a") is None and c2.get("b") == 2 and c2.get("c") == 3)

    check("cache key can be a composite tuple (widened offer-cache key shape)",
          TTLCache(60).get(("JFK", "ASE", "2026-08-15", "economy", True)) is None)

    print(f"\n{'ALL PASS' if not fails else str(len(fails)) + ' FAILED'} (offline checks)")
    return 1 if fails else 0


if __name__ == "__main__":
    import sys
    sys.exit(selftest())
