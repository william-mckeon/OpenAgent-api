#!/usr/bin/env python3
# ============================================================================
# openagent-api - FastAPI Backend Gateway
# Maintainer: William McKeon
# The identity gateway for OpenAgent
# ============================================================================
#
# ROLE:
#   openagent-api is the product-layer backend of the OpenAgent system. It
#   is the only HTTP boundary between openagent-frontend and openagent-infra.
#   The frontend talks only to openagent-api; it never reaches
#   openagent-infra directly and does not carry the persona — both
#   responsibilities live here.
#
#   Five concerns live in this file and nowhere else (the fifth is OPTIONAL,
#   active only when openagent-memory is configured):
#     1. Auth boundary — validates OPENAGENT_API_KEY inbound, attaches
#        INFRA_API_KEY outbound to openagent-infra, LOGGER_API_KEY outbound
#        to openagent-logger, and (when memory is enabled) MEMORY_API_KEY
#        outbound to openagent-memory. Independent secrets at independent
#        boundaries. The frontend never sees INFRA_API_KEY, LOGGER_API_KEY,
#        or MEMORY_API_KEY. openagent-infra never sees OPENAGENT_API_KEY,
#        LOGGER_API_KEY, or MEMORY_API_KEY. openagent-logger never sees
#        OPENAGENT_API_KEY, INFRA_API_KEY, or MEMORY_API_KEY.
#     2. Identity owner + prompt assembly — loads bio.txt once at startup
#        and prepends it as the first system message on every upstream /chat
#        call. When memory is enabled, openagent-api also OWNS the final
#        query assembly: [bio] + [retrieved older turns] + [recent N turns
#        verbatim] + [current user turn]. Memory only ranks; the gateway
#        builds the prompt.
#     3. SSE relay — orchestrates a streaming POST to openagent-infra via
#        InfraClient (see src/client/infra.py), pipes the SSE
#        byte-for-byte back to the frontend including the data: prefix and
#        the [DONE] sentinel, and detects mid-stream client disconnects.
#     4. Logger emission — emits operational events (request_received,
#        upstream_call, upstream_error, stream_complete, and — when memory
#        is enabled — memory_retrieve_degraded and memory_ingest_error) and
#        full conversation captures to openagent-logger via the
#        fire-and-forget LoggerClient (src/client/logger.py). The /chat hot
#        path never blocks on logger availability — emissions enqueue and a
#        background asyncio task drains the queue. If openagent-logger is
#        unreachable, events are queued and eventually dropped if the queue
#        overflows (drop-oldest policy). See src/client/logger.py for the
#        full integration contract.
#     5. Memory retrieval + ingest (OPTIONAL) — when openagent-memory is
#        configured, openagent-api calls MemoryClient.retrieve() on the hot
#        path BEFORE assembling the prompt (awaited, bounded, fail-open) and
#        fires MemoryClient.ingest_turn_pair_background() AFTER a clean
#        stream (off the user's path, a tracked background task). Memory is
#        OPT-IN (enabled by the presence of MEMORY_URL + MEMORY_API_KEY) and
#        is NOT a refuse-to-boot dependency — when it is unconfigured or its
#        session_id is unset, openagent-api forwards the full history exactly
#        as it did before memory existed. See src/client/memory.py for the
#        full contract.
#
#   reasoning_effort is an optional pass-through of the OpenAI-style field
#   (low / medium / high). When the frontend sends it, openagent-api
#   forwards it upstream so openagent-infra can adjust the model's reasoning
#   depth. When the frontend omits it, openagent-api does not include it in
#   the upstream payload — openagent-infra applies its own server-side
#   default. openagent-api itself holds no default for this field (pure
#   pass-through) to avoid two sources of truth for the same setting.
#
# ARCHITECTURE:
#
#   Browser
#     │
#     │ HTTPS / WebSocket (Streamlit on host port 8000)
#     ▼
#   openagent-frontend  (Streamlit on container :8501, host :8000)
#     │
#     │ HTTP POST /chat  {messages, reasoning_effort?}  (no system msg)
#     │ HTTP GET  /health                               (gates input)
#     │ X-API-Key: OPENAGENT_API_KEY
#     ▼
#   openagent-api       (THIS FILE — uvicorn on :8001)
#     │
#     ├─→ openagent-infra (HOT PATH — synchronous, blocks /chat response)
#     │   via InfraClient (src/client/infra.py)
#     │   HTTP POST /chat  {messages, reasoning_effort?}  (system prepended)
#     │   HTTP GET  /health                               (forwarded readiness)
#     │   X-API-Key: INFRA_API_KEY
#     │   │
#     │   │ openagent-infra forwards to the configured BYOC provider (an
#     │   │ OpenAI-compatible chat-completions endpoint) and injects the
#     │   │ reasoning-effort level into the request.
#     │   ▼
#     │   BYOC provider — base model (default route)
#     │     │
#     │     │ openagent-api sends no model selector, so openagent-infra
#     │     │ routes every /chat here to its default (base) model.
#     │     │ openagent-infra also exposes a fast nervous-system model on a
#     │     │ separate endpoint, but openagent-api never routes to it — that
#     │     │ is openagent-infra's internal capability, not visible to this
#     │     │ file.
#     │     │
#     │     ▼ SSE events — each one is a JSON-encoded OpenAI ChatCompletion
#     │       chunk (the model server's native streaming format):
#     │
#     │         data: {"id":"chatcmpl-...","object":"chat.completion.chunk",
#     │                "choices":[{"delta":{"reasoning":"<tok>"}}]}\n\n
#     │         ...  (reasoning tokens stream first)
#     │         data: {"id":"chatcmpl-...","choices":[{"delta":{"content":"<tok>"}}]}\n\n
#     │         ...  (then visible answer tokens)
#     │         data: {"id":"chatcmpl-...","choices":[{"delta":{},"finish_reason":"stop"}]}\n\n
#     │         data: [DONE]\n\n
#     │
#     ├─→ openagent-memory (OPTIONAL — session-scoped RAG; only when configured)
#     │   via MemoryClient (src/client/memory.py)
#     │   HOT PATH  (bounded, fail-open):  POST /retrieve  (before assembly)
#     │   OFF-PATH  (tracked task):        POST /ingest ×2 (after success)
#     │   X-API-Key: MEMORY_API_KEY
#     │   Retrieval degrades to "recent turns only" if memory/its embedder
#     │   is unavailable; ingest failures are logged + emitted, never block.
#     │
#     └─→ openagent-logger (FIRE-AND-FORGET — never blocks /chat)
#         via LoggerClient (src/client/logger.py)
#         HTTP POST /events  {ops_event | conversation_capture}
#         X-API-Key:        LOGGER_API_KEY
#         HMAC-SHA256 sig:  computed per-event with LOGGER_HMAC_SECRET
#         Async drain:      in-process asyncio.Queue + background task
#                           (see src/client/logger.py for full contract)
#         Emission points per /chat:
#           request_received        ───→ ops_event (at ingress)
#           memory_retrieve_degraded ──→ ops_event (memory path, on degrade)
#           upstream_call           ───→ ops_event (before openagent-infra POST)
#           upstream_error          ───→ ops_event (on any upstream failure)
#           stream_complete         ───→ ops_event (after SSE pump finishes)
#           conversation_capture         (after successful stream_complete only)
#           memory_ingest_error     ───→ ops_event (memory path, on ingest fail)
#
#   Each chunk's choices[0].delta carries either a reasoning token
#   (chain-of-thought, in the model's reasoning format) or a content token
#   (the visible answer). The frontend decodes the JSON and routes the two
#   streams separately per its UX policy. openagent-api does no parsing on
#   the relay path — bytes flow through this service unchanged. A
#   SIDE-CHANNEL parser inside sse_pump watches the bytes as they fly past,
#   extracts delta.content tokens into an accumulator, and emits the result
#   as conversation_capture.output_text once the stream ends. The parser
#   runs AFTER the yield, so it adds zero latency to the frontend-facing
#   relay. It is request-handler logic that lives alongside the
#   conversation_capture emission, not transport logic.
#
# PORT CONVENTION:
#   8000 = openagent-frontend (user-facing)
#   8001 = openagent-api      (internal API the frontend talks to)
#   8002 = openagent-infra    (FastAPI proxy; only openagent-api calls it)
#   8003 = openagent-logger   (capture layer; only openagent-api emits to it)
#   8004 = openagent-memory   (RAG layer; only openagent-api calls it)
#   BYOC provider = remote inference, reached only by openagent-infra
#
# DOCKER SERVICE:
#   This file runs as the openagent-api service in this repo's
#   docker-compose.yml:
#     Command:    uvicorn backend.api:app --host 0.0.0.0 --port 8001
#     PYTHONPATH: /app/src  (set in Dockerfile)
#     Volumes:    none (bio.txt is baked into the image at build time)
#     Networks:   reachable by openagent-frontend; reaches openagent-infra,
#                 openagent-logger, and (when configured) openagent-memory.
#
# IMPORT PATH RULE:
#   PYTHONPATH is /app/src inside the container. The package form for
#   backend modules is `from backend.<module> import ...`. The package form
#   for outbound clients is `from client.<service> import ...` — see
#   src/client/__init__.py. The package holds three client classes:
#   LoggerClient (src/client/logger.py), InfraClient (src/client/infra.py),
#   and MemoryClient (src/client/memory.py). openagent-api is its own
#   service in its own repo and imports nothing from any other service's
#   repo. All inter-service communication is over HTTP.
#
# RULES — WHAT THIS FILE MUST NEVER DO:
#   ❌ Connect to a database, run a router, call an agent, load tools, hold
#      conversation history, or do anything resembling routing, agent
#      orchestration, or conversation-history behaviour. openagent-api is a
#      stateless SSE gateway with a side-channel capture emitter, and
#      nothing more. (Memory storage lives in openagent-memory's OWN
#      database, never here; openagent-api only ranks-via-retrieve and
#      writes-via-ingest over HTTP.)
#   ❌ Buffer the upstream stream into memory before forwarding. The whole
#      point of streaming is that the user sees tokens as they arrive. A
#      scale-to-zero provider cold-start can take minutes on first request,
#      and even at low reasoning effort generation takes several seconds —
#      burying that behind a spinner would ruin the UX.
#   ❌ Inspect, parse, or split the model's reasoning format ON THE RELAY
#      PATH. The current upstream emits OpenAI ChatCompletion chunks (one
#      JSON object per SSE event) where choices[0].delta.reasoning carries
#      chain-of-thought tokens and choices[0].delta.content carries the
#      visible answer; future models or upstream contracts may change the
#      shape entirely. openagent-api forwards bytes byte-for-byte and stays
#      out of the reasoning-display policy decision (which lives at the
#      frontend layer). The SIDE-CHANNEL accumulator parses chunks AFTER
#      they have been yielded to the frontend — never before, never
#      blocking — and any parse failure is silently tolerated rather than
#      affecting the relay.
#   ❌ Apply a server-side default for reasoning_effort. openagent-api is
#      pure pass-through — if the frontend omits the field, openagent-api
#      omits it from the upstream payload and openagent-infra applies its
#      own default. Adding an openagent-api default would create two
#      sources of truth for the same setting.
#   ❌ Block /chat on a memory call. retrieve() is bounded + fail-open and is
#      the only memory call on the hot path; ingest runs off the user's path
#      as a detached background task. A memory outage degrades retrieval to
#      "recent turns only" and never delays the user's first token or fails
#      the request.
#   ❌ Leak INFRA_API_KEY downstream — never log it, never echo it back to
#      the frontend, never include it in error responses. It lives in this
#      service's environment and goes upstream only. The key is owned by
#      InfraClient (stored on the client at construction and pre-attached
#      to the AsyncClient at start()); api.py reads it from config only to
#      pass it to the InfraClient constructor.
#   ❌ Leak LOGGER_API_KEY, LOGGER_HMAC_SECRET, or MEMORY_API_KEY anywhere.
#      They live in this service's environment and are passed once to their
#      respective clients at startup; never logged, never echoed, never
#      forwarded to the wrong boundary or the frontend.
#   ❌ Trust INFRA_API_KEY, OPENAGENT_API_KEY, LOGGER_API_KEY, and
#      MEMORY_API_KEY to be the same value. They are independently generated
#      and validated against different boundaries.
#   ❌ Block /chat on a logger emission. The fire-and-forget contract is
#      sacred — emit methods on LoggerClient are synchronous and do not
#      perform I/O; they enqueue and return in microseconds. If you ever
#      feel tempted to `await logger_client.emit_...()` stop and read
#      src/client/logger.py.
#   ❌ Retry logger emissions on failure. The LoggerClient drops events on
#      POST failure by design (per openagent-logger DATASHEET §7's
#      accepted-loss framing). Retrying inside openagent-api would just pile
#      up memory pressure.
#   ❌ Emit conversation_capture (or ingest to memory) for streams that did
#      not complete successfully. Captures and ingests are for the completed
#      turn; partial or errored streams aren't useful. stream_complete with
#      outcome=client_disconnect or outcome=failure is enough — no capture,
#      no ingest.
#   ❌ Allow CORS origin "*". The frontend is the only legitimate client.
#   ❌ Pass through frontend-supplied system messages. bio.txt is the
#      authoritative OpenAgent persona. Any "system" role in the inbound
#      request body is dropped before forwarding upstream.
#   ❌ Catch the httpx exceptions inside InfraClient.stream_chat(). They
#      must propagate to sse_pump in this file so upstream_error ops_events
#      can be emitted with the request_id context. The client is
#      transport-only; error-event emission is request-handler logic and
#      lives here.
#
# RULES — WHAT THIS FILE MUST ALWAYS DO:
#   ✅ Validate OPENAGENT_API_KEY on every inbound /chat request. Return
#      401 when missing or wrong, with detail "Invalid or missing API key"
#      so the frontend's emoji classifier surfaces 🔐.
#   ✅ Validate OPENAGENT_API_KEY on inbound /health requests too. The
#      /health endpoint is sensitive enough to deserve auth — it reveals
#      whether the upstream is reachable, which is internal operational
#      state. (openagent-infra exposes its own /health to unauthenticated
#      callers; openagent-api still authenticates its /health because
#      operational state at this boundary is something the gateway owns and
#      chooses to protect.)
#   ✅ Attach X-API-Key: <INFRA_API_KEY> to every outbound call to
#      openagent-infra. InfraClient handles this internally — its
#      httpx.AsyncClient is constructed with the header pre-attached at
#      start() time.
#   ✅ Attach X-API-Key: <LOGGER_API_KEY> to every outbound call to
#      openagent-logger, and X-API-Key: <MEMORY_API_KEY> to every outbound
#      call to openagent-memory. The LoggerClient and MemoryClient each
#      handle this internally — their httpx.AsyncClient is constructed with
#      the header pre-attached.
#   ✅ Load bio.txt once at startup and prepend it as the first
#      {"role": "system", "content": ...} message on every upstream /chat
#      request.
#   ✅ Validate reasoning_effort if present — must be one of "low",
#      "medium", or "high". Pydantic enforces this with a regex pattern;
#      invalid values produce HTTP 422 automatically.
#   ✅ Forward reasoning_effort to openagent-infra ONLY when the frontend
#      sent one. Omit the field entirely from the upstream payload when the
#      frontend did not send it (pure pass-through).
#   ✅ Stream openagent-infra's SSE response back to the frontend
#      byte-for-byte. Each event is a JSON-encoded OpenAI ChatCompletion
#      chunk in the form:
#        data: {"id":...,"choices":[{"delta":{"reasoning":"<tok>"}}]}\n\n
#      for chain-of-thought tokens, or:
#        data: {"id":...,"choices":[{"delta":{"content":"<tok>"}}]}\n\n
#      for visible answer tokens. The stream terminates with a final
#      empty-delta chunk (finish_reason="stop") followed by:
#        data: [DONE]\n\n
#      openagent-api forwards them all unchanged. The frontend is
#      responsible for decoding the JSON and routing reasoning vs content
#      to whatever UI surface it wants.
#   ✅ Generate a request_id (uuid4 string) at the start of every /chat
#      call. This UUID is the correlation ID joining the request_received,
#      upstream_call, upstream_error, stream_complete ops_events and the
#      conversation_capture for that one request. It is also the value
#      openagent-logger uses as the canonical-string prefix when verifying
#      HMAC signatures.
#   ✅ Thread session_id through every emitted event. session_id comes from
#      the MEMORY_SESSION_ID env var today (it is null when memory is not
#      configured / no session_id is set; the frontend will mint and supply
#      it once it manages conversations). When set, the same value scopes
#      memory retrieve/ingest AND populates the logger's previously-null
#      session_id field, so events from one conversation can be joined.
#      user_id remains null on every event — the reference stack has no
#      per-user identity.
#   ✅ Emit a request_received ops_event after auth passes, before any
#      other work. The event captures that a /chat call arrived; HTTP
#      status of the response captures whether validation passed.
#   ✅ Emit an upstream_call ops_event immediately before opening the
#      streaming POST to openagent-infra. This event's timestamp is the
#      moment openagent-api hands control to InfraClient (which in turn
#      hands control to httpx).
#   ✅ Emit an upstream_error ops_event in EVERY exception handler in
#      sse_pump, with details.error_type set to the exception class name.
#      Emit BEFORE yielding the SSE error event to the frontend so the
#      operational signal lands even if the error event itself gets lost
#      (slow client, mid-stream disconnect, etc.).
#   ✅ Emit a stream_complete ops_event after the SSE pump finishes, with
#      outcome="success" on clean stream completion or
#      outcome="client_disconnect" if http_request.is_disconnected()
#      returned True mid-stream. Include bytes_relayed and latency_ms in
#      details so operators can correlate latency tails to throughput.
#   ✅ Emit a conversation_capture ONLY after a successful stream_complete
#      (outcome="success"). The capture's output_text is the assembled
#      visible-answer content (delta.content tokens concatenated). The
#      reasoning chain is NOT captured — see src/client/logger.py header
#      for the rationale.
#   ✅ When memory is active, fire ingest_turn_pair_background() ONLY after a
#      successful stream_complete, alongside the conversation_capture. It
#      ingests the user turn then the assistant turn off the user's path; a
#      client-disconnect or upstream-error turn ingests neither side.
#   ✅ Accumulate delta.content tokens via SIDE-CHANNEL JSON parsing while
#      still forwarding bytes byte-for-byte to the frontend. The yield to
#      the frontend happens BEFORE the parse in every chunk iteration, so
#      the parse adds zero latency to the user-visible stream. Parse
#      failures are silently tolerated (the accumulator may produce a
#      truncated or empty output_text on broken streams, but the relay
#      itself is unaffected).
#   ✅ Map upstream error conditions to the same status codes
#      openagent-frontend's emoji classifier already understands:
#        🔌 502 — cannot reach openagent-infra
#        ⏳ 503 — openagent-infra reports degraded (provider cold-start)
#        🔌 504 — read timeout during generation
#        🔐 401 — auth failure (ours, inbound)
#        ⚠️  400 — empty messages list / no user message
#        ⚠️  422 — malformed request body / invalid reasoning_effort
#        ❌ 500 — anything else
#   ✅ Treat openagent-infra's "degraded" /health status as an upstream
#      "loading" condition for the frontend. The state names differ between
#      the two services (openagent-infra reports the health of its upstream
#      provider; openagent-api preserves the field-name semantics the
#      frontend gate-open loop already understands). This translation is
#      performed inside InfraClient.check_health(); the mapping table itself
#      is documented in health_check().
#   ✅ Validate the inbound messages list (non-empty, contains a user
#      message) before any upstream work, mirroring openagent-infra's 400
#      contract so the frontend gets fast feedback.
#
# ENDPOINTS:
#   POST /chat    — SSE relay (StreamingResponse → text/event-stream) plus
#                   fire-and-forget logger emission (request_received,
#                   upstream_call, stream_complete, conversation_capture on
#                   success; or request_received, upstream_call,
#                   upstream_error on error). When memory is active, a
#                   memory_retrieve_degraded ops_event may precede the
#                   stream, and a background ingest (with possible
#                   memory_ingest_error events) follows a successful stream.
#   GET  /health  — proxied openagent-infra readiness
#
# SESSION STATE:
#   None in-process. Stateless across requests by design. session_id is read
#   from the MEMORY_SESSION_ID env var (a single static value for now) and
#   threaded onto every emitted event; it is null when memory is not
#   configured or MEMORY_SESSION_ID is unset. user_id is always null — the
#   reference stack has no per-user identity. openagent-api holds no
#   per-session conversation memory itself; durable conversation memory (when
#   enabled) lives in openagent-memory's own database, reached over HTTP.
# ============================================================================

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Dict, List, Optional, Set

