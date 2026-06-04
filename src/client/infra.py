#!/usr/bin/env python3
# ============================================================================
# openagent-api - Infra Client
# Maintainer: William McKeon
# Outbound HTTP client for the openagent-infra upstream
# ============================================================================
#
# ROLE:
#   InfraClient encapsulates the outbound HTTP boundary from openagent-api
#   to openagent-infra. It is one of two client modules in src/client/ —
#   the other being LoggerClient (src/client/logger.py) — and follows the
#   same shape: owns its own httpx.AsyncClient, owns its own timeout config,
#   owns its own header attachment, exposes lifecycle methods
#   (start/stop/aclose), and presents a small typed surface for callers in
#   backend/api.py.
#
#   backend/api.py imports InfraClient from client.infra alongside
#   LoggerClient from client.logger, instantiates it at lifespan startup,
#   calls stream_chat() inside sse_pump's async-with, calls check_health()
#   inside the health_check endpoint, and calls stop() at lifespan shutdown.
#
#   The client is transport-only. It does not know about request_id,
#   session_id, the per-call latency timer, or the conversation_capture
#   accumulator — those all belong to backend/api.py. It does not parse the
#   SSE chunk format, does not emit events to openagent-logger, and does not
#   retry. Errors from /chat propagate to the caller; /health errors are
#   absorbed and reported as an "unreachable" status.
#
# CONTRACT:
#
#   Construction is config-only — no I/O happens until start() is called.
#   This lets backend/api.py instantiate the client inside the lifespan
#   handler before opening any sockets, and lets the client validate its
#   inputs up front:
#
#     client = InfraClient(
#         url=config.INFRA_URL,
#         api_key=config.INFRA_API_KEY,
#         connect_timeout=config.UPSTREAM_CONNECT_TIMEOUT,
#         read_timeout=config.UPSTREAM_READ_TIMEOUT,
#         health_timeout=config.HEALTH_TIMEOUT,
#     )
#     await client.start()
#
#   start() is idempotent — calling it twice has no effect on the second
#   call. This protects against accidental double-start in tests or in
#   pathological lifespan reentrancy.
#
#   stop() and aclose() are aliases for the same operation: close the
#   underlying httpx.AsyncClient. stop() is the FastAPI-lifespan-convention
#   name; aclose() matches httpx's own context-manager convention. Both are
#   idempotent.
#
#   stream_chat(payload) is a SYNCHRONOUS method that returns an async
#   context manager (httpx's _AsyncResponseContextManager). The caller uses
#   it with `async with`:
#
#     async with infra_client.stream_chat(payload) as upstream:
#         if upstream.status_code != 200:
#             body = await upstream.aread()
#             ...
#         async for chunk in upstream.aiter_raw():
#             ...
#
#   The yielded object is an httpx.Response with .status_code, .aread(), and
#   .aiter_raw(). No wrapping. Exceptions raised by httpx (ConnectTimeout,
#   ConnectError, ReadTimeout, RemoteProtocolError, HTTPError, etc.)
#   propagate to the caller — they must be caught by sse_pump in
#   backend/api.py so it can emit upstream_error ops_events with the
#   request_id context (which this client deliberately does not know about).
#
#   check_health() is the only method that catches errors internally. It
#   returns Tuple[str, Optional[Dict[str, Any]]]:
#       status_str  — one of "ok", "loading", "unreachable"
#       raw_body    — the parsed JSON dict openagent-infra returned, or None
#                     if no body was obtained or it was unparseable
#   Any httpx error, non-200 status, JSON-parse error, or non-dict body
#   translates to ("unreachable", None). The caller's /health response shape
#   stays consistent regardless of what went wrong. check_health() NEVER
#   raises.
#
#   The shape of the dict inside raw_body is openagent-infra's
#   responsibility — this client forwards it through unchanged without
#   parsing or renaming any keys. openagent-infra's body contains base_model
#   and nervous_system fields (each "ok" | "unreachable" | "not configured")
#   alongside its proxy and status fields. The top-level "status" field
#   semantics ("ok" vs "degraded") are what the translation table in the
#   RULES section below keys on. backend/api.py drops raw_body directly into
#   its /health response under openagent_infra.raw, so the frontend gets an
#   accurate snapshot of the upstream's own view of itself — informational
#   only; the frontend's gate-open loop reads the translated top-level
#   "status" field, not the raw body keys.
#
# RULES — WHAT THIS FILE MUST NEVER DO:
#   ❌ Catch httpx exceptions in stream_chat(). Those need to propagate to
#      sse_pump in backend/api.py so it can emit upstream_error ops_events
#      with the original exception class name AND the request_id context.
#      The client is transport-only; error-event emission is request-handler
#      logic that lives a layer up.
#   ❌ Buffer the upstream response in memory. stream_chat() exposes a
#      streaming context manager; it must never collect the full body into a
#      single bytes object. A scale-to-zero provider cold-start can take
#      minutes and high-effort reasoning generations several minutes —
#      buffering would defeat the whole point of SSE.
#   ❌ Parse or decode the SSE chunk format. The relay is byte-for-byte;
#      that is its job. The side-channel parser in backend/api.py's sse_pump
#      owns chunk decoding for the conversation_capture accumulator. Adding
#      chunk-format awareness here would couple the client to a
#      provider-specific shape that may change in the future.
#   ❌ Hold per-request state. The client is a process-lifetime singleton
#      managed by FastAPI's lifespan. request_id, session_id, the per-call
#      latency timer, and the conversation_capture accumulator all belong to
#      backend/api.py and never travel through here.
#   ❌ Log the X-API-Key (api_key) value. The key is supplied at
#      construction and pre-attached to the httpx.AsyncClient at start()
#      time; logging it would defeat the compartmentalized-auth security
#      model documented in README.md and DATASHEET.md.
#   ❌ Emit events to openagent-logger. Logger emission is the caller's
#      responsibility (backend/api.py uses LoggerClient for this; InfraClient
#      and LoggerClient are siblings, not composed). /health calls
#      deliberately do NOT emit per openagent-logger DATASHEET §6.1 —
#      emitting on every 3-second poll would flood the capture layer with no
#      operational value.
#   ❌ Retry failed calls. Both /chat and /health are no-retry by design.
#      Failures bubble up; the caller decides what to do with them. (Adding
#      retry here would compound latency on the already-slow cold-start
#      path.)
#
# RULES — WHAT THIS FILE MUST ALWAYS DO:
#   ✅ Use these timeout values: connect=10s (configurable via
#      OPENAGENT_UPSTREAM_CONNECT_TIMEOUT), read=None i.e. unbounded
#      (configurable via OPENAGENT_UPSTREAM_READ_TIMEOUT), write=10s, pool=5s
#      for the streaming client. /health uses a separate short timeout (5s,
#      configurable via OPENAGENT_HEALTH_TIMEOUT). A finite read timeout
#      would kill long but legitimate generations.
#   ✅ Attach X-API-Key as a default header on the httpx.AsyncClient at
#      start() time. Every outbound request inherits it; no per-call
#      attachment code anywhere.
#   ✅ Translate openagent-infra's status vocabulary in check_health() per
#      this table:
#        "ok"                → "ok"
#        "degraded"          → "loading"   (provider worker cold-starting)
#        "loading"           → "loading"   (kept for backward compat)
#        anything else       → "unreachable"
#        non-200 response    → "unreachable"
#        unparseable body    → "unreachable"
#        httpx error         → "unreachable"
#   ✅ Return the raw upstream body alongside the translated status from
#      check_health(), so the caller can include it under
#      `openagent_infra.raw` in the /health response payload.
#   ✅ Match LoggerClient's lifecycle surface: start(), stop(), aclose()
#      (alias for stop()). FastAPI's lifespan calls start() at startup (after
#      env-var validation but before yielding to uvicorn) and stop() at
#      shutdown (after LoggerClient.stop() so pending events drain before the
#      upstream connection pool tears down).
#   ✅ Log a single INFO line at start() and a single INFO line at stop()
#      matching the LoggerClient style — "InfraClient started (url=...,
#      connect_timeout=...s, read_timeout=...)" and "InfraClient closed."
#      These show up in operator logs alongside the LoggerClient lines so
#      cross-service correlation stays clean.
# ============================================================================

