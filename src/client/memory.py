#!/usr/bin/env python3
# ============================================================================
# openagent-api - Memory Client
# Maintainer: William McKeon
# Outbound HTTP client for the openagent-memory retrieval layer
# ============================================================================
#
# ROLE:
#   MemoryClient encapsulates the outbound HTTP boundary from openagent-api
#   to openagent-memory (port 8004) — the session-scoped RAG layer. It is the
#   third client module in src/client/, a sibling to InfraClient
#   (src/client/infra.py) and LoggerClient (src/client/logger.py), and
#   follows the same shape: owns its own httpx.AsyncClient, owns its own
#   timeout config, owns its own header attachment, exposes lifecycle methods
#   (start/stop/aclose), and presents a small typed surface for callers in
#   backend/api.py.
#
#   backend/api.py imports MemoryClient from client.memory, instantiates it
#   at lifespan startup (only when openagent-memory is configured — memory is
#   an OPT-IN feature, not a refuse-to-boot dependency like the logger),
#   calls retrieve() on the /chat hot path before prompt assembly, calls
#   ingest_turn_pair_background() from the successful stream_complete branch,
#   and calls stop() at lifespan shutdown.
#
#   The client is transport-only. It does not know about request_id, the
#   per-call latency timer, the prompt-assembly logic, or the dedup against
#   the recent-N window — those all belong to backend/api.py. It does not
#   build the final query (openagent-api owns prompt assembly; memory only
#   ranks). It does not talk to openagent-infra (memory calls /embed from its
#   OWN repo's client, never through openagent-api).
#
# TWO BOUNDARIES, TWO FAILURE POLICIES:
#
#   retrieve()  — HOT PATH, FAIL-OPEN.
#     Awaited on the /chat hot path before prompt assembly, but aggressively
#     bounded (MEMORY_RETRIEVE_TIMEOUT) and fail-open: any timeout, transport
#     error, non-200, unparseable body, or degraded:true response returns
#     ([], degraded=True) and NEVER raises. A memory outage (or a cold
#     embedder behind memory) degrades the answer to "recent turns only" and
#     never delays the user's first token. This preserves openagent-api's
#     invariant that nothing non-essential blocks /chat.
#
#   ingest() / ingest_turn_pair_background() — OFF THE USER'S PATH.
#     Both the user turn and the assistant turn are ingested AFTER a clean
#     stream completion, via a tracked background task (asyncio.create_task),
#     sequentially (user first, then assistant, so created_at reflects turn
#     order). The task is kept in an in-flight set so it is not
#     garbage-collected mid-flight, and is only cancelled at shutdown — never
#     when the model responds, because cancelling a slow ingest (cold
#     embedder) would lose the turn it was meant to persist.
#
#     ingest() itself is NOT fail-open, per openagent-memory's contract: a
#     failed ingest returns a real signal (memory answers 503 when its
#     embedder is unavailable, since a silently-dropped ingest would remove a
#     turn from all future retrieval). ingest() surfaces that to its caller
#     by raising MemoryIngestError. The background wrapper catches it, logs a
#     WARNING, and emits a "memory_ingest_error" ops_event — but never lets it
#     reach the /chat handler (which has already returned by then).
#
# WHY ingest emits an ops_event but retrieve does not:
#   Logger emission is normally the caller's job (InfraClient deliberately
#   never emits — see its header). retrieve() honours that: it returns the
#   degraded flag and backend/api.py emits the retrieve-degraded ops_event
#   with the request_id context this client does not hold. But the ingest
#   background task is DETACHED — the /chat handler has returned before it
#   runs, so api.py cannot emit on its behalf. To keep ingest failures
#   visible in the capture layer rather than only in stdout, the caller
#   injects an `on_event` callback (a thin closure over
#   logger_client.emit_ops_event with request_id/session_id pre-bound). The
#   task calls it on failure. MemoryClient still never imports or knows about
#   LoggerClient; it only calls the callback it was handed.
#
# SESSION SCOPE:
#   openagent-memory is session-scoped — it searches within ONE conversation,
#   keyed by session_id. openagent-api supplies session_id (today from the
#   MEMORY_SESSION_ID env var; later from a frontend that manages
#   conversations). This client just forwards whatever session_id it is
#   given on every retrieve/ingest.
#
# HMAC (NOT TODAY — SCAFFOLDED FOR LATER):
#   openagent-memory uses transport-key auth only (X-API-Key: MEMORY_API_KEY)
#   and defines NO HMAC contract yet. The module-level signing helpers below
#   (_canonical_payload_json, _sign, and the _maybe_sign hook in _post) are
#   present but INERT — they mirror openagent-logger's signing shape so that,
#   when memory adds payload signing, the change is localized: implement
#   _sign() against memory's future security.py (confirmed byte-for-byte, the
#   same cross-repo discipline the logger boundary requires), wire a
#   hmac_secret through the constructor, and _post() already attaches the
#   signature. Until then, nothing is signed and _maybe_sign() returns None.
#
# RULES — WHAT THIS FILE MUST NEVER DO:
#   ❌ Block or delay the /chat hot path. retrieve() is bounded + fail-open;
#      ingest is fired as a detached background task. Neither may stall the
#      user-visible stream.
#   ❌ Raise from retrieve(). Every failure path returns ([], True). A memory
#      problem must never surface as a /chat error.
#   ❌ Let an ingest failure escape into the /chat handler. The background
#      task catches everything, logs, and (optionally) emits an ops_event via
#      the injected callback. It never re-raises into request-handler code.
#   ❌ Cancel in-flight ingest tasks except at shutdown. Cancelling a slow
#      ingest (cold embedder) would lose the very turn it was persisting.
#   ❌ Retry failed calls. retrieve fails open; ingest surfaces the failure
#      once. Retrying here would pile up latency (retrieve) or memory
#      pressure (ingest).
#   ❌ Log MEMORY_API_KEY. It is supplied at construction and pre-attached to
#      the httpx.AsyncClient at start(); logging it would defeat the
#      compartmentalized-auth model.
#   ❌ Import or reach into LoggerClient. Failure visibility flows only
#      through the caller-injected on_event callback.
#   ❌ Build the prompt, dedup against recent-N, or interpret retrieved turns
#      beyond passing them back. Assembly is backend/api.py's job.
#
# RULES — WHAT THIS FILE MUST ALWAYS DO:
#   ✅ Attach X-API-Key: <MEMORY_API_KEY> as a default header on the
#      httpx.AsyncClient at start() time. Every outbound request inherits it.
#   ✅ Bound retrieve() with a short, configurable read timeout
#      (retrieve_timeout) so a cold embedder behind memory fails open fast.
#   ✅ Give ingest() a longer read timeout (ingest_timeout) than retrieve —
#      it runs off the user's path and must accommodate memory's own cold
#      embedder (memory bounds its /embed at ~10s).
#   ✅ Ingest user-then-assistant sequentially so created_at ordering matches
#      turn order. Skip empty content (memory requires content ≥ 1 char).
#   ✅ Match the InfraClient / LoggerClient lifecycle surface: start(),
#      stop(), aclose() (alias). stop() drains in-flight ingest tasks (with a
#      timeout) before cancelling stragglers and closing the httpx client.
#   ✅ Log a single INFO line at start() and at stop(), matching the
#      InfraClient / LoggerClient style, so cross-service correlation in
#      operator logs stays clean.
# ============================================================================

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import httpx