import httpx
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from client.infra import InfraClient
from client.logger import LoggerClient
from client.memory import MemoryClient

# Load environment variables before anything else so all os.environ.get()
# calls below see the correct values.
load_dotenv()


# ============================================================================
# VERSION
# ============================================================================

API_VERSION: str = "1.0.0"


# ============================================================================
# LOGGING CONFIGURATION
# ============================================================================
# Format matches openagent-frontend and openagent-infra exactly so operators
# can read interleaved stdout from all three services as one timeline. The
# OPENAGENT_LOG_LEVEL env var override is supported.

def setup_logging(level_name: str = "INFO") -> logging.Logger:
    """
    Configure application logging.

    Args:
        level_name: Log level as string (DEBUG, INFO, WARNING, ERROR,
                    CRITICAL). Overridden by OPENAGENT_LOG_LEVEL env var
                    if set.

    Returns:
        Configured logger named "openagent-api".
    """
    env_level = os.environ.get("OPENAGENT_LOG_LEVEL", "").upper()
    if env_level in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
        level_name = env_level

    level = getattr(logging, level_name, logging.INFO)

    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    return logging.getLogger("openagent-api")


logger: logging.Logger = setup_logging()


# ============================================================================
# CONFIGURATION
# ============================================================================
# All configuration is environment-driven so the same image runs in dev,
# staging, and production without code changes. Defaults match the
# openagent-frontend, openagent-infra, openagent-logger, and openagent-memory
# datasheet conventions wherever they overlap.