from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Tuple

import httpx


# Use the same module-level logger backend/api.py uses ("openagent-api"), so
# our log lines interleave cleanly with the rest of the service's output and
# operators see one timeline. No separate logger configuration.
logger = logging.getLogger("openagent-api")


class InfraClient:
    """
    Outbound HTTP client for openagent-infra.

    Wraps a single httpx.AsyncClient with timeouts and X-API-Key
    pre-attached. Exposes the two methods backend/api.py needs:
    ``stream_chat()`` for the /chat streaming POST and ``check_health()`` for
    the /health proxy. Lifecycle is managed by FastAPI's lifespan handler —
    call ``start()`` at startup and ``stop()`` at shutdown.

    Construction is pure config — no I/O happens until ``start()``. Both
    ``start()`` and ``stop()`` are idempotent.

    Attributes:
        url: Base URL of openagent-infra (no trailing slash). Set at
             construction time.
        connect_timeout: Seconds to wait for the TCP connection to
             openagent-infra (default 10.0). Short — if openagent-infra is
             unreachable, fail fast.
        read_timeout: Seconds to wait for the next chunk of data from
             openagent-infra. None means unbounded (default), which is
             required because provider cold-starts can take minutes and
             high-effort reasoning generations several minutes.
        health_timeout: Short timeout for /health calls (default 5.0). The
             /health endpoint is polled every 3 seconds by the frontend's
             gate-open loop and must never block.
    """

    def __init__(
        self,
        url: str,
        api_key: str,
        connect_timeout: float = 10.0,
        read_timeout: Optional[float] = None,
        health_timeout: float = 5.0,
    ) -> None:
        """
        Configure the client. No I/O happens here.

        Args:
            url: Base URL of openagent-infra. Trailing slashes are stripped.
                 Must be non-empty.
            api_key: X-API-Key value (INFRA_API_KEY) attached as a default
                 header on every outbound request. Never logged.
            connect_timeout: TCP connect timeout in seconds.
            read_timeout: Per-chunk read timeout in seconds, or None for
                 unbounded.
            health_timeout: Override timeout for /health calls.

        Raises:
            ValueError: if url or api_key is empty.
        """
        if not url:
            raise ValueError(
                "InfraClient.url is required (got empty string)."
            )
        if not api_key:
            raise ValueError(
                "InfraClient.api_key is required (got empty string)."
            )
        self.url: str = url.rstrip("/")
        self._api_key: str = api_key
        self.connect_timeout: float = connect_timeout
        self.read_timeout: Optional[float] = read_timeout
        self.health_timeout: float = health_timeout
        self._http_client: Optional[httpx.AsyncClient] = None

    async def start(self) -> None:
        """
        Open the underlying httpx.AsyncClient.

        Idempotent — calling start() on an already-started client is a no-op
        (no second client is created, no log line is emitted). This protects
        against double-start in tests and in pathological lifespan
        reentrancy.

        Timeout configuration:
          connect: configurable, default 10s (fail fast on unreachable
                   upstream)
          read:    configurable, default None / unbounded (a provider
                   cold-start can take minutes; high-effort reasoning
                   generations several minutes; a finite read timeout would
                   kill long but legitimate generations)
          write:   10s (request body is tiny — messages list plus optional
                   reasoning_effort)
          pool:    5s (we never want to block on the pool)

        The X-API-Key header is attached at the AsyncClient level so every
        outbound request inherits it automatically.
        """
        if self._http_client is not None:
            return

        timeout = httpx.Timeout(
            connect=self.connect_timeout,
            read=self.read_timeout,  # None = unbounded
            write=10.0,
            pool=5.0,
        )
        self._http_client = httpx.AsyncClient(
            base_url=self.url,
            timeout=timeout,
            headers={"X-API-Key": self._api_key},
        )

        read_label = (
            "unbounded"
            if self.read_timeout is None
            else f"{self.read_timeout}s"
        )
        logger.info(
            f"InfraClient started "
            f"(url={self.url}, "
            f"connect_timeout={self.connect_timeout}s, "
            f"read_timeout={read_label})"
        )

    async def stop(self) -> None:
        """
        Close the underlying httpx.AsyncClient.

        Idempotent — calling stop() on an already-stopped (or never-started)
        client is a no-op. Errors during close are caught and logged as
        warnings, never raised, because the FastAPI lifespan shutdown path
        should not fail the whole shutdown sequence over a transient close
        error.
        """
        if self._http_client is None:
            return

        try:
            await self._http_client.aclose()
            logger.info("InfraClient closed.")
        except Exception as err:
            logger.warning(
                f"Error closing InfraClient: "
                f"{type(err).__name__}: {err}"
            )
        finally:
            self._http_client = None

    async def aclose(self) -> None:
        """
        Alias for stop().

        Provided so external code can use ``async with`` semantics or treat
        the client like an httpx.AsyncClient if desired. Functionally
        identical to stop().
        """
        await self.stop()

    def stream_chat(self, payload: Dict[str, Any]):
        """
        Open a streaming POST to openagent-infra's /chat endpoint.

        Returns the async context manager from httpx.AsyncClient.stream()
        directly — no wrapping. The caller uses it as::

            async with infra_client.stream_chat(payload) as upstream:
                if upstream.status_code != 200:
                    body = await upstream.aread()
                    ...
                async for chunk in upstream.aiter_raw():
                    yield chunk
                    ...

        Exceptions raised by httpx (ConnectTimeout, ConnectError,
        ReadTimeout, RemoteProtocolError, HTTPError, plus the generic
        Exception catch-all) propagate out of the ``async with`` block in the
        caller — they must be caught by sse_pump in backend/api.py so it can
        emit upstream_error ops_events with the request_id context that this
        client deliberately does not know about.

        This method is SYNCHRONOUS (no async def, no await). It returns the
        async context manager that httpx.stream() returns; the caller awaits
        the context manager's __aenter__ via ``async with``. This is httpx's
        idiomatic streaming pattern.

        Args:
            payload: The JSON request body for openagent-infra. Typically
                ``{"messages": [...], "reasoning_effort": "..."}``. The system
                message (bio.txt) prepending and the optional reasoning_effort
                field are the caller's responsibility; this method just
                forwards the dict.

        Returns:
            An async context manager yielding an httpx.Response with
            ``status_code``, ``aread()``, and ``aiter_raw()`` available.

        Raises:
            RuntimeError: if start() was not called before stream_chat().
                          This is a programming error, not a runtime
                          condition — a properly-configured lifespan handler
                          always calls start() at startup.
        """
        if self._http_client is None:
            raise RuntimeError(
                "InfraClient.stream_chat() called before start(). "
                "Ensure the FastAPI lifespan handler invokes start() "
                "at startup."
            )
        return self._http_client.stream(
            "POST",
            "/chat",
            json=payload,
        )

    async def check_health(self) -> Tuple[str, Optional[Dict[str, Any]]]:
        """
        Call openagent-infra's /health endpoint and translate the status.

        This method encapsulates the entire health-check round-trip: the GET
        request, the timeout, the response parsing, and the status-vocabulary
        translation. The caller in backend/api.py gets back a tuple it can
        drop straight into its own /health response payload.

        Status translation (openagent-infra → openagent-api):
          "ok"                → "ok"
          "degraded"          → "loading"   (provider worker cold-starting)
          "loading"           → "loading"   (backward-compat for older
                                             contract)
          anything else       → "unreachable"
          non-200 response    → "unreachable"
          unparseable body    → "unreachable"
          httpx error         → "unreachable"

        The ``degraded`` → ``loading`` translation deserves a note.
        openagent-infra reports ``degraded`` when its own FastAPI proxy is
        healthy but its upstream provider (the base model openagent-api
        routes to) is cold-starting (a scale-to-zero provider can take
        minutes to spin up on the first request). Semantically that is the
        same condition the frontend has always called ``loading`` — model
        isn't ready, hold the chat input closed — so we translate the field
        value here. The frontend's gate-open polling loop keeps reading
        consistent semantics regardless of which upstream contract version
        it's against.

        This method NEVER raises. Any httpx error, non-200 status, JSON-parse
        error, or non-dict body translates to ("unreachable", None). The
        caller's /health response shape stays consistent regardless of what
        went wrong upstream.

        Logging is intentionally at DEBUG level. The /health endpoint is
        polled every 3 seconds; logging at INFO would flood operator logs
        with thousands of near-identical lines per hour with no operational
        value. Set OPENAGENT_LOG_LEVEL=DEBUG to see these lines during
        troubleshooting.

        Returns:
            Tuple of (status_str, raw_body) where:
              * status_str is one of "ok", "loading", "unreachable"
              * raw_body is the parsed JSON dict openagent-infra returned, or
                None if no body was obtained or it was unparseable. The
                caller embeds this under ``openagent_infra.raw`` in its own
                /health response. The body contains base_model and
                nervous_system fields (each "ok" | "unreachable" | "not
                configured") alongside proxy and status. This client forwards
                the body through unchanged; the body shape does not affect
                the status translation above because the top-level "status"
                field semantics ("ok" vs "degraded") are what it keys on.
        """
        if self._http_client is None:
            logger.debug(
                "InfraClient.check_health() called before start() "
                "— returning unreachable."
            )
            return "unreachable", None

        # ---------- GET request ----------
        try:
            resp = await self._http_client.get(
                "/health",
                timeout=self.health_timeout,
            )
        except httpx.HTTPError as err:
            logger.debug(f"openagent-infra /health unreachable: {err}")
            return "unreachable", None

        # ---------- non-200 ----------
        if resp.status_code != 200:
            logger.debug(
                f"openagent-infra /health returned {resp.status_code}"
            )
            return "unreachable", None

        # ---------- parse body ----------
        try:
            raw_body = resp.json()
        except Exception as parse_err:
            logger.debug(
                f"Could not parse /health body: {parse_err}"
            )
            return "unreachable", None

        if not isinstance(raw_body, dict):
            # Defensive: json.loads can return list/int/str for inputs that
            # are valid JSON but not the shape we expect.
            return "unreachable", None

        # ---------- translate status vocabulary ----------
        raw_status = raw_body.get("status", "unknown")
        if raw_status == "ok":
            return "ok", raw_body
        if raw_status == "degraded":
            # openagent-infra: provider cold-starting. Semantically
            # equivalent to "loading" from the frontend's perspective.
            return "loading", raw_body
        if raw_status == "loading":
            # Older contract version. Kept for backward compatibility in case
            # a deployment is still on the previous status vocabulary.
            return "loading", raw_body
        return "unreachable", raw_body


# ============================================================================
# END OF FILE
# ============================================================================