# Module-level logger. Uses the openagent-api logging hierarchy so the format
# string set up in backend/api.py's setup_logging() applies here
# automatically. WARNINGs and above are visible by default; DEBUG output
# (per-ingest success, per-retrieve degradation detail) appears only when
# OPENAGENT_LOG_LEVEL=DEBUG.
logger = logging.getLogger("openagent-api.client.memory")


__all__ = ["MemoryClient", "MemoryIngestError"]


# A thin callback the caller (backend/api.py) injects so the detached ingest
# background task can surface failures into the capture layer without this
# client ever knowing about LoggerClient. Signature: (action, outcome,
# details) -> None. backend/api.py binds request_id and session_id into the
# closure before handing it over.
OpsEventEmitter = Callable[[str, str, Dict[str, Any]], None]


# ============================================================================
# EXCEPTIONS
# ============================================================================

class MemoryIngestError(Exception):
    """
    Raised by ingest() when a single-turn ingest does not succeed.

    Unlike retrieve() (which fails open and never raises), ingest is NOT
    fail-open: openagent-memory deliberately answers 503 when its embedder is
    unavailable, because a silently-dropped ingest would remove a turn from
    all future retrieval with no signal. ingest() surfaces that by raising
    this exception; the background wrapper catches it, logs, and emits a
    memory_ingest_error ops_event. It never reaches the /chat handler.

    Attributes:
        status_code: The HTTP status openagent-memory returned, if the
                     failure was a non-201 response; None for transport-level
                     failures (timeout, connection refused) or a
                     called-before-start() programming error.
    """

    def __init__(self, message: str, status_code: Optional[int] = None) -> None:
        super().__init__(message)
        self.status_code = status_code