class Config:
    """
    Runtime configuration loaded from environment variables.

    Attributes:
        INFRA_URL:          Base URL of openagent-infra. No trailing slash.
                            Required. Default points at the Docker service
                            name "openagent-infra" on port 8002 per the
                            port convention.

        INFRA_API_KEY:      Outbound secret for the openagent-infra
                            boundary. Sent as X-API-Key on every upstream
                            /chat and /health request. MUST match
                            openagent-infra's API_KEY env var byte-for-byte.
                            Required. Never logged, never echoed.

        OPENAGENT_API_KEY:  Inbound secret for the openagent-frontend
                            boundary. Validated on every /chat and /health
                            request from the frontend. MUST match
                            openagent-frontend's OPENAGENT_API_KEY env var
                            byte-for-byte. Required. Never logged, never
                            echoed.

        FRONTEND_URL:       Optional extra CORS origin for the frontend.
                            Useful for production deployments at custom
                            domains. Empty string means "skip".

        UPSTREAM_CONNECT_TIMEOUT: Seconds to wait for the TCP connection to
                            openagent-infra. Short — if openagent-infra is
                            unreachable, fail fast. Passed into InfraClient
                            at construction.

        UPSTREAM_READ_TIMEOUT: Seconds to wait for the next chunk of data
                            from openagent-infra. None = unbounded, because
                            provider cold-starts can take minutes on first
                            request and high-effort reasoning generations
                            can take several minutes. Passed into
                            InfraClient at construction.

        HEALTH_TIMEOUT:     Short timeout for upstream /health calls. This
                            endpoint is polled every 3 seconds by the
                            frontend's gate-open loop and must never block.
                            Passed into InfraClient at construction and used
                            internally by check_health().

        BIO_PATH:           Path to bio.txt inside the container. Default
                            matches the COPY directive in this repo's
                            Dockerfile.

        LOGGER_URL:         Base URL of openagent-logger. No trailing slash.
                            Required. On a unified Docker network this is
                            typically http://openagent-logger:8003.

        LOGGER_API_KEY:     Outbound secret for the openagent-logger
                            boundary. Sent as X-API-Key on every event POST.
                            MUST match openagent-logger's LOGGER_API_KEY env
                            var byte-for-byte. Required. Never logged, never
                            echoed.

        LOGGER_HMAC_SECRET: HMAC payload-signing secret for the
                            openagent-logger boundary. Used to compute the
                            per-event hmac_signature. MUST match
                            openagent-logger's LOGGER_HMAC_SECRET env var
                            byte-for-byte. Required. Never logged, never
                            echoed.

        LOGGER_QUEUE_MAX_SIZE: Maximum pending events in the LoggerClient's
                            in-memory queue before the drop-oldest overflow
                            policy kicks in. Set from env
                            OPENAGENT_LOGGER_QUEUE_MAX_SIZE. Default 1000,
                            which accommodates a sustained openagent-logger
                            outage of several minutes at typical /chat
                            throughput. Not forwarded to openagent-logger;
                            this is an openagent-api-side operational
                            tunable.

        MEMORY_URL:         Base URL of openagent-memory. No trailing slash.
                            OPTIONAL — memory is opt-in. When MEMORY_URL and
                            MEMORY_API_KEY are both set, memory is enabled
                            (MEMORY_ENABLED). When unset, openagent-api
                            forwards the full history exactly as it did
                            before memory existed. Typically
                            http://openagent-memory:8004 on a shared network.

        MEMORY_API_KEY:     Outbound transport secret for the
                            openagent-memory boundary. Sent as X-API-Key on
                            every /retrieve and /ingest call. MUST match
                            openagent-memory's MEMORY_API_KEY byte-for-byte.
                            OPTIONAL (required only to enable memory). Never
                            logged, never echoed. (No HMAC on this boundary
                            today — openagent-memory uses transport-key auth
                            only; the MemoryClient scaffolds signing for a
                            future addition.)

        MEMORY_SESSION_ID:  Static session id used to scope memory
                            retrieve/ingest AND to populate the logger's
                            session_id field. A stopgap until the frontend
                            manages conversations and supplies a per-session
                            id. When empty, memory retrieve/ingest is inactive
                            even if MEMORY_ENABLED (no session to scope), and
                            session_id is emitted as null.

        MEMORY_RECENT_N:    Number of most-recent conversation messages kept
                            verbatim in the assembled prompt, ahead of the
                            current user turn. The retrieved older turns are
                            deduped against this window. Counted in messages
                            (a user or assistant turn each count as one).
                            Default 10.

        MEMORY_TOP_K:       Optional cap on retrieved turns per /retrieve.
                            When unset (None), the field is omitted from the
                            request and openagent-memory applies its own
                            default (MEMORY_TOP_K_DEFAULT, 5).

        MEMORY_RETRIEVE_TIMEOUT: Read timeout (seconds) on the hot-path
                            /retrieve call. Short by design — a cold embedder
                            behind memory should fail open fast so the /chat
                            hot path is never delayed. Passed into
                            MemoryClient at construction.

        MEMORY_ENABLED:     Derived (not an env var). True iff MEMORY_URL and
                            MEMORY_API_KEY are both set. Gates whether the
                            MemoryClient is constructed at startup.
    """

    INFRA_URL: str = os.environ.get(
        "OPENAGENT_INFRA_URL", "http://openagent-infra:8002"
    ).rstrip("/")

    INFRA_API_KEY: str = os.environ.get("INFRA_API_KEY", "").strip()

    OPENAGENT_API_KEY: str = os.environ.get("OPENAGENT_API_KEY", "").strip()

    FRONTEND_URL: str = os.environ.get(
        "OPENAGENT_FRONTEND_URL", ""
    ).strip()

    UPSTREAM_CONNECT_TIMEOUT: float = float(
        os.environ.get("OPENAGENT_UPSTREAM_CONNECT_TIMEOUT", "10.0")
    )

    UPSTREAM_READ_TIMEOUT: Optional[float] = (
        None
        if os.environ.get("OPENAGENT_UPSTREAM_READ_TIMEOUT", "").lower()
        in ("", "none", "null")
        else float(os.environ["OPENAGENT_UPSTREAM_READ_TIMEOUT"])
    )

    HEALTH_TIMEOUT: float = float(
        os.environ.get("OPENAGENT_HEALTH_TIMEOUT", "5.0")
    )

    BIO_PATH: str = os.environ.get(
        "OPENAGENT_BIO_PATH", "/app/src/prompt/bio.txt"
    )

    # ----- logger boundary -----

    LOGGER_URL: str = os.environ.get(
        "LOGGER_URL", ""
    ).strip().rstrip("/")

    LOGGER_API_KEY: str = os.environ.get("LOGGER_API_KEY", "").strip()

    LOGGER_HMAC_SECRET: str = os.environ.get(
        "LOGGER_HMAC_SECRET", ""
    ).strip()

    LOGGER_QUEUE_MAX_SIZE: int = int(
        os.environ.get("OPENAGENT_LOGGER_QUEUE_MAX_SIZE", "1000")
    )

    # ----- memory boundary (OPTIONAL / opt-in) -----

    MEMORY_URL: str = os.environ.get(
        "MEMORY_URL", ""
    ).strip().rstrip("/")

    MEMORY_API_KEY: str = os.environ.get("MEMORY_API_KEY", "").strip()

    MEMORY_SESSION_ID: str = os.environ.get(
        "MEMORY_SESSION_ID", ""
    ).strip()

    MEMORY_RECENT_N: int = int(os.environ.get("MEMORY_RECENT_N", "10"))

    MEMORY_TOP_K: Optional[int] = (
        None
        if os.environ.get("MEMORY_TOP_K", "").strip() == ""
        else int(os.environ["MEMORY_TOP_K"])
    )

    MEMORY_RETRIEVE_TIMEOUT: float = float(
        os.environ.get("MEMORY_RETRIEVE_TIMEOUT", "5.0")
    )

    # Derived opt-in flag — memory is enabled only when both its URL and key
    # are present. Memory is NOT a refuse-to-boot dependency; when disabled,
    # openagent-api forwards the full history exactly as before.
    MEMORY_ENABLED: bool = bool(MEMORY_URL) and bool(MEMORY_API_KEY)


