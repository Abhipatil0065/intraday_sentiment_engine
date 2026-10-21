"""
LLM client (section 26 - NLP / LLM ANALYSIS).\r\n\r\nProvider-neutral: any OpenAI-compatible endpoint (NVIDIA NIM, DeepSeek,\r\nOpenRouter, Groq, Ollama, vLLM...)

Design rules:
  * Strict JSON output, validated against a Pydantic schema.
  * Any failure (missing key, network, malformed JSON, validation) falls back
    to the deterministic heuristic classifier - the pipeline never dies.
  * Concurrency-limited and retried with exponential backoff.
  * Client-side rate limited (LLM_REQUESTS_PER_MINUTE, default 40/min) on a
    rolling window, so a news burst waits instead of drawing a provider 429.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections import deque

import httpx

from app.config import settings
from app.logging_conf import get_logger

log = get_logger(__name__)

# Per-call timeout for a single LLM classification request. 60s ceiling so a
# single slow-but-valid model response cannot be mistaken for a dead endpoint.
# The circuit breaker will skip LLM entirely after 3 consecutive 503s/timeouts.
LLM_CALL_TIMEOUT_SECONDS: int = 60

_SEMAPHORE: asyncio.Semaphore | None = None
_CLIENT: httpx.AsyncClient | None = None
# Loop the cached client is bound to - see get_client().
_CLIENT_LOOP: asyncio.AbstractEventLoop | None = None
# Loop the cached semaphore is bound to - see _semaphore_fixed().
_SEMAPHORE_LOOP: asyncio.AbstractEventLoop | None = None


def _semaphore_fixed() -> asyncio.Semaphore:
    """Lazily created concurrency limiter, rebound when the event loop changes.

    The scheduler runs the pipeline in a worker thread via ``asyncio.run()``,
    which opens and CLOSES a fresh event loop every tick. An
    ``asyncio.Semaphore`` binds itself to the loop that first had to WAIT on
    it, and asyncio then refuses to use it from any other loop ("... is bound
    to a different event loop"). With a single call in flight the semaphore
    never blocked, so it never bound, and the bug stayed invisible; with
    concurrent calls it blocks, and the NEXT tick would fail every call past
    the first with an exception that looks like a provider failure.

    Same guard as ``get_client()`` below: when the running loop changes, the
    cached object is replaced rather than reused.
    """
    global _SEMAPHORE, _SEMAPHORE_LOOP
    try:
        current_loop = asyncio.get_running_loop()
    except RuntimeError:  # pragma: no cover - always called from a loop
        current_loop = None
    if _SEMAPHORE is None or _SEMAPHORE_LOOP is not current_loop:
        _SEMAPHORE = asyncio.Semaphore(settings.llm_max_concurrent)
        _SEMAPHORE_LOOP = current_loop
    return _SEMAPHORE


# ==========================================================================
# Client-side rate limit (requests per minute)
# ==========================================================================
class RateLimiter:
    """
    Rolling-window limiter: never allows more than ``limit`` acquisitions in
    any 60-second span. Each acquisition is one real HTTP attempt against the
    provider, so retries are charged against the same budget the key has.

    Waiting is the point - blocking is preferable to letting NVIDIA answer 429
    and throw the result away. ``limit <= 0`` disables the limiter entirely.
    """

    WINDOW_SECONDS = 60.0

    def __init__(self, limit: int) -> None:
        self.limit = max(0, int(limit))
        self._stamps: deque[float] = deque()
        self._lock = asyncio.Lock()

    def _prune(self, now: float) -> None:
        cutoff = now - self.WINDOW_SECONDS
        while self._stamps and self._stamps[0] <= cutoff:
            self._stamps.popleft()

    def used(self) -> int:
        """Requests still inside the current window (diagnostics/tests)."""
        self._prune(time.monotonic())
        return len(self._stamps)

    def last_admitted_at(self) -> float | None:
        """
        Wall-clock stamp of the most recent admission, or ``None`` if none yet.

        This is the timestamp the limiter actually charged against the window,
        so it is what any caller reasoning about admission spacing must use. A
        fresh ``time.monotonic()`` read after :meth:`acquire` returns is NOT
        equivalent: the gap between the charge and the read is scheduler skew,
        which can push the reading across a window boundary and make a correct
        limiter appear to have exceeded its limit.

        Deliberately does not prune, so it never mutates limiter state.
        """
        return self._stamps[-1] if self._stamps else None

    async def acquire(self) -> None:
        """Block until a slot is free, then consume it."""
        if self.limit <= 0:
            return

        waited = False
        while True:
            async with self._lock:
                now = time.monotonic()
                self._prune(now)
                if len(self._stamps) < self.limit:
                    self._stamps.append(now)
                    if waited:
                        log.info(
                            "LLM rate limit %d/min: slot freed, request resumed.",
                            self.limit,
                        )
                    return
                # Budget spent - sleep until the oldest request ages out.
                sleep_for = max(self._stamps[0] + self.WINDOW_SECONDS - now, 0.05)
                if not waited:
                    waited = True
                    log.info(
                        "LLM rate limit %d/min reached - holding request for "
                        "%.1fs instead of tripping the provider's 429.",
                        self.limit, sleep_for,
                    )
            await asyncio.sleep(sleep_for)


_LIMITER: RateLimiter | None = None


def _limiter_fixed() -> RateLimiter:
    """Lazily created per-process rate limiter."""
    global _LIMITER
    if _LIMITER is None or _LIMITER.limit != max(0, int(settings.llm_requests_per_minute)):
        _LIMITER = RateLimiter(settings.llm_requests_per_minute)
    return _LIMITER


def requests_in_window() -> int:
    """Requests made in the last 60s (surfaces headroom in /api/health)."""
    return _LIMITER.used() if _LIMITER is not None else 0


# ==========================================================================
# Circuit breaker - disables LLM for this tick after 3 consecutive failures
# ==========================================================================
class CircuitBreaker:
    """
    Tracks consecutive LLM failures (503, timeout). After 3 consecutive failures,
    the breaker opens and LLM calls fail fast for the remainder of the tick.
    The breaker resets automatically at the start of the next tick.

    No asyncio.Lock: the counter is read and written without an await between
    them, so it is already atomic inside the event loop. A lock would be a
    liability here - an asyncio.Lock binds to the loop that first had to wait
    on it, and the scheduler opens a fresh loop every tick, so the lock from
    the previous tick would raise "bound to a different event loop" exactly
    when several calls fail at once (which is when the breaker matters most).
    """

    def __init__(self, threshold: int = 3):
        self.threshold = threshold
        self._consecutive_failures = 0
        self._open = False

    @property
    def is_open(self) -> bool:
        return self._open

    async def record_success(self) -> None:
        """Reset failure counter on success."""
        if self._consecutive_failures > 0:
            log.debug("LLM circuit breaker: reset after success")
            self._consecutive_failures = 0

    async def record_failure(self) -> None:
        """Increment failure counter; open breaker if threshold reached."""
        self._consecutive_failures += 1
        if self._consecutive_failures >= self.threshold:
            self._open = True
            log.warning(
                "LLM circuit breaker OPEN after %d consecutive failures - "
                "skipping LLM for remainder of tick",
                self._consecutive_failures,
            )

    async def reset(self) -> None:
        """Reset breaker state (called at tick start)."""
        self._consecutive_failures = 0
        self._open = False


_CIRCUIT_BREAKER: CircuitBreaker | None = None


def circuit_breaker() -> CircuitBreaker:
    """Get the singleton circuit breaker (created inside running loop)."""
    global _CIRCUIT_BREAKER
    if _CIRCUIT_BREAKER is None:
        _CIRCUIT_BREAKER = CircuitBreaker()
    return _CIRCUIT_BREAKER


async def reset_circuit_breaker() -> None:
    """Reset the circuit breaker at the start of each tick."""
    global _CIRCUIT_BREAKER
    if _CIRCUIT_BREAKER is not None:
        await _CIRCUIT_BREAKER.reset()


def llm_status() -> str:
    """
    Return LLM status for health checks.

    Returns:
        "ok" - LLM enabled and circuit breaker closed
        "degraded" - LLM enabled but circuit breaker open
        "disabled" - LLM not configured
    """
    if not settings.has_llm:
        return "disabled"
    cb = _CIRCUIT_BREAKER
    if cb is not None and cb.is_open:
        return "degraded"
    return "ok"


async def get_client() -> httpx.AsyncClient:
    global _CLIENT, _CLIENT_LOOP
    try:
        current_loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
    except RuntimeError:  # pragma: no cover - always called from a loop
        current_loop = None
    # Recreate the client when it belongs to a different event loop. The
    # scheduler runs the pipeline in a worker thread via asyncio.run(), which
    # opens and CLOSES a fresh loop every tick, so a cached client ends up
    # bound to a dead loop and the next call fails with "Event loop is
    # closed". The old client cannot be awaited shut (its loop is gone), so
    # it is simply replaced.
    if _CLIENT is None or _CLIENT.is_closed or _CLIENT_LOOP is not current_loop:
        _CLIENT = httpx.AsyncClient(
            base_url=settings.llm_base_url.rstrip("/"),
            timeout=settings.llm_timeout_seconds,
            headers={
                "Authorization": f"Bearer {settings.llm_api_key}",
                "Content-Type": "application/json",
            },
        )
        _CLIENT_LOOP = current_loop
    return _CLIENT


async def close_client() -> None:
    global _CLIENT, _CLIENT_LOOP
    if _CLIENT is not None and not _CLIENT.is_closed:
        await _CLIENT.aclose()
    _CLIENT = None
    _CLIENT_LOOP = None


_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)


def extract_json(text: str) -> dict:
    """
    Pull a JSON object out of a model response.

    Handles raw JSON, ```json fenced blocks, and JSON buried in prose.
    """
    if not text:
        raise ValueError("empty model response")
    text = text.strip()

    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"```\s*$", "", text).strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    match = _JSON_BLOCK.search(text)
    if match:
        return json.loads(match.group(0))
    raise ValueError("no JSON object found in model response")


async def chat_json(
    system_prompt: str,
    user_prompt: str,
    *,
    max_tokens: int | None = None,
    temperature: float = 0.0,
) -> dict:
    """
    Single JSON chat completion against any OpenAI-compatible endpoint.

    Retries transient errors with exponential backoff, then raises. Callers
    are expected to catch and fall back to heuristics.

    Circuit breaker: if open, fails fast without attempting the call.
    """
    if not settings.has_llm:
        raise RuntimeError(
            f"No LLM API key configured for {settings.llm_base_url} "
            f"(set LLM_API_KEY in .env)"
        )

    # Check circuit breaker before attempting
    cb = circuit_breaker()
    if cb.is_open:
        raise RuntimeError("LLM circuit breaker open - falling back to heuristics")

    client = await get_client()

    def build_payload() -> dict:
        p: dict = {
            "model": settings.llm_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens or settings.llm_max_tokens,
        }
        if settings.llm_json_mode:
            p["response_format"] = {"type": "json_object"}
        if settings.llm_disable_thinking:
            # Nemotron-3 / DeepSeek-R1 style models otherwise spend the whole
            # token budget reasoning and return an empty content field.
            p["chat_template_kwargs"] = {"enable_thinking": False}
        return p

    payload = build_payload()
    last_exc: Exception | None = None

    for attempt in range(3):
        try:
            # Charge every real HTTP attempt against the per-minute budget,
            # including retries, before it leaves the process.
            await _limiter_fixed().acquire()
            async with _semaphore_fixed():
                # Per-call wall-clock timeout so a single dead endpoint cannot
                # monopolise the event loop for minutes. asyncio.wait_for
                # cancels the underlying httpx request on timeout.
                resp = await asyncio.wait_for(
                    client.post("/chat/completions", json=payload),
                    timeout=LLM_CALL_TIMEOUT_SECONDS,
                )

            # 503 = provider overloaded. Retrying burns the per-minute budget
            # and the backoff sleep blocks the event loop, so bail out
            # immediately and let the classifier fall back to heuristics.
            if resp.status_code == 503:
                await cb.record_failure()
                raise RuntimeError(
                    "provider returned 503 (overloaded) - "
                    "falling back to heuristics"
                )

            # Some providers reject response_format - degrade once, loudly.
            if resp.status_code == 400 and "response_format" in payload:
                detail = resp.text.lower()
                if "response_format" in detail or "json" in detail:
                    log.warning(
                        "Endpoint rejected response_format=json_object; "
                        "retrying with prompt-only JSON instructions."
                    )
                    payload.pop("response_format", None)
                    settings.llm_json_mode = False
                    continue

            # Some providers reject chat_template_kwargs - drop it silently.
            if resp.status_code == 400 and "chat_template_kwargs" in payload:
                if "chat_template" in resp.text.lower():
                    log.warning(
                        "Endpoint rejected chat_template_kwargs; continuing "
                        "with the model's default reasoning behaviour."
                    )
                    payload.pop("chat_template_kwargs", None)
                    settings.llm_disable_thinking = False
                    continue

            if resp.status_code == 429 or resp.status_code >= 500:
                raise httpx.HTTPStatusError(
                    f"retryable {resp.status_code}", request=resp.request, response=resp
                )
            if resp.status_code in (401, 403):
                raise RuntimeError(
                    f"LLM auth rejected ({resp.status_code}) for "
                    f"model '{settings.llm_model}' at {settings.llm_base_url}. "
                    f"Check your API key."
                )
            resp.raise_for_status()

            data = resp.json()
            message = (data.get("choices") or [{}])[0].get("message", {})
            content = message.get("content") or ""

            # Reasoning models can exhaust the budget before emitting content.
            if not content.strip():
                reasoning = message.get("reasoning_content") or ""
                if reasoning and "chat_template_kwargs" not in payload:
                    log.warning(
                        "Model returned no content (reasoning model). "
                        "Retrying with thinking disabled."
                    )
                    payload["chat_template_kwargs"] = {"enable_thinking": False}
                    settings.llm_disable_thinking = True
                    continue
                raise ValueError(
                    "empty model response - the model returned no content "
                    "(reasoning model with an exhausted token budget?)"
                )
            await cb.record_success()
            return extract_json(content)

        except RuntimeError as exc:
            if "circuit breaker" in str(exc) or "503" in str(exc) or "auth rejected" in str(exc):
                raise  # auth failures, 503, and open breaker are terminal - do not retry
            raise
        except asyncio.TimeoutError:
            await cb.record_failure()
            raise
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            wait = 0.5 * (2**attempt)
            log.warning(
                "LLM call failed (attempt %d/3): %s - retrying in %.1fs",
                attempt + 1, exc, wait,
            )
            await asyncio.sleep(wait)

    await cb.record_failure()
    raise RuntimeError(f"LLM call failed after 3 attempts: {last_exc}")


async def health_check() -> dict:
    """
    Cheap provider probe used by /api/health and --check-llm.

    Returns {"ok": bool, "detail": str}. Never raises.
    """
    if not settings.has_llm:
        return {"ok": False, "detail": "no API key configured (heuristic mode)"}
    try:
        # Generous budget: reasoning models may need room even when disabled.
        result = await chat_json(
            "Reply with only this JSON object and nothing else.",
            'Return exactly: {"ok": true}',
            max_tokens=256,
        )
        return {"ok": bool(result), "detail": f"model={settings.llm_model}"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "detail": str(exc)[:300]}