# ============================================================================
# SIGNING SCAFFOLD (INERT TODAY — SEE HEADER "HMAC")
# ============================================================================
# openagent-memory uses transport-key auth only and defines no HMAC contract
# yet. These helpers are present but no-ops so that adding payload signing
# later is a localized change. They mirror the shape of
# openagent-logger/src/client/logger.py's signing helpers intentionally.

def _canonical_payload_json(payload: Dict[str, Any]) -> str:
    """
    Canonical JSON serialisation reserved for future HMAC signing.

    Matches the openagent-logger contract byte-for-byte (sort_keys=True,
    separators=(",", ":"), default=str, ensure_ascii=False) so that, when
    openagent-memory adds payload signing, the canonical form is already
    correct and only the signing scheme plus a byte-for-byte confirmation
    against memory's security.py remain to be done.

    Not called on any live path today.
    """
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
        ensure_ascii=False,
    )


def _sign(secret: str, payload: Dict[str, Any]) -> Optional[str]:
    """
    STUB — inert today.

    openagent-memory uses transport-key auth only and defines no HMAC
    contract yet, so this returns None and nothing is signed. When memory
    adds signing, implement this to mirror openagent-logger's _sign()
    (HMAC-SHA256 over the agreed canonical string built from
    _canonical_payload_json), CONFIRMED byte-for-byte against memory's
    security.py — the same cross-repo discipline the logger boundary
    requires. Returning None keeps _post()'s signature attachment a no-op.
    """
    # Intentionally inert. Do NOT implement a signing scheme here until
    # openagent-memory's security.py defines one to match against.
    return None


# ============================================================================
# MEMORY CLIENT
# ============================================================================