config = Config()


# ============================================================================
# CORS ORIGINS
# ============================================================================
# openagent-frontend is the only legitimate client. The list below covers
# the deployment topologies from the openagent-frontend datasheet:
#   - Local dev (Streamlit at localhost:8000 / :8501)
#   - Docker on a shared network (service name "openagent-frontend")
#   - Custom production domain (via OPENAGENT_FRONTEND_URL env)
# We deliberately do NOT allow "*" — see RULES above.

_cors_origins: List[str] = [
    "http://localhost:8000",            # Streamlit host port
    "http://localhost:8501",            # Streamlit container port if exposed
    "http://127.0.0.1:8000",
    "http://127.0.0.1:8501",
    "http://openagent-frontend:8501",   # Internal Docker service name
]

if (
    config.FRONTEND_URL
    and config.FRONTEND_URL not in _cors_origins
):
    _cors_origins.append(config.FRONTEND_URL)


# ============================================================================
# IDENTITY LOADER
# ============================================================================
# bio.txt is the canonical OpenAgent persona. It lives in this repo
# (src/prompt/bio.txt) so it can be versioned and rotated independently of
# either the frontend or the model. The file is baked into the Docker image
# at build time via a COPY directive in the Dockerfile and is read once at
# startup.

def load_identity(path: str) -> str:
    """
    Load the system prompt (bio.txt) from disk.

    Args:
        path: Absolute path to bio.txt inside the container.

    Returns:
        Contents of bio.txt with leading and trailing whitespace stripped.
        Falls back to a minimal identity string if the file cannot be read,
        so the service still starts and serves traffic in degraded mode
        rather than refusing to boot.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read().strip()
        if not content:
            logger.warning(
                f"bio.txt at {path} is empty — falling back to default "
                f"identity. Check the COPY directive in the Dockerfile."
            )
            return _default_identity()
        logger.info(
            f"Identity loaded from {path} ({len(content)} chars)."
        )
        return content
    except FileNotFoundError:
        logger.error(
            f"bio.txt not found at {path}. "
            f"Check the Dockerfile COPY directive and the "
            f"OPENAGENT_BIO_PATH env var. Falling back to default identity."
        )
        return _default_identity()
    except Exception as err:
        logger.error(
            f"Failed to load bio.txt from {path}: {err}. "
            f"Falling back to default identity."
        )
        return _default_identity()


def _default_identity() -> str:
    """Minimal fallback identity used when bio.txt is unavailable."""
    return "You are OpenAgent, a helpful AI assistant."


# ============================================================================
# SSE EVENT PARSER (used by sse_pump's side-channel accumulator)
# ============================================================================
# Pure function. Module-level so it's testable in isolation and so the
# accumulator inside sse_pump stays readable. NEVER raises — failures
# return None and the calling code silently moves on. This is deliberate:
# the byte-for-byte relay to the frontend must not be affected by any
# parsing concern. The accumulator's job is best-effort capture; if a chunk
# fails to parse, that single content token is lost from the
# conversation_capture's output_text but the user's chat experience is
# unaffected.
#
# This helper lives in backend/api.py rather than in client/infra.py. The
# accumulator state it feeds lives in sse_pump (per-request), and the
# conversation_capture emission it ultimately serves is request-handler
# logic. The InfraClient is transport-only and deliberately does not inspect
# chunk shape — that keeps it decoupled from any specific upstream chunk
# format.

def _try_parse_sse_event(event_str: str) -> Optional[Dict[str, Any]]:
    """
    Parse a single SSE event line and return the parsed JSON chunk object,
    or None if the event is the [DONE] sentinel, not a data: line, blank,
    or unparseable.

    Args:
        event_str: One SSE event as a string, with leading/trailing
                   whitespace already stripped. Expected forms:
                     "data: {...JSON...}"      → parse and return dict
                     "data: [DONE]"            → return None
                     "" (empty)                → return None
                     anything else             → return None

    Returns:
        Parsed dict on success; None for any non-parseable input.
    """
    if not event_str:
        return None
    if not event_str.startswith("data: "):
        return None
    data_part = event_str[len("data: "):].strip()
    if not data_part:
        return None
    if data_part == "[DONE]":
        return None
    try:
        parsed = json.loads(data_part)
        # Defensive: json.loads can return non-dict types (list, int, str,
        # etc.) for malformed inputs that still happen to be valid JSON. The
        # accumulator only ever uses dict access patterns.
        if not isinstance(parsed, dict):
            return None
        return parsed
    except (json.JSONDecodeError, ValueError):
        return None


# ============================================================================
# PROMPT ASSEMBLY (memory-aware) — used by chat_endpoint
# ============================================================================
# Pure functions, module-level for the same reasons as _try_parse_sse_event:
# testable in isolation, and keeping chat_endpoint readable while honouring
# the single-file-app design. These are only used on the memory-enabled path;
# the memory-disabled path keeps the original "[bio] + full history" build
# inline in chat_endpoint.
#
# The memory-enabled assembly is:
#
#     [system: bio] + [retrieved older turns, deduped] + [recent N verbatim]
#                   + [current user turn]
#
# Memory only RANKS (returns candidate turns + scores); openagent-api BUILDS
# the final query. The retrieved block is deduped against the recent-N window
# and the current turn by SHA-256 content hash — the same key openagent-memory
# uses for its own storage dedupe — so a turn that is both recent and relevant
# (or a near-verbatim repeat of the current turn) is never duplicated.

def _content_hash(text: str) -> str:
    """
    SHA-256 hex of the UTF-8 encoded content. Matches openagent-memory's
    dedupe key so the de-duplication here is consistent with memory's own.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _last_user_index(messages: List[Dict[str, str]]) -> Optional[int]:
    """
    Index of the LAST user-role message in the list, or None if there is no
    user message. The current turn being answered is the last user message;
    everything before it is conversation history.
    """
    idx: Optional[int] = None
    for i, m in enumerate(messages):
        if m.get("role") == "user":
            idx = i
    return idx


def _assemble_messages_with_memory(
    identity_text: str,
    retrieved: List[Dict[str, Any]],
    incoming_messages: List[Dict[str, str]],
    recent_n: int,
) -> List[Dict[str, str]]:
    """
    Build the upstream messages list for the memory-enabled path.

    Layout:
        [system: bio]
        + [retrieved older turns, deduped vs recent-N and current, in
           chronological order]
        + [recent N verbatim turns]
        + [current user turn]

    Args:
        identity_text:     bio.txt content, used as the system message.
        retrieved:         Raw turn dicts from openagent-memory, each shaped
                           {id, role, content, score, created_at}. May be
                           empty (no results, or retrieval degraded).
        incoming_messages: The frontend's full message list AFTER system-
                           message filtering, ending (normally) with the
                           current user turn.
        recent_n:          How many of the most-recent history messages to
                           keep verbatim ahead of the current turn.

    Returns:
        The assembled messages list ready to forward to openagent-infra.
    """
    last_idx = _last_user_index(incoming_messages)
    if last_idx is None:
        # Defensive — chat_endpoint validates that a user message exists
        # before calling this, but never assume. Fall back to full history.
        return [
            {"role": "system", "content": identity_text}
        ] + incoming_messages

    current_message = incoming_messages[last_idx]
    history = incoming_messages[:last_idx]
    recent = history[-recent_n:] if recent_n > 0 else []

    # Dedup retrieved against the recent-N window AND the current turn by
    # content hash, so a turn that is both recent and relevant — or a
    # near-verbatim repeat of the current turn — is not included twice.
    exclude_hashes: Set[str] = {_content_hash(m["content"]) for m in recent}
    exclude_hashes.add(_content_hash(current_message["content"]))

    # Order retrieved chronologically (created_at ascending, unknown last) so
    # the older-context block reads in time order before the recent verbatim
    # turns. ISO-8601 strings sort lexically in time order, so no parsing is
    # needed; turns with a null/missing created_at sort to the end.
    ordered = sorted(
        retrieved,
        key=lambda t: (t.get("created_at") is None, t.get("created_at") or ""),
    )

    retrieved_messages: List[Dict[str, str]] = []
    seen_hashes: Set[str] = set()
    for turn in ordered:
        content = turn.get("content")
        role = turn.get("role")
        if not content or role not in ("user", "assistant"):
            continue
        h = _content_hash(content)
        if h in exclude_hashes or h in seen_hashes:
            continue
        seen_hashes.add(h)
        retrieved_messages.append({"role": role, "content": content})

    return (
        [{"role": "system", "content": identity_text}]
        + retrieved_messages
        + recent
        + [current_message]
    )


# ============================================================================
# GLOBAL RUNTIME STATE
# ============================================================================
# Populated inside the lifespan context manager at startup. Module-level
# globals keep the access pattern simple — no app.state lookups in the hot
# path.
#
# infra_client encapsulates the httpx.AsyncClient used for the
# openagent-infra boundary; the surface visible to this file is
# start()/stop()/stream_chat()/check_health().
#
# memory_client is None unless openagent-memory is configured (opt-in). When
# present it owns its own httpx.AsyncClient plus a set of in-flight ingest
# tasks; the surface visible here is start()/stop()/retrieve()/
# ingest_turn_pair_background().

identity: str = ""
infra_client: Optional[InfraClient] = None
logger_client: Optional[LoggerClient] = None
memory_client: Optional[MemoryClient] = None


# ============================================================================
# LIFESPAN — STARTUP AND SHUTDOWN
# ============================================================================

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """
    FastAPI lifespan handler.

    Startup sequence:
      1. Banner.
      2. Validate OPENAGENT_API_KEY is set (fail fast if missing).
      3. Validate INFRA_API_KEY is set (fail fast if missing).
      4. Validate OPENAGENT_INFRA_URL is set.
      5. Load bio.txt into the global `identity` string.
      6. Instantiate the InfraClient and call start() to open its
         underlying httpx.AsyncClient with the configured timeouts and
         X-API-Key header pre-attached.
      7. Validate the three required openagent-logger env vars (LOGGER_URL,
         LOGGER_API_KEY, LOGGER_HMAC_SECRET).
      8. Instantiate the LoggerClient and launch its background drain task
         via client.start().
      9. If openagent-memory is configured (MEMORY_ENABLED), instantiate the
         MemoryClient and call start(). Memory is OPT-IN and NOT a
         refuse-to-boot dependency — when unconfigured, openagent-api
         forwards full history exactly as before.
     10. Log readiness.

    Shutdown sequence (memory → logger → infra):
      1. Stop the MemoryClient FIRST (if present), draining in-flight ingest
         tasks. Doing this before the LoggerClient means any
         memory_ingest_error ops_events emitted during the drain can still
         be enqueued onto a live logger queue.
      2. Stop the LoggerClient, allowing pending events to drain (with
         timeout) before the upstream connection pool is torn down.
      3. Stop the InfraClient, which closes its underlying
         httpx.AsyncClient.
      4. Log final state.

    The lifespan context manager (FastAPI 0.93+) replaces the deprecated
    @app.on_event("startup") / @app.on_event("shutdown") pair.
    """
    global identity, infra_client, logger_client, memory_client

    # ------------------------------------------------------------------
    # 1. Banner
    # ------------------------------------------------------------------
    logger.info("=" * 60)
    logger.info(
        f"openagent-api v{API_VERSION} starting — identity gateway"
    )
    logger.info("=" * 60)

    # ------------------------------------------------------------------
    # 2. Validate OPENAGENT_API_KEY (inbound auth)
    # ------------------------------------------------------------------
    # Without this, anyone who can reach the container can call /chat.
    # Refuse to boot rather than start in an insecure state.
    if not config.OPENAGENT_API_KEY:
        logger.error(
            "CRITICAL: OPENAGENT_API_KEY is not set. "
            "Add it to .env and rebuild. openagent-api will not start "
            "without an inbound auth secret."
        )
        raise RuntimeError(
            "OPENAGENT_API_KEY is required for inbound authentication."
        )

    # ------------------------------------------------------------------
    # 3. Validate INFRA_API_KEY (outbound auth to openagent-infra)
    # ------------------------------------------------------------------
    # Without this, every upstream /chat call would receive a 401.
    if not config.INFRA_API_KEY:
        logger.error(
            "CRITICAL: INFRA_API_KEY is not set. "
            "Add it to .env and rebuild. openagent-api cannot reach "
            "openagent-infra without a valid upstream key."
        )
        raise RuntimeError(
            "INFRA_API_KEY is required for upstream authentication."
        )

    # ------------------------------------------------------------------
    # 4. Validate OPENAGENT_INFRA_URL
    # ------------------------------------------------------------------
    if not config.INFRA_URL:
        logger.error("CRITICAL: OPENAGENT_INFRA_URL is not set.")
        raise RuntimeError("OPENAGENT_INFRA_URL is required for operation.")
    logger.info(f"Upstream openagent-infra: {config.INFRA_URL}")

    # ------------------------------------------------------------------
    # 5. Load identity from bio.txt
    # ------------------------------------------------------------------
    identity = load_identity(config.BIO_PATH)

    # ------------------------------------------------------------------
    # 6. Instantiate and start the InfraClient
    # ------------------------------------------------------------------
    # The client owns:
    #   - the httpx.AsyncClient (connect short, read unbounded by default,
    #     write 10s, pool 5s)
    #   - the X-API-Key header (pre-attached at start() time)
    #   - the /chat streaming POST wrapper (stream_chat)
    #   - the /health proxy with status-vocabulary translation
    #     (check_health)
    # See src/client/infra.py for the full contract. The client logs its own
    # "InfraClient started (url=..., connect_timeout=..., read_timeout=...)"
    # line from start(), matching the LoggerClient style.
    infra_client = InfraClient(
        url=config.INFRA_URL,
        api_key=config.INFRA_API_KEY,
        connect_timeout=config.UPSTREAM_CONNECT_TIMEOUT,
        read_timeout=config.UPSTREAM_READ_TIMEOUT,
        health_timeout=config.HEALTH_TIMEOUT,
    )
    await infra_client.start()

    # ------------------------------------------------------------------
    # 7. Validate the openagent-logger env vars
    # ------------------------------------------------------------------
    # All three are required at startup — openagent-api refuses to boot if
    # any is missing, matching the INFRA_API_KEY discipline.
    if not config.LOGGER_URL:
        logger.error(
            "CRITICAL: LOGGER_URL is not set. "
            "Add it to .env and rebuild. openagent-api will not start "
            "without an openagent-logger URL."
        )
        raise RuntimeError(
            "LOGGER_URL is required for the openagent-logger boundary."
        )
    if not config.LOGGER_API_KEY:
        logger.error(
            "CRITICAL: LOGGER_API_KEY is not set. "
            "Add it to .env and rebuild. The value must match "
            "openagent-logger's LOGGER_API_KEY byte-for-byte."
        )
        raise RuntimeError(
            "LOGGER_API_KEY is required for the openagent-logger boundary."
        )
    if not config.LOGGER_HMAC_SECRET:
        logger.error(
            "CRITICAL: LOGGER_HMAC_SECRET is not set. "
            "Add it to .env and rebuild. The value must match "
            "openagent-logger's LOGGER_HMAC_SECRET byte-for-byte."
        )
        raise RuntimeError(
            "LOGGER_HMAC_SECRET is required for the openagent-logger boundary."
        )
    logger.info(f"Outbound openagent-logger: {config.LOGGER_URL}")

    # ------------------------------------------------------------------
    # 8. Instantiate and start the LoggerClient
    # ------------------------------------------------------------------
    # Fire-and-forget event emission to openagent-logger. The client owns
    # its own httpx.AsyncClient (separate from the openagent-infra one), an
    # in-memory asyncio.Queue, and a background drain task. See
    # src/client/logger.py for the full contract.
    #
    # We do NOT probe openagent-logger for connectivity at startup. If
    # openagent-logger is unreachable, the first few drain attempts will
    # fail and log warnings, and the events will be lost. This matches the
    # fire-and-forget contract — openagent-api's responsibility is to serve
    # /chat; openagent-logger's availability is non-essential.
    logger_client = LoggerClient(
        url=config.LOGGER_URL,
        api_key=config.LOGGER_API_KEY,
        hmac_secret=config.LOGGER_HMAC_SECRET,
        source_service="openagent-api",
        queue_max_size=config.LOGGER_QUEUE_MAX_SIZE,
    )
    await logger_client.start()
    logger.info(
        f"LoggerClient ready "
        f"(queue_max_size={config.LOGGER_QUEUE_MAX_SIZE})"
    )

    # ------------------------------------------------------------------
    # 9. Instantiate and start the MemoryClient (OPTIONAL / opt-in)
    # ------------------------------------------------------------------
    # Memory is enabled only when MEMORY_URL and MEMORY_API_KEY are both
    # present. It is NOT a refuse-to-boot dependency: when unconfigured,
    # openagent-api forwards the full history exactly as it did before memory
    # existed. We do NOT probe openagent-memory at startup — retrieves fail
    # open and ingests surface their own failures when they run.
    if config.MEMORY_ENABLED:
        memory_client = MemoryClient(
            url=config.MEMORY_URL,
            api_key=config.MEMORY_API_KEY,
            retrieve_timeout=config.MEMORY_RETRIEVE_TIMEOUT,
        )
        await memory_client.start()
        if not config.MEMORY_SESSION_ID:
            logger.warning(
                "MemoryClient enabled but MEMORY_SESSION_ID is unset; "
                "retrieval and ingest will be INACTIVE until a session_id is "
                "available (env var today; frontend-managed later). Full "
                "history is forwarded in the meantime."
            )
        logger.info(
            f"MemoryClient ready "
            f"(url={config.MEMORY_URL}, "
            f"recent_n={config.MEMORY_RECENT_N}, "
            f"top_k="
            f"{config.MEMORY_TOP_K if config.MEMORY_TOP_K is not None else 'memory-default'}, "
            f"session_id={'set' if config.MEMORY_SESSION_ID else 'unset'})"
        )
    else:
        logger.info(
            "openagent-memory not configured (MEMORY_URL / MEMORY_API_KEY "
            "unset); retrieval and ingest disabled, forwarding full history."
        )

    # ------------------------------------------------------------------
    # 10. Ready
    # ------------------------------------------------------------------
    logger.info("=" * 60)
    logger.info(f"openagent-api v{API_VERSION} is ready to accept requests.")
    logger.info(f"CORS origins: {_cors_origins}")
    logger.info("=" * 60)

    # --- Server runs here ---
    yield

    # ------------------------------------------------------------------
    # SHUTDOWN (memory → logger → infra)
    # ------------------------------------------------------------------
    logger.info("openagent-api shutting down...")

    # Stop the MemoryClient FIRST so in-flight ingest tasks drain before the
    # rest of the service tears down. Draining before the LoggerClient stops
    # means any memory_ingest_error ops_events emitted during the drain can
    # still be enqueued onto a live logger queue.
    if memory_client is not None:
        try:
            await memory_client.stop()
        except Exception as err:
            logger.warning(
                f"Error stopping MemoryClient: "
                f"{type(err).__name__}: {err}"
            )

    # Stop the LoggerClient next so any pending events drain before the
    # upstream connection pool is torn down. The client's stop() waits up to
    # its drain_timeout (default 5s) for the queue to empty, then cancels its
    # background task and closes its internal httpx client.
    if logger_client is not None:
        try:
            await logger_client.stop()
        except Exception as err:
            logger.warning(
                f"Error stopping LoggerClient: "
                f"{type(err).__name__}: {err}"
            )

    # Stop the InfraClient last, which closes its internal httpx.AsyncClient.
    # The client logs its own "InfraClient closed." line on successful close.
    if infra_client is not None:
        try:
            await infra_client.stop()
        except Exception as err:
            logger.warning(
                f"Error stopping InfraClient: "
                f"{type(err).__name__}: {err}"
            )

    logger.info("openagent-api shutdown complete.")


# ============================================================================
# FASTAPI APPLICATION
# ============================================================================