class MemoryClient:
    """
    Outbound HTTP client for openagent-memory (the session-scoped RAG layer).

    Wraps a single httpx.AsyncClient with timeouts and X-API-Key
    pre-attached. Exposes the surface backend/api.py needs: ``retrieve()``
    (hot-path, fail-open ranking), ``ingest()`` (single-turn write, surfaces
    failures), and ``ingest_turn_pair_background()`` (the detached
    user-then-assistant write fired after a clean stream). Lifecycle is
    managed by FastAPI's lifespan handler — call ``start()`` at startup and
    ``stop()`` at shutdown.

    Construction is pure config — no I/O happens until ``start()``. Both
    ``start()`` and ``stop()`` are idempotent.

    Attributes:
        retrieve_timeout: Read timeout (seconds) for /retrieve. Short by
            design — a cold embedder behind memory should fail open fast so
            the /chat hot path is never delayed. A retrieve that exceeds this
            returns ([], degraded=True).
        ingest_timeout: Read timeout (seconds) for /ingest. Longer than
            retrieve because ingest runs off the user's path and must
            accommodate memory's own cold-embedder window (memory bounds its
            /embed at ~10s).
        connect_timeout: TCP connect timeout (seconds) for both operations.
            Short — if memory is unreachable, fail fast (retrieve fails open;
            ingest raises).
    """

    def __init__(
        self,
        url: str,
        api_key: str,
        retrieve_timeout: float = 5.0,
        ingest_timeout: float = 15.0,
        connect_timeout: float = 5.0,
    ) -> None:
        """
        Configure the client. No I/O happens here.

        Args:
            url: Base URL of openagent-memory (e.g. http://openagent-memory:8004
                 or http://host.docker.internal:8004). Trailing slashes are
                 stripped. Must be non-empty.
            api_key: X-API-Key value (MEMORY_API_KEY) attached as a default
                 header on every outbound request. Must match
                 openagent-memory's MEMORY_API_KEY byte-for-byte. Never
                 logged.
            retrieve_timeout: Read timeout in seconds for /retrieve.
            ingest_timeout: Read timeout in seconds for /ingest.
            connect_timeout: TCP connect timeout in seconds.

        Raises:
            ValueError: if url or api_key is empty.
        """
        if not url:
            raise ValueError(
                "MemoryClient.url is required (got empty string)."
            )
        if not api_key:
            raise ValueError(
                "MemoryClient.api_key is required (got empty string)."
            )

        self._url: str = url.rstrip("/")
        self._api_key: str = api_key
        self._retrieve_timeout: float = retrieve_timeout
        self._ingest_timeout: float = ingest_timeout
        self._connect_timeout: float = connect_timeout

        # Reserved for future HMAC signing — see header "HMAC". None today,
        # so _maybe_sign() short-circuits and nothing is signed.
        self._hmac_secret: Optional[str] = None

        self._http_client: Optional[httpx.AsyncClient] = None
        self._running: bool = False

        # In-flight ingest tasks. Held so create_task results are not
        # garbage-collected mid-flight; drained and cancelled in stop().
        self._inflight: Set[asyncio.Task] = set()

    # ----------------------------------------------------------------------
    # LIFECYCLE
    # ----------------------------------------------------------------------

    async def start(self) -> None:
        """
        Open the underlying httpx.AsyncClient.

        Idempotent — calling start() on an already-started client is a no-op
        (no second client is created, no log line is emitted).

        Does NOT probe openagent-memory for connectivity. The client starts
        regardless of whether memory is reachable; if memory is down,
        retrieves fail open and ingests surface failures when they run. This
        matches the opt-in, non-essential role of memory in openagent-api.

        The baseline client timeout uses the (longer) ingest profile; the
        retrieve() call passes its own tighter per-call timeout.

        The X-API-Key header is attached at the AsyncClient level so every
        outbound request inherits it automatically.
        """
        if self._http_client is not None:
            return

        self._http_client = httpx.AsyncClient(
            base_url=self._url,
            timeout=httpx.Timeout(
                connect=self._connect_timeout,
                read=self._ingest_timeout,
                write=self._connect_timeout,
                pool=self._connect_timeout,
            ),
            headers={"X-API-Key": self._api_key},
        )
        self._running = True

        logger.info(
            f"MemoryClient started "
            f"(url={self._url}, "
            f"retrieve_timeout={self._retrieve_timeout}s, "
            f"ingest_timeout={self._ingest_timeout}s)"
        )

    async def stop(self, drain_timeout: float = 5.0) -> None:
        """
        Drain in-flight ingest tasks, then close the underlying httpx client.

        Idempotent — calling stop() on an already-stopped (or never-started)
        client is a no-op.

        Sequence:
          1. Mark the client as no longer running so any post-stop
             ingest_turn_pair_background() call is skipped rather than
             scheduling a task that will not be drained.
          2. Wait up to ``drain_timeout`` seconds for in-flight ingest tasks
             to finish. If memory is responsive this typically completes
             quickly.
          3. Cancel any stragglers and await their cancellation.
          4. Close the httpx client.

        Note on shutdown ordering (owned by backend/api.py's lifespan): the
        MemoryClient is stopped BEFORE the LoggerClient, so any
        memory_ingest_error ops_events emitted while draining can still be
        enqueued onto a live logger queue.

        Args:
            drain_timeout: Seconds to wait for in-flight ingests to finish
                           before cancelling. Default 5s.
        """
        if not self._running and self._http_client is None:
            return
        self._running = False

        # Drain in-flight ingest tasks (best-effort, with timeout).
        if self._inflight:
            pending = list(self._inflight)
            logger.info(
                f"MemoryClient stopping; "
                f"{len(pending)} ingest task(s) in flight, "
                f"draining (timeout={drain_timeout}s)"
            )
            try:
                await asyncio.wait_for(
                    asyncio.gather(*pending, return_exceptions=True),
                    timeout=drain_timeout,
                )
                logger.info("MemoryClient ingest tasks drained")
            except asyncio.TimeoutError:
                stragglers = [t for t in pending if not t.done()]
                logger.warning(
                    f"MemoryClient drain timeout after {drain_timeout}s; "
                    f"cancelling {len(stragglers)} ingest task(s)"
                )
                for t in stragglers:
                    t.cancel()
                # Await the cancellations so they settle before we close the
                # client out from under them.
                await asyncio.gather(*pending, return_exceptions=True)

        # Close the httpx client last, after in-flight POSTs have finished or
        # been cancelled.
        if self._http_client is not None:
            try:
                await self._http_client.aclose()
            except Exception as err:
                logger.warning(
                    f"Error closing MemoryClient http client: "
                    f"{type(err).__name__}: {err}"
                )
            self._http_client = None

        logger.info("MemoryClient stopped")

    async def aclose(self) -> None:
        """
        Alias for stop().

        Provided so external code can treat the client like an
        httpx.AsyncClient if desired. Functionally identical to stop().
        """
        await self.stop()

    # ----------------------------------------------------------------------
    # RETRIEVE — HOT PATH, FAIL-OPEN
    # ----------------------------------------------------------------------

    async def retrieve(
        self,
        session_id: str,
        query: str,
        top_k: Optional[int] = None,
    ) -> Tuple[List[Dict[str, Any]], bool]:
        """
        Rank a session's stored turns against the current query.

        Awaited on the /chat hot path before prompt assembly, but bounded by
        ``retrieve_timeout`` and FAIL-OPEN: any timeout, transport error,
        non-200 status, unparseable body, or a degraded:true response returns
        ``([], True)``. This method NEVER raises — a memory problem degrades
        the answer to "recent turns only" and never blocks or fails /chat.

        Args:
            session_id: Scopes the search to one conversation. Forwarded
                        verbatim (today from MEMORY_SESSION_ID).
            query:      The current user message; memory embeds and compares
                        it against PRIOR stored turns for this session.
            top_k:      Optional cap on results. When None, the field is
                        omitted from the request body and openagent-memory
                        applies its own default (MEMORY_TOP_K_DEFAULT, 5).

        Returns:
            Tuple of (retrieved, degraded):
              * retrieved — list of turn dicts as returned by memory, each
                shaped {id, role, content, score, created_at}, ordered by
                descending score. Empty on any failure or when degraded.
              * degraded  — True when retrieval failed open (memory or its
                embedder unavailable). backend/api.py uses this flag to skip
                the retrieved block and emit a retrieve-degraded ops_event.
        """
        if self._http_client is None:
            logger.debug(
                "MemoryClient.retrieve() called before start() — failing open."
            )
            return [], True

        body: Dict[str, Any] = {"session_id": session_id, "query": query}
        if top_k is not None:
            body["top_k"] = top_k

        timeout = httpx.Timeout(
            connect=self._connect_timeout,
            read=self._retrieve_timeout,
            write=self._retrieve_timeout,
            pool=self._connect_timeout,
        )

        # ---------- POST ----------
        try:
            resp = await self._post("/retrieve", body, timeout)
        except httpx.HTTPError as err:
            logger.warning(
                f"openagent-memory /retrieve unreachable "
                f"({type(err).__name__}); failing open (recent turns only)."
            )
            return [], True

        # ---------- non-200 ----------
        if resp.status_code != 200:
            logger.warning(
                f"openagent-memory /retrieve returned HTTP "
                f"{resp.status_code}; failing open (recent turns only)."
            )
            return [], True

        # ---------- parse ----------
        try:
            payload = resp.json()
        except Exception as parse_err:
            logger.warning(
                f"openagent-memory /retrieve body unparseable "
                f"({type(parse_err).__name__}); failing open."
            )
            return [], True

        if not isinstance(payload, dict):
            logger.warning(
                "openagent-memory /retrieve body was not a JSON object; "
                "failing open."
            )
            return [], True

        # ---------- degraded / results ----------
        degraded = bool(payload.get("degraded", False))
        if degraded:
            # Memory is up but its embedder is unavailable; per memory's
            # contract the list is empty. Treat as fail-open. DEBUG, not
            # WARNING — this is an expected condition during embedder
            # cold-starts, and api.py emits the operational signal.
            logger.debug(
                "openagent-memory /retrieve degraded (embedder unavailable); "
                "proceeding with recent turns only."
            )
            return [], True

        retrieved = payload.get("retrieved")
        if not isinstance(retrieved, list):
            retrieved = []

        return retrieved, False

    # ----------------------------------------------------------------------
    # INGEST — OFF THE USER'S PATH, NOT FAIL-OPEN
    # ----------------------------------------------------------------------

    async def ingest(
        self,
        session_id: str,
        role: str,
        content: str,
    ) -> Dict[str, Any]:
        """
        Embed and store a single turn.

        NOT fail-open: a non-201 response or a transport error is surfaced to
        the caller as MemoryIngestError. openagent-memory answers 503 when its
        embedder is unavailable specifically so the turn-loss is signalled
        rather than silent. Callers run this off the user's critical path
        (see ingest_turn_pair_background) and log/observe failures there.

        Args:
            session_id: Scopes the turn; retrieval filters on it.
            role:       "user" or "assistant".
            content:    The turn text to embed and store. Must be non-empty
                        (memory requires content ≥ 1 char).

        Returns:
            The parsed JSON body openagent-memory returned on 201, shaped
            {session_id, role, stored, duplicate, id}. A duplicate insert is
            a success (stored:false, duplicate:true), not an error.

        Raises:
            MemoryIngestError: on any non-201 status (status_code set) or if
                               called before start() (status_code None).
            httpx.HTTPError:   on transport failure (timeout, connection
                               refused) — propagates to the caller, which
                               treats it the same as MemoryIngestError.
        """
        if self._http_client is None:
            raise MemoryIngestError(
                "MemoryClient.ingest() called before start().",
                status_code=None,
            )

        body: Dict[str, Any] = {
            "session_id": session_id,
            "role": role,
            "content": content,
        }

        timeout = httpx.Timeout(
            connect=self._connect_timeout,
            read=self._ingest_timeout,
            write=self._connect_timeout,
            pool=self._connect_timeout,
        )

        resp = await self._post("/ingest", body, timeout)

        if resp.status_code != 201:
            detail = ""
            try:
                detail = resp.text[:200]
            except Exception:
                pass
            raise MemoryIngestError(
                f"openagent-memory /ingest returned HTTP "
                f"{resp.status_code}: {detail}",
                status_code=resp.status_code,
            )

        try:
            return resp.json()
        except Exception:
            # 201 but unparseable body — treat as a successful store and
            # return a minimal shape so callers don't KeyError.
            return {"stored": True, "duplicate": False}

    def ingest_turn_pair_background(
        self,
        session_id: str,
        user_text: str,
        assistant_text: str,
        on_event: Optional[OpsEventEmitter] = None,
    ) -> None:
        """
        Fire-and-forget the user+assistant ingest as a tracked background
        task. Synchronous and non-blocking — schedules the work and returns.

        Called from the successful stream_complete branch in backend/api.py,
        where both texts are already in scope (user_text == the captured
        input_text, assistant_text == the assembled output_text). The two
        turns are ingested SEQUENTIALLY (user first, then assistant) inside a
        single task so created_at reflects turn order. The task is held in an
        in-flight set (so it is not garbage-collected) and is only cancelled
        at shutdown — never when the model responds.

        Failures of either ingest are caught inside the task: logged at
        WARNING, and (if on_event was provided) reported as a
        "memory_ingest_error" ops_event. They never propagate to the /chat
        handler, which has already returned by the time this runs.

        Args:
            session_id:     Scopes both turns. Forwarded verbatim.
            user_text:      The current user message (memory's "user" turn).
                            Skipped if empty.
            assistant_text: The assembled visible answer (memory's
                            "assistant" turn). Skipped if empty.
            on_event:       Optional callback (action, outcome, details) ->
                            None that backend/api.py binds to
                            logger_client.emit_ops_event with request_id and
                            session_id pre-bound. Used to surface ingest
                            failures into the capture layer. This client never
                            touches LoggerClient directly.
        """
        if not self._running or self._http_client is None:
            logger.debug(
                "MemoryClient.ingest_turn_pair_background() called while not "
                "running; skipping ingest."
            )
            return

        task = asyncio.create_task(
            self._ingest_pair(session_id, user_text, assistant_text, on_event)
        )
        self._inflight.add(task)
        # Discard from the in-flight set when done so it doesn't grow
        # unbounded; the set only ever holds genuinely pending tasks.
        task.add_done_callback(self._inflight.discard)

    # ----------------------------------------------------------------------
    # INTERNAL: INGEST TASK BODY + POST
    # ----------------------------------------------------------------------

    async def _ingest_pair(
        self,
        session_id: str,
        user_text: str,
        assistant_text: str,
        on_event: Optional[OpsEventEmitter],
    ) -> None:
        """
        Sequentially ingest the user turn then the assistant turn.

        Each ingest is independently guarded — a failure on the user turn
        does not skip the assistant turn (though if memory's embedder is down,
        both will typically fail). CancelledError (shutdown) propagates.
        """
        await self._ingest_one_safe(session_id, "user", user_text, on_event)
        await self._ingest_one_safe(
            session_id, "assistant", assistant_text, on_event
        )

    async def _ingest_one_safe(
        self,
        session_id: str,
        role: str,
        content: str,
        on_event: Optional[OpsEventEmitter],
    ) -> None:
        """
        Ingest one turn, swallowing and reporting any failure.

        Empty content is skipped (memory requires content ≥ 1 char).
        CancelledError is re-raised so shutdown can cancel the task; every
        other exception is logged and optionally reported via on_event, never
        propagated.
        """
        if not content:
            logger.debug(
                f"MemoryClient: skipping empty {role} turn ingest "
                f"(session_id={session_id})."
            )
            return

        try:
            result = await self.ingest(session_id, role, content)
        except asyncio.CancelledError:
            # Shutdown cancelled us mid-ingest; let it propagate.
            raise
        except Exception as err:
            status_code = getattr(err, "status_code", None)
            logger.warning(
                f"openagent-memory ingest failed "
                f"(role={role}, session_id={session_id}, "
                f"{type(err).__name__}: {err}); turn not stored."
            )
            if on_event is not None:
                try:
                    on_event(
                        "memory_ingest_error",
                        "failure",
                        {
                            "turn": role,
                            "error_type": type(err).__name__,
                            "status_code": status_code,
                        },
                    )
                except Exception as emit_err:
                    # The failure-reporting path must never break the task.
                    logger.debug(
                        f"MemoryClient: on_event callback raised "
                        f"{type(emit_err).__name__}: {emit_err}"
                    )
        else:
            logger.debug(
                f"openagent-memory ingest ok "
                f"(role={role}, session_id={session_id}, "
                f"stored={result.get('stored')}, "
                f"duplicate={result.get('duplicate')})"
            )

    async def _post(
        self,
        path: str,
        body: Dict[str, Any],
        timeout: httpx.Timeout,
    ) -> httpx.Response:
        """
        POST a JSON body to openagent-memory.

        X-API-Key is inherited from the AsyncClient's default headers. The
        per-call timeout distinguishes the hot-path retrieve profile from the
        off-path ingest profile. _maybe_sign() is consulted for a future HMAC
        signature header; it returns None today, so no signature is attached.

        Transport errors (httpx.HTTPError) propagate to the caller —
        retrieve() catches them to fail open; ingest() lets them propagate to
        the background wrapper.
        """
        if self._http_client is None:
            # Defensive — callers guard this, but never dereference None.
            raise httpx.HTTPError("MemoryClient._post: http client is None")

        headers: Optional[Dict[str, str]] = None
        signature = self._maybe_sign(body)
        if signature is not None:
            headers = {"X-Signature": signature}

        return await self._http_client.post(
            path,
            json=body,
            timeout=timeout,
            headers=headers,
        )

    def _maybe_sign(self, payload: Dict[str, Any]) -> Optional[str]:
        """
        Return a payload signature, or None when signing is not configured.

        INERT today: openagent-memory uses transport-key auth only, so
        self._hmac_secret is None and this short-circuits to None. When memory
        adds HMAC, wire a secret through the constructor, implement _sign()
        against memory's security.py, and _post() will start attaching the
        returned signature — a localized change with no other call sites to
        touch.
        """
        if self._hmac_secret is None:
            return None
        return _sign(self._hmac_secret, payload)  # pragma: no cover (future)


# ============================================================================
# END OF FILE
# ============================================================================