app = FastAPI(
    title="openagent-api",
    description=(
        "The identity gateway for OpenAgent. SSE relay between "
        "openagent-frontend and openagent-infra (via InfraClient), with "
        "fire-and-forget event emission to openagent-logger (via "
        "LoggerClient) and optional session-scoped retrieval/ingest via "
        "openagent-memory (via MemoryClient). Owns the OpenAgent system "
        "prompt and the outbound secrets (INFRA_API_KEY, LOGGER_API_KEY, "
        "LOGGER_HMAC_SECRET, and — when memory is enabled — MEMORY_API_KEY); "
        "remains stateless across requests."
    ),
    version=API_VERSION,
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


# ============================================================================
# AUTH DEPENDENCY
# ============================================================================
# FastAPI dependency that validates the inbound X-API-Key header against
# OPENAGENT_API_KEY. Used on every /chat and /health request.
#
# Returns 401 with the same detail string openagent-infra uses ("Invalid or
# missing API key") so the frontend's emoji classifier surfaces 🔐
# regardless of which service rejected the request.

async def require_api_key(
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
) -> None:
    """
    Validate the inbound X-API-Key header against OPENAGENT_API_KEY.

    Args:
        x_api_key: Value of the X-API-Key header on the inbound request.

    Raises:
        HTTPException 401: header missing or value does not match.
    """
    if not x_api_key or x_api_key != config.OPENAGENT_API_KEY:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
        )


# ============================================================================
# REQUEST AND RESPONSE MODELS
# ============================================================================

class Message(BaseModel):
    """
    A single OpenAI-format message.

    role:    One of "user", "assistant", or "system". The "system" role is
             reserved for openagent-api's own bio.txt injection and SHOULD
             NOT be sent by the frontend. If a frontend sends one anyway it
             is dropped during validation rather than passed through — this
             prevents prompt-override attacks from a tampered or
             out-of-date client.

    content: The message text. Must be non-empty per the upstream contract.
    """
    role: str = Field(..., pattern="^(user|assistant|system)$")
    content: str


class ChatRequest(BaseModel):
    """
    Incoming request from openagent-frontend.

    The frontend sends only the user/assistant turns it has accumulated in
    st.session_state. openagent-api prepends the system message from bio.txt
    before forwarding upstream — the frontend never sees or handles bio.txt
    content.

    Fields:
        messages: List of {role, content} dicts. Must contain at least one
                  "user" message after server-side filtering. Any "system"
                  messages are dropped before forwarding upstream.

        reasoning_effort: Optional. One of "low", "medium", or "high".
                  Controls the upstream model's reasoning depth. When the
                  frontend sends a value, openagent-api forwards it to
                  openagent-infra. When omitted, openagent-api does NOT
                  include the field in the upstream payload — openagent-infra
                  applies its own server-side default. This is pure
                  pass-through; openagent-api holds no default of its own to
                  avoid two sources of truth for the same setting. Pydantic
                  validates the value if present; an invalid value produces
                  HTTP 422 automatically before any upstream call is made.

    Note: there is intentionally no session_id field yet. session_id comes
    from the MEMORY_SESSION_ID env var today; it will move to a request
    field (or header) once the frontend manages conversations.
    """
    messages: List[Message]
    reasoning_effort: Optional[str] = Field(
        default=None,
        pattern="^(low|medium|high)$",
    )


# ============================================================================
# CHAT ENDPOINT — SSE RELAY + LOGGER EMISSION (+ OPTIONAL MEMORY)
# ============================================================================

@app.post("/chat", dependencies=[Depends(require_api_key)])
async def chat_endpoint(
    request: ChatRequest,
    http_request: Request,
) -> StreamingResponse:
    """
    Main entry point for openagent-frontend.

    Flow:
      0. (Pre-handler) FastAPI's dependency runs require_api_key. If auth
         fails the dependency raises 401 and this function is never entered.
      1. Generate request_id (uuid4 with dashes — 36 chars, the
         str(uuid.uuid4()) form). This is the correlation ID joining the
         ops_events and conversation_capture for THIS /chat call. Captured
         here so every emission uses the same value.
      2. session_id is read from MEMORY_SESSION_ID (None when unset). It is
         threaded onto every emitted event and, when present alongside a
         configured MemoryClient (memory_active), scopes memory
         retrieve/ingest.
      3. Capture chat_start_time via time.monotonic() for latency.
      4. Emit request_received ops_event (after auth passes, before any
         other work — including before validation).
      5. Validate the messages list (non-empty, contains a user message).
         On validation failure, raise 400/422 — no further events are
         emitted.
      6. Drop any "system" messages the frontend tried to send.
      7. Extract the current/last user message (the retrieve query and the
         conversation_capture / memory-ingest input_text).
      8. If memory is active, await retrieve() (bounded, fail-open) for this
         session BEFORE assembling the prompt; emit memory_retrieve_degraded
         on a degraded result.
      9. Assemble the upstream messages list — the memory path
         ([bio] + retrieved + recent N + current) when memory is active, or
         the original [bio] + full history otherwise.
     10. Build the upstream payload (+ reasoning_effort pass-through).
     11. Return StreamingResponse wrapping sse_pump(). The pump:
         a. Emits upstream_call ops_event.
         b. Opens the streaming POST via
            infra_client.stream_chat(upstream_payload).
         c. On non-200 upstream: emits upstream_error, yields error event,
            returns (no stream_complete, no capture, no ingest).
         d. On happy path: yields each chunk byte-for-byte AND accumulates
            delta.content tokens via side-channel parsing.
         e. On clean stream end: emits stream_complete (success) and
            conversation_capture, and — when memory is active — fires the
            background user+assistant ingest.
         f. On client disconnect mid-stream: emits stream_complete
            (client_disconnect); no capture, no ingest.
         g. On any exception: emits upstream_error, yields error event.

    Args:
        request:      Pydantic-parsed ChatRequest from the frontend.
                      Includes messages and optional reasoning_effort.
        http_request: Underlying Starlette request, used for mid-stream
                      disconnect detection.

    Returns:
        StreamingResponse with content_type="text/event-stream".

    Raises:
        HTTPException 400: messages list empty or no user message after
                           system-message filtering.
        HTTPException 401: OPENAGENT_API_KEY missing or wrong (handled by
                           the require_api_key dependency).
        HTTPException 422: invalid reasoning_effort value (handled by
                           Pydantic before this function runs).
    """
    # ------------------------------------------------------------------
    # 1-3. CORRELATION IDS AND TIMING
    # ------------------------------------------------------------------
    # request_id is the canonical correlation key for this /chat call,
    # carried on every event emitted to openagent-logger. session_id comes
    # from MEMORY_SESSION_ID (None when unset); it threads onto every event
    # and scopes memory retrieve/ingest when memory is active. chat_start_time
    # anchors the latency measurement that lands in stream_complete and
    # conversation_capture.
    request_id: str = str(uuid.uuid4())
    session_id: Optional[str] = config.MEMORY_SESSION_ID or None
    chat_start_time: float = time.monotonic()

    # Memory is "active" for this request only when the client was
    # constructed (memory configured) AND we have a session_id to scope it.
    # Without a session_id we cannot retrieve/ingest, so we behave exactly as
    # if memory were disabled (forward full history) rather than truncating
    # to recent-N with no retrieval to compensate.
    memory_active: bool = memory_client is not None and session_id is not None

    # ------------------------------------------------------------------
    # 4. EMIT request_received
    # ------------------------------------------------------------------
    # Fired after auth has passed (dependency ran before this function was
    # entered) and before any validation. The HTTP response code of the
    # eventual reply carries the validation outcome; this event captures
    # only "a request arrived".
    logger_client.emit_ops_event(
        action="request_received",
        outcome="success",
        request_id=request_id,
        session_id=session_id,
        details={
            "messages_count": len(request.messages),
            "reasoning_effort": (
                request.reasoning_effort
                if request.reasoning_effort is not None
                else "unset"
            ),
        },
    )

    # ------------------------------------------------------------------
    # 5. VALIDATE INCOMING MESSAGES
    # ------------------------------------------------------------------
    # Mirrors openagent-infra's 400 contract so the frontend's emoji
    # classifier surfaces ⚠️ regardless of which service rejected.
    if not request.messages:
        raise HTTPException(
            status_code=400,
            detail="Messages list cannot be empty.",
        )

    # ------------------------------------------------------------------
    # 6. DROP ANY FRONTEND-SUPPLIED SYSTEM MESSAGES
    # ------------------------------------------------------------------
    # bio.txt is the canonical OpenAgent persona, owned by openagent-api
    # alone. If a frontend (or a tampered client) attempts to send its own
    # system message, drop it silently rather than pass it through. The
    # frontend does not send system messages, so any system message
    # arriving here is by definition unexpected — log it for visibility.
    incoming_messages: List[Dict[str, str]] = []
    dropped_system_count = 0
    for m in request.messages:
        if m.role == "system":
            dropped_system_count += 1
            continue
        incoming_messages.append({"role": m.role, "content": m.content})

    if dropped_system_count > 0:
        logger.warning(
            f"Dropped {dropped_system_count} frontend-supplied system "
            f"message(s). bio.txt is the only authoritative system prompt."
        )

    # Re-validate after filtering: at least one user message must remain.
    has_user_message = any(m["role"] == "user" for m in incoming_messages)
    if not has_user_message:
        raise HTTPException(
            status_code=400,
            detail="Messages must include at least one user message.",
        )

    # ------------------------------------------------------------------
    # 7. EXTRACT THE CURRENT (LAST) USER MESSAGE
    # ------------------------------------------------------------------
    # This is the user's most recent input. It is:
    #   - the retrieve query (memory searches PRIOR turns against it),
    #   - the conversation_capture input_text, and
    #   - the user-turn content for memory ingest.
    # Snapshotted here (before sse_pump runs) so it's in scope for the
    # eventual emission and ingest inside the pump.
    last_user_text: str = next(
        (
            m["content"]
            for m in reversed(incoming_messages)
            if m["role"] == "user"
        ),
        "",
    )

    # ------------------------------------------------------------------
    # 8. MEMORY RETRIEVE (hot path, bounded, fail-open) — memory path only
    # ------------------------------------------------------------------
    # Awaited before prompt assembly so the retrieved older turns can be
    # spliced into the upstream messages. retrieve() never raises: any
    # timeout / transport error / non-200 / degraded:true returns
    # ([], degraded=True). On a degraded result we proceed with recent turns
    # only and emit an ops_event so operators can correlate answer-quality
    # dips with embedder cold-starts.
    retrieved_turns: List[Dict[str, Any]] = []
    if memory_active and memory_client is not None:
        retrieved_turns, mem_degraded = await memory_client.retrieve(
            session_id=session_id,
            query=last_user_text,
            top_k=config.MEMORY_TOP_K,
        )
        if mem_degraded:
            retrieved_turns = []
            logger_client.emit_ops_event(
                action="memory_retrieve_degraded",
                outcome="degraded",
                request_id=request_id,
                session_id=session_id,
                details={"reason": "memory_unavailable_or_embedder_cold"},
            )

    # ------------------------------------------------------------------
    # 9. ASSEMBLE UPSTREAM MESSAGES
    # ------------------------------------------------------------------
    # Memory path: [bio] + [retrieved older, deduped] + [recent N] + [current].
    # Disabled path: [bio] + full history (the original behaviour). We only
    # truncate to recent-N when memory is active, because truncating without
    # retrieval to compensate would drop context for nothing.
    if memory_active:
        upstream_messages: List[Dict[str, str]] = _assemble_messages_with_memory(
            identity_text=identity,
            retrieved=retrieved_turns,
            incoming_messages=incoming_messages,
            recent_n=config.MEMORY_RECENT_N,
        )
    else:
        upstream_messages = [
            {"role": "system", "content": identity}
        ] + incoming_messages

    # ------------------------------------------------------------------
    # 10. BUILD UPSTREAM PAYLOAD — PURE PASS-THROUGH OF reasoning_effort
    # ------------------------------------------------------------------
    # If the frontend sent reasoning_effort, include it in the upstream
    # payload. If not, omit the field entirely so openagent-infra applies
    # its own server-side default. openagent-api holds no default of its
    # own — that decision belongs at the openagent-infra layer where
    # REASONING_EFFORT lives as an env var.
    upstream_payload: Dict[str, Any] = {"messages": upstream_messages}
    if request.reasoning_effort is not None:
        upstream_payload["reasoning_effort"] = request.reasoning_effort

    # Brief structured log line — full message bodies are not logged because
    # they can contain user data. Logging is intentionally minimal here;
    # openagent-logger now owns structured event capture. We DO log
    # reasoning_effort because it's a small enum value with no privacy
    # implications and useful for debugging cold-start vs warm-path latency
    # differences. We include request_id so operator logs and
    # openagent-logger rows can be cross-referenced.
    user_msg_count = sum(
        1 for m in incoming_messages if m["role"] == "user"
    )
    last_user_preview = last_user_text[:60]
    effort_label = (
        request.reasoning_effort
        if request.reasoning_effort is not None
        else "unset"
    )
    memory_log = ""
    if memory_active:
        memory_log = (
            f"retrieved={len(retrieved_turns)} "
            f"assembled_turns={len(upstream_messages)} "
        )
    logger.info(
        f"POST /chat | req={request_id} "
        f"turns={len(incoming_messages)} "
        f"user_msgs={user_msg_count} "
        f"reasoning_effort={effort_label} "
        f"session={'set' if session_id else 'none'} "
        f"{memory_log}"
        f"| last_user: {last_user_preview}"
        f"{'...' if len(last_user_preview) >= 60 else ''}"
    )

    # ------------------------------------------------------------------
    # 11. GUARD: ensure infra_client was initialised
    # ------------------------------------------------------------------
    # If lifespan startup did not run (which would only happen if something
    # is very wrong), fail clearly rather than NoneType-error.
    if infra_client is None:
        raise HTTPException(
            status_code=500,
            detail="openagent-api is not initialised (infra_client missing).",
        )

    # ------------------------------------------------------------------
    # 12. OPEN UPSTREAM STREAM AND PUMP BYTES (+ accumulate for capture)
    # ------------------------------------------------------------------
    async def sse_pump() -> AsyncIterator[bytes]:
        """
        Async generator that pipes openagent-infra's SSE stream to the
        frontend byte-for-byte AND maintains a side-channel content
        accumulator for the eventual conversation_capture (and, when memory
        is active, the assistant-turn ingest).

        Relay behaviour: yields raw bytes — including the data: prefix and
        the \\n\\n separators — exactly as openagent-infra emits them. Each
        event is a JSON-encoded OpenAI ChatCompletion chunk; reasoning
        tokens arrive in choices[0].delta.reasoning, visible answer tokens
        arrive in choices[0].delta.content, and the stream terminates with
        an empty-delta chunk (finish_reason="stop") followed by data: [DONE].
        The frontend's parser handles the decode.

        Side-channel: as each chunk flies past, it is appended to
        event_buffer; the buffer is split on b"\\n\\n" to find complete SSE
        events; each event is parsed via _try_parse_sse_event();
        delta.content tokens are accumulated into content_buffer;
        model_used is captured from the first chunk that carries a model
        field; input_tokens / output_tokens are captured from any usage
        field that appears (emitted only when openagent-infra sets
        stream_options.include_usage upstream, which it does not today —
        those fields will be None). The yield to the frontend happens BEFORE
        the parse in every iteration, so the parse adds zero latency to the
        user-visible stream. Parse failures are silently tolerated; the
        worst case is that output_text in the conversation_capture is
        shorter than the actual response.

        The outbound HTTP call goes through InfraClient: the
        `async with infra_client.stream_chat(upstream_payload) as upstream:`
        block yields an httpx.Response — same status_code, same aread(),
        same aiter_raw() — and the surrounding exception handlers catch the
        httpx exception types directly.

        Emission points (per openagent-logger DATASHEET §6.1):
          - upstream_call    : just before opening the stream
          - upstream_error   : in every exception branch
          - stream_complete  : after the stream finishes (success or
                               client_disconnect)
          - conversation_capture : after a successful stream_complete only

        Memory (when active): after a successful stream_complete and the
        conversation_capture, ingest_turn_pair_background() is fired to store
        the user turn then the assistant turn off the user's path. A
        client-disconnect or upstream-error turn ingests neither side.

        If the client disconnects mid-stream we exit the loop early so the
        upstream connection can be closed and openagent-infra stops wasting
        compute. We still emit stream_complete with
        outcome=client_disconnect so the operational signal lands.
        """
        # Side-channel accumulator state.
        content_buffer: List[str] = []
        event_buffer: bytes = b""
        model_used: Optional[str] = None
        input_tokens: Optional[int] = None
        output_tokens: Optional[int] = None
        bytes_relayed: int = 0

        # ------------------------------------------------------------------
        # EMIT upstream_call
        # ------------------------------------------------------------------
        # Fired immediately before opening the httpx stream. The timestamp
        # on this event is the moment openagent-api hands control to
        # InfraClient (which in turn hands control to httpx).
        logger_client.emit_ops_event(
            action="upstream_call",
            outcome="initiated",
            request_id=request_id,
            session_id=session_id,
            details={
                "url": config.INFRA_URL,
                "reasoning_effort": effort_label,
            },
        )

        try:
            # Streaming POST goes through InfraClient. The returned context
            # manager is httpx's own.
            async with infra_client.stream_chat(
                upstream_payload,
            ) as upstream:
                # ----------------------------------------------------------
                # Non-200 upstream responses — openagent-infra emits these
                # as JSON, not SSE. We've already committed to a 200 by the
                # time aiter_raw runs (StreamingResponse headers go out
                # before the generator yields), so we surface the upstream
                # status as an in-band SSE error event followed by [DONE].
                # The frontend's parser handles unknown event payloads
                # gracefully; an [ERROR ...] line shows up in the chat as a
                # visible failure rather than a silent stop.
                # ----------------------------------------------------------
                if upstream.status_code != 200:
                    body_text = ""
                    try:
                        body_bytes = await upstream.aread()
                        body_text = body_bytes.decode(
                            "utf-8", errors="replace"
                        )
                    except Exception:
                        pass
                    logger.warning(
                        f"Upstream returned {upstream.status_code}: "
                        f"{body_text[:200]}"
                    )

                    # Emit upstream_error BEFORE yielding the error event so
                    # the operational signal lands even if the error event
                    # itself never reaches a paying-attention client.
                    logger_client.emit_ops_event(
                        action="upstream_error",
                        outcome="failure",
                        request_id=request_id,
                        session_id=session_id,
                        details={
                            "error_type": "upstream_non_200",
                            "status_code": upstream.status_code,
                        },
                    )

                    error_event = (
                        f"data: [ERROR upstream_status="
                        f"{upstream.status_code}]\n\n"
                        "data: [DONE]\n\n"
                    ).encode("utf-8")
                    yield error_event
                    # No stream_complete, no conversation_capture, no memory
                    # ingest — the stream never actually happened.
                    return

                # ----------------------------------------------------------
                # Happy path: relay raw bytes until upstream closes, while
                # parsing each event in the side channel.
                # ----------------------------------------------------------
                async for chunk in upstream.aiter_raw():
                    if await http_request.is_disconnected():
                        logger.info(
                            f"Client disconnected mid-stream after "
                            f"{bytes_relayed} bytes — closing upstream."
                        )
                        # Emit stream_complete with outcome=client_disconnect
                        # so we have visibility into how often this happens.
                        # No conversation_capture and no memory ingest — the
                        # conversation is incomplete from the user's
                        # perspective and isn't useful to capture or store.
                        disconnect_latency_ms = int(
                            (time.monotonic() - chat_start_time) * 1000
                        )
                        logger_client.emit_ops_event(
                            action="stream_complete",
                            outcome="client_disconnect",
                            request_id=request_id,
                            session_id=session_id,
                            details={
                                "bytes_relayed": bytes_relayed,
                                "latency_ms": disconnect_latency_ms,
                            },
                        )
                        return

                    bytes_relayed += len(chunk)
                    # Yield FIRST — frontend latency is the priority.
                    yield chunk

                    # Then side-channel parse for the accumulator. Parse
                    # failures are tolerated silently; the byte relay above
                    # has already happened, so the user's chat is unaffected
                    # by anything below.
                    event_buffer += chunk
                    while b"\n\n" in event_buffer:
                        event_bytes, event_buffer = event_buffer.split(
                            b"\n\n", 1
                        )
                        event_str = event_bytes.decode(
                            "utf-8", errors="replace"
                        ).strip()
                        parsed = _try_parse_sse_event(event_str)
                        if parsed is None:
                            continue
                        # Capture model from the first chunk that has one.
                        if model_used is None:
                            model_used = parsed.get("model")
                        # Accumulate delta.content tokens.
                        choices = parsed.get("choices") or []
                        if choices:
                            delta = choices[0].get("delta") or {}
                            content_tok = delta.get("content")
                            if content_tok:
                                content_buffer.append(content_tok)
                        # Capture usage if present (rare — emitted only with
                        # stream_options.include_usage).
                        usage = parsed.get("usage")
                        if usage:
                            if input_tokens is None:
                                input_tokens = usage.get("prompt_tokens")
                            if output_tokens is None:
                                output_tokens = usage.get("completion_tokens")

                # ----------------------------------------------------------
                # Stream finished cleanly. Emit stream_complete and the
                # conversation_capture, then (when memory is active) fire the
                # background user+assistant ingest.
                # ----------------------------------------------------------
                latency_ms = int(
                    (time.monotonic() - chat_start_time) * 1000
                )
                output_text = "".join(content_buffer)

                logger.info(
                    f"Stream complete | req={request_id} "
                    f"{bytes_relayed} bytes relayed, "
                    f"{len(output_text)} chars captured, "
                    f"latency={latency_ms}ms"
                )

                logger_client.emit_ops_event(
                    action="stream_complete",
                    outcome="success",
                    request_id=request_id,
                    session_id=session_id,
                    details={
                        "bytes_relayed": bytes_relayed,
                        "latency_ms": latency_ms,
                    },
                )

                logger_client.emit_conversation_capture(
                    request_id=request_id,
                    input_text=last_user_text,
                    output_text=output_text,
                    session_id=session_id,
                    model_used=model_used,
                    reasoning_effort=request.reasoning_effort,
                    latency_ms=latency_ms,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                )

                # ------------------------------------------------------
                # MEMORY INGEST (off the user's path) — success branch only.
                # ------------------------------------------------------
                # Fire-and-forget a tracked background task that ingests the
                # user turn then the assistant turn (sequential, so created_at
                # reflects turn order). This is synchronous and non-blocking —
                # it schedules the task and returns immediately, so the
                # StreamingResponse generator finishes promptly while the
                # ingests complete on their own (a cold embedder cannot hold
                # the connection open after the user already has their
                # answer). Failures are caught inside the task: logged, and
                # surfaced to the logger as memory_ingest_error via the
                # on_event callback below. They never affect /chat.
                if memory_active and memory_client is not None:
                    memory_client.ingest_turn_pair_background(
                        session_id=session_id,
                        user_text=last_user_text,
                        assistant_text=output_text,
                        on_event=lambda action, outcome, details: (
                            logger_client.emit_ops_event(
                                action=action,
                                outcome=outcome,
                                request_id=request_id,
                                session_id=session_id,
                                details=details,
                            )
                        ),
                    )

        except httpx.ConnectTimeout:
            logger.error(
                f"Upstream connect timeout to {config.INFRA_URL}"
            )
            logger_client.emit_ops_event(
                action="upstream_error",
                outcome="failure",
                request_id=request_id,
                session_id=session_id,
                details={"error_type": "ConnectTimeout"},
            )
            yield (
                "data: [ERROR upstream=ConnectTimeout]\n\n"
                "data: [DONE]\n\n"
            ).encode("utf-8")
        except httpx.ConnectError:
            logger.error(
                f"Cannot reach openagent-infra at {config.INFRA_URL}"
            )
            logger_client.emit_ops_event(
                action="upstream_error",
                outcome="failure",
                request_id=request_id,
                session_id=session_id,
                details={"error_type": "ConnectError"},
            )
            yield (
                "data: [ERROR upstream=ConnectError]\n\n"
                "data: [DONE]\n\n"
            ).encode("utf-8")
        except httpx.ReadTimeout:
            logger.error(
                "Upstream read timeout during generation"
            )
            logger_client.emit_ops_event(
                action="upstream_error",
                outcome="failure",
                request_id=request_id,
                session_id=session_id,
                details={"error_type": "ReadTimeout"},
            )
            yield (
                "data: [ERROR upstream=ReadTimeout]\n\n"
                "data: [DONE]\n\n"
            ).encode("utf-8")
        except httpx.RemoteProtocolError:
            logger.error(
                "openagent-infra closed the connection unexpectedly"
            )
            logger_client.emit_ops_event(
                action="upstream_error",
                outcome="failure",
                request_id=request_id,
                session_id=session_id,
                details={"error_type": "RemoteProtocolError"},
            )
            yield (
                "data: [ERROR upstream=RemoteProtocolError]\n\n"
                "data: [DONE]\n\n"
            ).encode("utf-8")
        except httpx.HTTPError as err:
            logger.error(
                f"Upstream HTTP error during stream: {err}"
            )
            logger_client.emit_ops_event(
                action="upstream_error",
                outcome="failure",
                request_id=request_id,
                session_id=session_id,
                details={"error_type": type(err).__name__},
            )
            yield (
                f"data: [ERROR upstream={type(err).__name__}]\n\n"
                "data: [DONE]\n\n"
            ).encode("utf-8")
        except Exception as err:
            logger.error(
                f"Unexpected error during stream: {err}"
            )
            logger_client.emit_ops_event(
                action="upstream_error",
                outcome="failure",
                request_id=request_id,
                session_id=session_id,
                details={"error_type": type(err).__name__},
            )
            yield (
                f"data: [ERROR {type(err).__name__}]\n\n"
                "data: [DONE]\n\n"
            ).encode("utf-8")

    # ------------------------------------------------------------------
    # 13. RETURN STREAMINGRESPONSE
    # ------------------------------------------------------------------
    # Headers chosen so any reverse proxy in front of openagent-api (nginx,
    # traefik, Cloudflare) does not buffer the stream:
    #   Cache-Control: no-cache       — don't cache SSE
    #   X-Accel-Buffering: no         — disable nginx buffering
    #   Connection: keep-alive        — hold the socket open
    return StreamingResponse(
        sse_pump(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# ============================================================================
# HEALTH ENDPOINT
# ============================================================================

@app.get("/health", dependencies=[Depends(require_api_key)])
async def health_check() -> JSONResponse:
    """
    Proxied health check.

    The openagent-frontend datasheet specifies a 3-second polling loop
    against /health that gates the chat input until the model is ready. The
    frontend polls openagent-api here, and openagent-api forwards the
    question to openagent-infra.

    Response shape (always HTTP 200, status is in the body — the frontend's
    gate-open loop reads the top-level "status" field only):

        {
          "status": "ok" | "loading" | "unreachable",
          "openagent_api": {
            "version": "1.0.0",
            "identity_loaded": true
          },
          "openagent_infra": {
            "url": "http://openagent-infra:8002",
            "status": "ok" | "loading" | "unreachable",
            "raw": <upstream JSON or null>
          }
        }

    Status mapping (openagent-infra → openagent-api):
      upstream "ok"          → "ok"
      upstream "degraded"    → "loading"   (provider cold-starting)
      upstream "loading"     → "loading"   (kept for backward compat)
      upstream anything else → "unreachable"
      no response from       → "unreachable"

    Note on the "raw" field:
      The "raw" field carries openagent-infra's own /health response body
      passed through unchanged. openagent-api does not parse, rename, or
      drop any of its keys. openagent-infra's body contains base_model and
      nervous_system status fields (each "ok" | "unreachable" | "not
      configured") alongside its proxy and status fields. The top-level
      "status" field semantics ("ok" vs "degraded") are what the
      status-mapping table above keys on; "raw" is informational only.

    The "degraded" → "loading" translation deserves explanation.
    openagent-infra reports "degraded" when its FastAPI proxy is healthy but
    its upstream provider is cold-starting (a scale-to-zero provider can
    take minutes to spin up on the first request). Semantically that's the
    same condition the frontend has always called "loading" — model isn't
    ready, hold the chat input closed. We translate the field value so the
    frontend's gate-open loop continues to read consistent semantics
    regardless of which upstream contract version it's against.

    The upstream GET, the timeout config, the JSON-parsing, and the
    status-vocabulary translation all live inside InfraClient.check_health().
    This endpoint just invokes the client and drops the returned tuple into
    the response payload. The mapping table above stays here as
    cross-service documentation; the implementation lives in
    src/client/infra.py.

    /health does NOT emit any events to openagent-logger. The endpoint is
    polled every 3 seconds by the frontend gate-open loop; emitting on every
    poll would flood openagent-logger with thousands of near-identical
    events per hour with no operational value. If operational visibility
    into /health behaviour becomes needed later, a dedicated "health_check"
    ops_event can be added with an intentional sampling policy.

    Note on openagent-memory and openagent-logger: /health intentionally does
    NOT include either's status. Both are non-essential to serving a /chat
    response — the logger is fire-and-forget, and memory retrieval fails open
    to "recent turns only". Coupling the gate-open signal to either would
    make a degraded but non-fatal dependency look like a hard outage to the
    frontend. To check those directly, query their own /health endpoints.
    """
    upstream_status: str = "unreachable"
    upstream_raw: Optional[Dict[str, Any]] = None

    # The entire upstream call, timeout, parse, and status translation is
    # encapsulated in InfraClient.check_health(). It never raises — any
    # error translates internally to ("unreachable", None). The endpoint
    # shape stays consistent regardless of upstream condition.
    if infra_client is not None:
        upstream_status, upstream_raw = await infra_client.check_health()

    # The top-level status reflects openagent-infra's status. openagent-api
    # itself is always "up" if this code is running — there is no degraded
    # mode for openagent-api short of a crash, in which case the frontend
    # would not get a response at all.
    return JSONResponse(
        status_code=200,
        content={
            "status": upstream_status,
            "openagent_api": {
                "version": API_VERSION,
                "identity_loaded": bool(identity),
            },
            "openagent_infra": {
                "url": config.INFRA_URL,
                "status": upstream_status,
                "raw": upstream_raw,
            },
        },
    )


# ============================================================================
# END OF FILE
# ============================================================================