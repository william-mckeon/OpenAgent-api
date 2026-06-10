# openagent-api — Datasheet

> Reference document for building on top of, or integrating with, openagent-api.
> Intended audience: **openagent-frontend**, **openagent-infra**, **openagent-logger**,
> **openagent-memory**, and any other service in the OpenAgent system that needs to
> understand what openagent-api is, what it owns, and how it talks to the rest of the system.

---

## Quick Reference

| Item | Value |
|---|---|
| Role | The Identity Gateway — auth + persona + prompt assembly + SSE relay + fire-and-forget event emitter (+ optional memory retrieve/ingest) |
| Framework | FastAPI on uvicorn |
| Language | Python 3.11 |
| Protocol in (frontend) | HTTP/1.1 |
| Protocol out (openagent-infra) | HTTP/1.1 + Server-Sent Events (SSE consumer + re-emitter) |
| Protocol out (openagent-logger) | HTTP/1.1 (fire-and-forget JSON POST) |
| Protocol out (openagent-memory) | HTTP/1.1 (request/response retrieve + off-path ingest) — **optional** |
| Host port | `8001` |
| Container port | `8001` |
| Auth in | `X-API-Key: OPENAGENT_API_KEY` (required on /chat and /health) |
| Auth out (openagent-infra) | `X-API-Key: INFRA_API_KEY` (attached on every upstream call) |
| Auth out (openagent-logger) | `X-API-Key: LOGGER_API_KEY` + HMAC-SHA256 signature keyed with `LOGGER_HMAC_SECRET` on every event payload |
| Auth out (openagent-memory) | `X-API-Key: MEMORY_API_KEY` (transport-key only — **no HMAC** on this boundary today) — optional |
| Inbound endpoints | `POST /chat` (SSE), `GET /health` |
| Outbound consumed | `POST /chat`, `GET /health` (openagent-infra); `POST /retrieve`, `POST /ingest` (openagent-memory, optional) |
| Outbound emitted | `POST /events` (openagent-logger, fire-and-forget; 5 core event types per /chat, + 2 memory-conditional when memory is enabled) |
| Backend dependency | openagent-infra (`OPENAGENT_INFRA_URL`, typically `:8002`) — required |
| Backend dependency | openagent-logger (`LOGGER_URL`, typically `:8003`) — required |
| Backend dependency | openagent-memory (`MEMORY_URL`, typically `:8004`) — **optional / opt-in** |
| Reasoning effort | Pass-through field on `/chat` (low / medium / high). No openagent-api default. |
| Session store | None — stateless across requests. `session_id` is threaded onto events from `MEMORY_SESSION_ID`. |
| Persistent store | None in openagent-api — bio.txt is read-only, baked into image. (When memory is enabled, the durable conversation store is openagent-memory's own DB, not here.) |
| In-process state | LoggerClient queue (asyncio.Queue, default 1000) + background drain task; MemoryClient in-flight ingest task set (when memory enabled) |
| System prompt | `src/prompt/bio.txt`, baked into image |
| Version | 1.0.0 |

---

## Overview

`openagent-api` is the **product-layer backend** of the OpenAgent system. It is a stateless (across requests) FastAPI gateway that owns five concerns (the fifth optional) and nothing else:

1. **The persona** — `src/prompt/bio.txt`, prepended as the first system message on every upstream call.
2. **The compartmentalized auth chain** — validates `OPENAGENT_API_KEY` inbound, attaches `INFRA_API_KEY` outbound to `openagent-infra`, attaches `LOGGER_API_KEY` and HMAC-signs with `LOGGER_HMAC_SECRET` outbound to `openagent-logger`, and (when memory is enabled) attaches `MEMORY_API_KEY` outbound to `openagent-memory`. All secrets are independent values.
3. **The SSE relay** — opens a streaming POST to `openagent-infra` and pumps tokens byte-for-byte back to the frontend, with optional `reasoning_effort` pass-through and a side-channel parser that accumulates the visible answer for `conversation_capture`.
4. **The fire-and-forget event emitter** — every `/chat` call produces structured events (`request_received`, `upstream_call`, `[upstream_error if applicable]`, `stream_complete`, `conversation_capture` — plus `memory_retrieve_degraded` / `memory_ingest_error` when memory is enabled and degrades) that `openagent-api` enqueues for a background drain task to deliver to `openagent-logger`. The `/chat` hot path never blocks on logger availability.
5. **Prompt assembly + the optional memory boundary** — `openagent-api` owns how the upstream prompt is built. Without memory, that is simply `[bio] + [the frontend's message list]`. When `openagent-memory` is configured, the gateway retrieves relevant prior turns *before* assembling the prompt (on the hot path, but bounded and fail-open) and ingests the completed turn pair *afterward* (off the hot path, as a background task). Memory is **opt-in**; absent its configuration the gateway forwards full history exactly as before. Memory only *ranks*; the gateway *builds* the prompt.

It is intentionally minimal. It does not load a model, run inference, host a database, authenticate users, validate session lifecycle, strip PII from captures, or interpret the model's reasoning format (the frontend's UX policy decision). It is an HTTP gateway with a side-channel observability sink and an optional retrieval-augmented assembly step, and nothing more.

---

## Where This Service Fits

```text
┌──────────────────────────────────────────────────────────────┐
│                    Browser (user)                            │
│                 http://localhost:8000                        │
└───────────────────────────┬──────────────────────────────────┘
                            │ HTTPS / WebSocket (Streamlit)
                            ▼
┌──────────────────────────────────────────────────────────────┐
│    openagent-frontend                                        │
│    Streamlit UI on container :8501, host :8000               │
│                                                              │
│    Owns: chat UI, in-session conversation state,             │
│          SSE consumption, reasoning-format display policy,   │
│          error display                                       │
└───────────────────────────┬──────────────────────────────────┘
                            │ HTTP POST /chat (SSE response)
                            │ HTTP GET  /health
                            │ Headers:  X-API-Key (OPENAGENT_API_KEY)
                            ▼
┌──────────────────────────────────────────────────────────────┐
│    openagent-api    ←── YOU ARE READING THIS DATASHEET       │
│    FastAPI + uvicorn on :8001                                │
│                                                              │
│    Owns: persona (bio.txt), auth boundaries,                 │
│          prompt assembly, SSE relay byte-for-byte,           │
│          side-channel parser, /health proxy,                 │
│          reasoning_effort pass-through,                      │
│          fire-and-forget event emission to openagent-logger, │
│          request_id correlation (UUID4 per /chat),           │
│          (optional) retrieve/ingest via openagent-memory     │
└────────────┬──────────────────────────────────┬──────────────┘
             │                                  │
             │ HOT PATH                         │ FIRE-AND-FORGET
             │ (blocks /chat response)          │ (never blocks /chat)
             │ HTTP POST /chat (SSE)            │ HTTP POST /events
             │ HTTP GET  /health                │ per /chat call
             │ X-API-Key (INFRA_API_KEY)        │ X-API-Key (LOGGER_API_KEY)
             │ Target: OPENAGENT_INFRA_URL      │ + HMAC-SHA256 signature
             │                                  │   (LOGGER_HMAC_SECRET)
             │                                  │ Target: LOGGER_URL
             ▼                                  ▼
┌──────────────────────────────────┐  ┌──────────────────────────────┐
│    openagent-infra               │  │    openagent-logger          │
│    (separate repo, separate      │  │    (separate repo, separate  │
│     Docker stack)                │  │     Docker stack)            │
│    FastAPI proxy on :8002        │  │    FastAPI capture on :8003  │
│                                  │  │                              │
│    Owns: model proxy, SSE        │  │    Owns: HMAC verification,  │
│          streaming, API key      │  │          replay-window check │
│          validation, reasoning   │  │          (300s), partitioned │
│          effort default,         │  │          PostgreSQL writes   │
│          "Reasoning: <level>"    │  │          (3 tables:          │
│          injection,              │  │            ops_events   90d  │
│          PROVIDER_API_KEY        │  │            conv_captures 180d│
│    Stateless — full messages     │  │            audit_events ~7yr)│
│    list sent on every request    │  │                              │
└───────────────────┬──────────────┘  └───────────────┬──────────────┘
                    │                                 │
                    │ HTTPS POST                      │ PostgreSQL TCP
                    │ Authorization: Bearer           │ Schema: openagent_logger
                    │ PROVIDER_API_KEY                │ Monthly RANGE partitions
                    ▼                                 ▼
┌──────────────────────────────────┐  ┌──────────────────────────────┐
│    BYOC Compute Provider         │  │    PostgreSQL (shared        │
│    base reasoning model          │  │     instance, schema-        │
│    Scales to zero when idle      │  │     separated)               │
└──────────────────────────────────┘  └──────────────────────────────┘
```

The diagram above shows the two always-present boundaries — the openagent-infra hot path and the openagent-logger fire-and-forget sibling. The **optional** openagent-memory boundary is documented just below so the core diagram stays readable.

### Memory boundary (optional)

When `MEMORY_URL` and `MEMORY_API_KEY` are both set, `openagent-api` gains a third outbound boundary to `openagent-memory` (`:8004`). It is consulted twice per turn — once on the hot path (retrieve, before assembly) and once off the user's path (ingest, after a clean stream):

```text
   openagent-api (:8001)
     │
     ├─[HOT PATH, before assembly]──▶ POST /retrieve {session_id, query, top_k?}
     │     • query = the current (last) user message
     │     • bounded by MEMORY_RETRIEVE_TIMEOUT, FAIL-OPEN: any timeout / error /
     │       non-200 / degraded:true ⇒ ([], degraded) ⇒ the prompt falls back to
     │       "recent turns only" + a memory_retrieve_degraded event; the user's
     │       first token is never delayed
     │     • returns {retrieved:[{id, role, content, score, created_at}], degraded}
     │
     └─[OFF-PATH, after a clean stream]──▶ POST /ingest ×2 {session_id, role, content}
           • user turn first, then assistant turn (a detached background task,
             so created_at reflects turn order)
           • NOT fail-open: openagent-memory answers 503 when its embedder is down,
             so the loss is signalled — but it runs after the user already has their
             answer, so a failure surfaces only as a memory_ingest_error event and
             never blocks or fails /chat

   openagent-memory (:8004) — session-scoped RAG; owns its OWN PostgreSQL + pgvector.
   Auth: X-API-Key: MEMORY_API_KEY  (transport-key only — NO HMAC on this boundary
   today; the MemoryClient scaffolds signing for a future addition).
```

`openagent-memory` is reached **only** by `openagent-api`, and only when enabled. Memory is opt-in and **not a refuse-to-boot dependency**: absent its configuration (or with `MEMORY_SESSION_ID` unset), `openagent-api` forwards the full message list exactly as before.

**Port topology:**
```text
User → openagent-frontend (:8000) → openagent-api (:8001) → openagent-infra (:8002) → BYOC provider
                                       │
                                       ├─→ openagent-memory (:8004) [optional: retrieve before, ingest after]
                                       └─→ openagent-logger (:8003) [fire-and-forget sibling]
```

`openagent-api` is the **only** client of `openagent-infra`. It is the **only** thing `openagent-frontend` talks to over HTTP for inference. It is also the **only** emitter of events to `openagent-logger`, and — when enabled — the **only** caller of `openagent-memory`.

**Note on openagent-infra's two-model architecture:** `openagent-infra` can serve a second model — a fast **nervous-system** control model — on a separate worker, selectable via an optional `model="nervous_system"` field on its `/chat` request. `openagent-api` **never sets that field**; every `/chat` call routes to the base model by default. The nervous-system route is available at the infra layer for callers that need a fast control model, and is outside `openagent-api`'s surface area today. The diagram shows the base-model path because that is the only path `openagent-api` exercises.

---

## What This Service Owns

A strict list of responsibilities that live inside `openagent-api` and nowhere else.

### 1. The persona (system prompt)

The persona lives in `src/prompt/bio.txt` inside this repo. It is baked into the Docker image at build time (`COPY src/prompt/ /app/src/prompt/`) and loaded once at container startup via `load_identity()`. It is prepended as the first `{"role": "system", "content": <bio>}` message on every `/chat` call to `openagent-infra`. (When memory is enabled, this prepending is the first step of a larger retrieval-augmented assembly — see §10.)

- **`openagent-infra` has no knowledge of this file.** It receives the system message as part of the messages array and (only) appends `Reasoning: <level>` to it before forwarding to the provider. The persona text passes through unchanged.
- **`openagent-frontend` has no knowledge of this file.** It stops carrying any persona and stops sending system messages in the request body. If a frontend sends one anyway, `openagent-api` drops it with a warning log.
- **`openagent-logger` has no knowledge of this file.** The persona text is not emitted to the logger. `conversation_capture.input_text` carries the current user message only (not the system message), and `conversation_capture.output_text` carries the visible answer. The persona never leaves `openagent-api`.
- **`openagent-memory` has no knowledge of this file.** Retrieved and ingested turns are user/assistant turns only; the persona is never ingested or retrieved.
- **No other service stores, duplicates, or overrides the persona.**
- **Changing the persona is a rebuild.** Editing `bio.txt` requires `docker-compose up -d --build` because it is baked into the image (or mount a volume over `/app/src/prompt` for live dev edits).

### 2. The inbound auth boundary (`OPENAGENT_API_KEY`)

`OPENAGENT_API_KEY` authorises `openagent-frontend` (or any future client) to talk to `openagent-api`. It is validated on every `/chat` and `/health` request via the `require_api_key` FastAPI dependency.

- Frontend sends it as the `X-API-Key` header.
- `openagent-api` compares it byte-for-byte against the value in its own environment.
- Mismatch returns `HTTP 401 {"detail": "Invalid or missing API key"}` — same shape as `openagent-infra`'s 401 so the frontend's emoji classifier surfaces 🔐 either way.
- This key never leaves `openagent-frontend` or `openagent-api`. `openagent-infra` never sees it. `openagent-logger` never sees it. `openagent-memory` never sees it. The provider never sees it.

### 3. The outbound auth credential for openagent-infra (`INFRA_API_KEY`)

`INFRA_API_KEY` is the secret `openagent-api` uses to authenticate to `openagent-infra`. It is owned by the `InfraClient` (in `src/client/infra.py`), which constructs its internal `httpx.AsyncClient` at `start()` time with `headers={"X-API-Key": INFRA_API_KEY}` pre-attached so every outbound request carries it automatically.

- This key never leaves `openagent-api`'s environment. The frontend never sees it. The logger never sees it. The memory layer never sees it. The provider never sees it.
- It is **a different value** from `OPENAGENT_API_KEY`. Two independent secrets, separate blast radii.
- It must match `openagent-infra`'s `API_KEY` env var byte-for-byte.
- It is never logged, never echoed in responses, and never included in error messages.

The logger boundary uses a separate pair of secrets (`LOGGER_API_KEY` + `LOGGER_HMAC_SECRET`) and the optional memory boundary a separate single secret (`MEMORY_API_KEY`), all following the same compartmentalization pattern — see §10, "Outbound to openagent-logger", and the Security Model section.

### 4. The SSE relay

`openagent-api`'s `/chat` endpoint is the only stream-handling logic in the product layer. The implementation is an async generator (`sse_pump`) that:

- Opens a streaming POST to `OPENAGENT_INFRA_URL/chat` (via `infra_client.stream_chat(payload)`) with the assembled messages list and optionally `reasoning_effort`.
- Iterates `response.aiter_raw()` and yields each chunk straight through.
- Forwards `data:` events, `\n\n` separators, and the `[DONE]` sentinel byte-for-byte.
- Forwards each event payload — a JSON-encoded OpenAI ChatCompletion chunk with `delta.reasoning` (chain-of-thought) and `delta.content` (visible answer) tokens — without decoding or interpreting the JSON on the relay path.
- **Runs a side-channel parser** in parallel with the byte-for-byte relay. Each yielded chunk is appended to an `event_buffer`, split on `\n\n`, and parsed as JSON to extract `choices[0].delta.content` tokens into an `output_text` accumulator. This accumulator feeds the eventual `conversation_capture` event. **The yield to the frontend happens BEFORE the parse in every loop iteration**, so the parse adds zero latency to the user-visible stream. Parse failures are silently tolerated.
- Detects mid-stream client disconnects via `await http_request.is_disconnected()` and abandons the upstream connection so the provider stops generating.
- Surfaces non-200 upstream responses as in-band SSE error events: `data: [ERROR upstream_status=503]\n\n` followed by `data: [DONE]\n\n`. An `upstream_error` event is also emitted to the logger before yielding the in-band error.

No other service consumes `openagent-infra`'s SSE stream directly. `openagent-api` is the only client.

### 5. The `reasoning_effort` pass-through

`reasoning_effort` is an optional field on the inbound `/chat` request body. Three accepted values: `low`, `medium`, `high`. Pydantic validates the value if present; an invalid value produces HTTP 422 before any upstream call.

The behaviour is **pure pass-through**:

- When the frontend sends a value, `openagent-api` includes it in the upstream payload.
- When the frontend omits the field, `openagent-api` also omits it — `openagent-infra` applies its own server-side default (controlled by `openagent-infra`'s `REASONING_EFFORT` env var).

`openagent-api` holds **no default of its own** for this field. The setting has one source of truth at the upstream layer.

Note: `reasoning_effort` appears on the `request_received`, `upstream_call`, and `conversation_capture` events emitted to the logger as well, so downstream analysis can correlate latency, output length, and reasoning effort.

### 6. The `/health` proxy

`openagent-api` exposes its own `/health` that proxies `openagent-infra`'s `/health`. The frontend polls here every 3 seconds during cold start to gate the chat input. Response shape:

```json
{
  "status": "ok" | "loading" | "unreachable",
  "openagent_api": {"version": "1.0.0", "identity_loaded": true},
  "openagent_infra": {
    "url": "http://...:8002",
    "status": "ok" | "loading" | "unreachable",
    "raw": {}
  }
}
```

The top-level `status` reflects the worst of `(openagent-api state, openagent-infra state)`. The frontend's gate-open loop reads only this field.

**Status mapping (openagent-infra → openagent-api):**

| upstream `status` | openagent-api `status` | Meaning                                              |
|-------------------|------------------------|------------------------------------------------------|
| `ok`              | `ok`                   | Provider worker warm and serving                     |
| `degraded`        | `loading`              | Provider worker cold-starting                        |
| `loading`         | `loading`              | (Kept for backward compat)                           |
| anything else     | `unreachable`          | infra responded but with an unknown state            |
| no response       | `unreachable`          | infra is down or unreachable from openagent-api      |

The `degraded` → `loading` translation: `openagent-infra` reports `degraded` when its FastAPI proxy is healthy but its provider worker is cold-starting (serverless workers scale to zero when idle). Semantically that's the same condition the frontend calls `loading` — model isn't ready, hold the chat input closed. `openagent-api` translates the field so the frontend reads consistent semantics. This translation lives inside `InfraClient.check_health()`, which returns `Tuple[str, Optional[Dict]]` (translated status + raw upstream body) and never raises; any failure to obtain a healthy response translates internally to `("unreachable", None)`.

**Note on the upstream raw body:** the `raw` field carries `openagent-infra`'s `/health` body unchanged. `openagent-api` **forwards it verbatim** — it does not parse, rename, or drop keys. The top-level `status` semantics are stable, so the mapping table works regardless of the exact `raw` shape; `raw` is informational only.

The endpoint requires the same `X-API-Key` as `/chat` because it reveals operational state (upstream URL, version, worker readiness) that should not be public. `/health` does NOT include `openagent-logger`'s or `openagent-memory`'s status and does NOT emit events to the logger: gate-open is polled every 3 seconds, so coupling it to a non-essential dependency would defeat fire-and-forget (logger) / fail-open (memory), and emitting per-poll events would flood the logger with no operational value.

### 7. Frontend-supplied system message rejection

If a `system` message is sent in a `/chat` request body — by an out-of-date frontend, a tampered client, or a misconfigured tool — `openagent-api` drops it server-side and logs a warning. The canonical persona is `bio.txt` and only `bio.txt`. This is defense in depth.

### 8. Error taxonomy mapping (gateway layer)

`openagent-api` maps upstream error conditions to the HTTP status codes the frontend's emoji classifier understands:

| Frontend emoji | Status | Trigger from openagent-api                                                   |
|----------------|--------|------------------------------------------------------------------------------|
| 🔌             | 502    | Cannot reach openagent-infra (TCP connect failed, network unreachable)       |
| ⏳             | 503    | openagent-infra reports `degraded` / `loading` (provider worker cold-start)  |
| 🔌             | 504    | Upstream read timeout during generation                                      |
| 🔐             | 401    | Inbound `X-API-Key` missing or wrong                                         |
| ⚠️              | 400    | Empty messages list / no user message after server-side filtering            |
| ⚠️              | 422    | Malformed request body / invalid `reasoning_effort` value                    |
| ❌             | 500    | Anything not matched above                                                   |

A memory failure never appears in this taxonomy: retrieval is fail-open (degrades to recent-only) and ingest is off the user's path, so neither changes the `/chat` status code.

### 9. Fire-and-forget event emission to openagent-logger

`openagent-api` is the only emitter of events to `openagent-logger`. The core five event types are emitted per `/chat` call (four on the failure path); when memory is enabled, two additional event types can appear conditionally. Each event carries a shared `request_id` (UUID4) and the `session_id` so downstream queries can reconstruct a `/chat`'s full event timeline and join events from one conversation.

The core emission points within `/chat`:
- `request_received` — at ingress, after auth passes
- `upstream_call` — immediately before opening the infra stream
- `upstream_error` — in every exception handler in the SSE pump
- `stream_complete` — after the SSE pump finishes (success or client_disconnect)
- `conversation_capture` — after a successful stream_complete only

The memory-conditional event types (only when memory is enabled):
- `memory_retrieve_degraded` — emitted before `upstream_call` when a retrieve fails open (at most once per `/chat`)
- `memory_ingest_error` — emitted after `conversation_capture` when a background ingest fails (at most twice per `/chat`, once per turn)

The emission is fire-and-forget by design: emit methods are synchronous, do no I/O, return in microseconds, and enqueue onto an in-process `asyncio.Queue`. A background `asyncio.Task` drains the queue and POSTs to the logger; failures result in WARNING logs and dropped events, with no impact on `/chat`. Queue overflow is handled by drop-oldest.

The full wire contract is documented in the [Outbound to openagent-logger](#outbound-to-openagent-logger) section below.

### 10. Prompt assembly and the memory boundary (optional)

`openagent-api` owns how the upstream prompt is built. Without memory, assembly is simply `[system: bio] + [the frontend's message list]`. When `openagent-memory` is enabled (both `MEMORY_URL` and `MEMORY_API_KEY` set) **and** a `session_id` is present (from `MEMORY_SESSION_ID`), `openagent-api` performs full retrieval-augmented assembly:

1. **Retrieve (hot path, fail-open).** Before assembling, `openagent-api` calls `openagent-memory`'s `/retrieve` with the current (last) user message as the query, bounded by `MEMORY_RETRIEVE_TIMEOUT`. Any timeout, transport error, non-200, or `degraded:true` response yields no turns, emits a `memory_retrieve_degraded` event, and assembly proceeds with "recent turns only". Retrieval never blocks or fails `/chat`.
2. **Assemble.** The upstream prompt becomes:
   ```text
   [ system: bio ]
   + [ retrieved older turns, deduped vs the recent window by SHA-256 content hash, chronological ]
   + [ the most recent N turns, verbatim ]   (N = MEMORY_RECENT_N, default 10)
   + [ the current user turn ]
   ```
   `openagent-memory` only *ranks* (returns candidate turns + scores); `openagent-api` *builds* the prompt — choosing which retrieved turns to keep (dropping any already present in the recent-N window or equal to the current turn, matched by SHA-256 content hash — the same key memory uses for its own storage dedupe), the ordering (chronological), and how many recent turns to keep verbatim.
3. **Ingest (off the user's path).** After a successful stream, `openagent-api` fires a detached background task that ingests the user turn then the assistant turn to `/ingest` (sequentially, so `created_at` reflects order). Ingest is *not* fail-open — `openagent-memory` answers 503 when its embedder is down so the loss is signalled — but because it runs after the user already has their answer, a failure surfaces only as a `memory_ingest_error` event and never affects `/chat`. A client-disconnect or errored turn ingests neither side.

The outbound boundary is owned by the `MemoryClient` (`src/client/memory.py`), which constructs its own `httpx.AsyncClient` at `start()` with `headers={"X-API-Key": MEMORY_API_KEY}` and `base_url=MEMORY_URL`. **There is no HMAC on this boundary today** — `openagent-memory` uses transport-key auth only; the client scaffolds signing (mirroring the logger's canonical-payload helpers) so a future addition is localized. `MEMORY_API_KEY` is a different value from every other secret, never leaves `openagent-api`'s environment, and must match `openagent-memory`'s `MEMORY_API_KEY` byte-for-byte.

When memory is disabled, or enabled but `MEMORY_SESSION_ID` is unset, none of the above runs — assembly is `[bio] + [full message list]`, no retrieve/ingest occurs, and the gateway behaves exactly as it did before memory existed. Memory is never a refuse-to-boot dependency.

---

## What This Service Does NOT Own

- **Model serving / inference** → the BYOC compute provider (proxied by `openagent-infra`)
- **Provider authentication (`PROVIDER_API_KEY`)** → `openagent-infra`
- **Model API key validation** → `openagent-infra`
- **`Reasoning: <level>` injection into the system message** → `openagent-infra`
- **Reasoning effort default value** → `openagent-infra` (its `REASONING_EFFORT` env var)
- **Event storage, partitioning, retention** → `openagent-logger`
- **Turn embedding, vector search, and the conversation store** → `openagent-memory` (when enabled); it owns its own PostgreSQL + pgvector
- **Reasoning-format display policy** → `openagent-frontend`
- **In-session conversation state** → `openagent-frontend`
- **Chat UI rendering** → `openagent-frontend`

Not implemented:

- **PII stripping / sanitisation of conversation_captures (or memory turns)** → not implemented; captures are stored raw by the logger, and turns are stored raw by openagent-memory.
- **Session lifecycle and session-id validation** → not implemented; `openagent-api` threads a single static `session_id` from `MEMORY_SESSION_ID` onto events (and uses it to scope memory) but does not manage session lifecycle or validate session ids. When `MEMORY_SESSION_ID` is unset, `session_id` is emitted as `null`.
- **Per-user identity / authentication** → not implemented; `user_id` is emitted as `null` on every event.
- **Persistent conversation history inside openagent-api** → not implemented. When memory is enabled, the durable store is openagent-memory's database, reached over HTTP — never the gateway.
- **Reasoning chain capture in `conversation_capture.output_text`** → not captured. `output_text` is `delta.content` only.
- **Durable retry of failed event submissions or failed ingests** → not implemented; `openagent-api` drops events on POST failure with no retry, and a failed ingest is logged/emitted but not retried.
- **Rate limiting** → not implemented (belongs at a reverse proxy if ever needed).
- **Multi-tenancy** → not supported.

---

## Inbound HTTP Contracts (provided)

### `POST /chat`

Forward a message list, optionally specify reasoning effort, get back an SSE stream. Authenticated.

**Request:**
```text
POST /chat HTTP/1.1
Host: openagent-api:8001
Content-Type: application/json
X-API-Key: <OPENAGENT_API_KEY>

{
  "messages": [
    {"role": "user",      "content": "..."},
    {"role": "assistant", "content": "..."},
    {"role": "user",      "content": "..."}
  ],
  "reasoning_effort": "medium"
}
```

| Field              | Type   | Required | Description                                                                                |
|--------------------|--------|----------|--------------------------------------------------------------------------------------------|
| `messages`         | array  | Yes      | Non-empty list. Must contain at least one `user` message after server-side filtering.      |
| `messages[].role`  | string | Yes      | One of `user`, `assistant`, `system`. `system` messages are dropped server-side.           |
| `messages[].content` | string | Yes    | Non-empty message text.                                                                    |
| `reasoning_effort` | string | No       | One of `low`, `medium`, `high`. Forwarded upstream when present; omitted when absent so `openagent-infra` applies its default. Validated by Pydantic — invalid values produce HTTP 422 before any upstream call. |

There is intentionally **no `session_id` field** on the request body yet — `session_id` comes from the `MEMORY_SESSION_ID` env var today and will move to a request field (or header) once the frontend manages conversations. Memory retrieval/ingest is transparent to this contract.

**Header:**

| Header | Required | Description |
|---|---|---|
| `X-API-Key` | Yes | Must match `OPENAGENT_API_KEY` byte-for-byte. |

**Response:** `text/event-stream`

```text
HTTP/1.1 200 OK
Content-Type: text/event-stream; charset=utf-8
Cache-Control: no-cache
X-Accel-Buffering: no
Connection: keep-alive

data: {"id":"chatcmpl-...","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"role":"assistant","content":""},"finish_reason":null}]}

data: {"id":"chatcmpl-...","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"reasoning":"User"},"finish_reason":null}]}

...  (more reasoning tokens — chain-of-thought)

data: {"id":"chatcmpl-...","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"Hello"},"finish_reason":null}]}

...  (more content tokens — visible answer)

data: {"id":"chatcmpl-...","object":"chat.completion.chunk","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}

data: [DONE]
```

The stream is `openagent-infra`'s stream re-emitted byte-for-byte. Each event payload is a JSON-encoded OpenAI ChatCompletion chunk. Chain-of-thought tokens stream first inside `choices[0].delta.reasoning`, then visible answer tokens inside `choices[0].delta.content`, then a final empty-delta chunk with `finish_reason: "stop"`, then `[DONE]`. `openagent-api` does not decode any of this on the relay path — the frontend's parser routes the two streams. A side-channel parser inside `openagent-api` parses each event in parallel to extract `delta.content` for the `conversation_capture`; that parse is downstream of the yield and adds zero latency. Mid-stream upstream failures are surfaced as in-band events: `data: [ERROR upstream=...]\n\n` followed by `data: [DONE]\n\n`.

**Side effects:** Each call triggers fire-and-forget event emissions to `openagent-logger`:
1. `request_received` at ingress (after auth)
2. `memory_retrieve_degraded` (memory enabled only, before `upstream_call`, when a retrieve fails open)
3. `upstream_call` before opening the infra stream
4. `upstream_error` on every exception path (instead of `stream_complete` and `conversation_capture`)
5. `stream_complete` after the stream finishes (success or client_disconnect)
6. `conversation_capture` after a successful `stream_complete` only (not for client_disconnect)
7. `memory_ingest_error` (memory enabled only, after `conversation_capture`, when a background ingest fails — up to twice)

All emissions share the same `request_id` (UUID4). `session_id` is populated from `MEMORY_SESSION_ID` (null when unset); `user_id` is emitted as `null`. See [Outbound to openagent-logger](#outbound-to-openagent-logger).

When memory is active, a successful stream also triggers an **off-path** background ingest of the user turn then the assistant turn to `openagent-memory` — see §10 and [Outbound HTTP Contracts](#outbound-http-contracts-consumed).

**Generation timing** (warm path):

| Scenario               | reasoning_effort | Approximate duration |
|------------------------|------------------|----------------------|
| Simple greeting        | `low`            | 5–15 seconds         |
| Short factual question | `medium`         | 15–45 seconds        |
| Complex reasoning task | `high`           | 1–3 minutes          |

Cold path: add the provider's serverless worker spin-up time for the first request after the worker has scaled to zero. When memory is enabled, the hot-path retrieve adds at most `MEMORY_RETRIEVE_TIMEOUT` seconds before the first token, and fails open past that bound.

**Error responses:**

| Status | Trigger | Body |
|--------|---------|------|
| `400`  | Empty messages list, or no `user` message after filtering | `{"detail": "..."}` |
| `401`  | `X-API-Key` missing or wrong | `{"detail": "Invalid or missing API key"}` |
| `422`  | Request body fails Pydantic validation (including invalid `reasoning_effort`) | FastAPI default validation error |
| `500`  | openagent-api internal error before stream open | `{"detail": "..."}` |

### `GET /health`

Proxied health check. Authenticated.

**Request:**
```text
GET /health HTTP/1.1
X-API-Key: <OPENAGENT_API_KEY>
```

**Response (always HTTP 200):**
```json
{
  "status": "ok" | "loading" | "unreachable",
  "openagent_api": {"version": "1.0.0", "identity_loaded": true},
  "openagent_infra": {
    "url": "http://openagent-infra:8002",
    "status": "ok" | "loading" | "unreachable",
    "raw": {}
  }
}
```

**Status semantics (top-level):**
- `ok` — openagent-api is up AND openagent-infra reports `ok` AND its provider worker is warm
- `loading` — openagent-api is up but openagent-infra reports `degraded` or `loading` (provider worker cold-starting)
- `unreachable` — openagent-api is up but cannot reach openagent-infra

`/health` does NOT include `openagent-logger`'s or `openagent-memory`'s status. Both are non-essential to serving a `/chat` response (logger is fire-and-forget; memory retrieval fails open), so they are deliberately excluded from the gate-open signal. To check them directly, query their own `/health` endpoints (`LOGGER_URL/health`, `MEMORY_URL/health`).

---

## Outbound HTTP Contracts (consumed)

`openagent-api` is a client of these endpoints, in a request/response (streaming) pattern. Full specs live in the respective service datasheets.

The `openagent-logger` boundary is NOT a consumed contract in this sense; it is a fire-and-forget emission, documented in [Outbound to openagent-logger](#outbound-to-openagent-logger).

### `POST {OPENAGENT_INFRA_URL}/chat` — consumed

**Request:**
```text
POST /chat HTTP/1.1
Content-Type: application/json
X-API-Key: <INFRA_API_KEY>

{
  "messages": [
    {"role": "system",    "content": "<bio.txt>"},
    {"role": "user",      "content": "..."},
    {"role": "assistant", "content": "..."}
  ],
  "reasoning_effort": "medium"
}
```

The `system` message is the `bio.txt` content prepended by `openagent-api`. The remaining messages are the **assembled** message list — either the frontend's payload after `system` filtering (memory off), or `[retrieved older turns] + [recent N turns] + [current turn]` (memory on); see §10. `reasoning_effort` is included only when the frontend sent one. This POST is issued via `InfraClient.stream_chat(payload)` in `src/client/infra.py`.

**Note on the `model` field:** `openagent-infra`'s `/chat` accepts an optional `model` field (`"base"` or `"nervous_system"`) selecting between its two workers — the base reasoning model and a fast nervous-system control model. `openagent-api` **never sets this field**; every request omits it, which routes the call to the base model by default. The nervous-system route is available at the infra layer for callers that need it and is outside `openagent-api`'s surface area.

**Response:** `text/event-stream`. Each event payload is a JSON-encoded OpenAI ChatCompletion chunk. Reasoning tokens appear in `choices[0].delta.reasoning`, visible answer tokens in `choices[0].delta.content`, terminating with an empty-delta chunk (`finish_reason: "stop"`) followed by `data: [DONE]`. `openagent-api` consumes this stream byte-for-byte on the relay path and runs a side-channel parser in parallel to extract content tokens for the `conversation_capture`.

**Timeout handling:**
- Connect timeout: 10 seconds (`UPSTREAM_CONNECT_TIMEOUT`).
- Read timeout: unbounded by default (`UPSTREAM_READ_TIMEOUT=none`) — accommodates provider cold-start and `high` reasoning effort generations.

### `GET {OPENAGENT_INFRA_URL}/health` — consumed

**Request:**
```text
GET /health HTTP/1.1
X-API-Key: <INFRA_API_KEY>
```

(`openagent-api` sends its key on every outbound call, including `/health`. `openagent-infra` does not require auth on `/health`, but the header is harmless.)

**Response:** Always HTTP 200, status in the body. The top-level `status` (`"ok"` means the base worker is reachable, `"degraded"` means it's cold-starting) is what `openagent-api` translates into its own status vocabulary via `InfraClient.check_health()`. `openagent-api` forwards the entire raw body verbatim under `openagent_infra.raw` in its own `/health` response — it does not parse, rename, or drop keys.

**Timeout handling:** 5-second connect/read (`HEALTH_TIMEOUT`). `check_health()` catches all errors internally and never raises — any httpx error, non-200 status, JSON-parse error, or non-dict body translates to `("unreachable", None)`.

### `POST {MEMORY_URL}/retrieve` — consumed (optional)

Issued on the hot path, before prompt assembly — only when memory is enabled AND a `session_id` is set. Via `MemoryClient.retrieve(...)` in `src/client/memory.py`.

**Request:**
```text
POST /retrieve HTTP/1.1
Content-Type: application/json
X-API-Key: <MEMORY_API_KEY>

{ "session_id": "dev-session-001", "query": "<current user message>", "top_k": 5 }
```

| Field | Type | Required | Description |
|---|---|---|---|
| `session_id` | string | Yes | Scopes the search to one conversation (from `MEMORY_SESSION_ID`). |
| `query` | string | Yes | The current (last) user message — the SAME value used as `conversation_capture.input_text` and the user-turn ingest content. |
| `top_k` | int | No | Cap on results; omitted when `MEMORY_TOP_K` is unset, so `openagent-memory` applies its own default (5). |

**Response:** HTTP 200 (always — `openagent-memory` fails open on its side too).
```json
{
  "session_id": "dev-session-001",
  "retrieved": [
    {"id": "...", "role": "assistant", "content": "...", "score": 0.82, "created_at": "2026-05-15T19:53:14Z"}
  ],
  "degraded": false
}
```

`openagent-api` treats the call as **FAIL-OPEN**: any timeout, transport error, non-200, unparseable body, or `degraded:true` ⇒ no retrieved turns + a `memory_retrieve_degraded` event; assembly falls back to "recent turns only". `retrieve()` returns `Tuple[List[Dict], bool]` (retrieved turns, degraded flag) and never raises.

**Timeout handling:** connect 5s; read = `MEMORY_RETRIEVE_TIMEOUT` (default 5.0s). Short by design so a cold embedder behind memory fails open fast and never delays the first token.

### `POST {MEMORY_URL}/ingest` — consumed (optional, off-path)

Issued twice (user turn then assistant turn) AFTER a successful stream, on a detached background task. Via `MemoryClient.ingest_turn_pair_background(...)`.

**Request:**
```text
POST /ingest HTTP/1.1
Content-Type: application/json
X-API-Key: <MEMORY_API_KEY>

{ "session_id": "dev-session-001", "role": "user", "content": "<turn text>" }
```

| Field | Type | Required | Description |
|---|---|---|---|
| `session_id` | string | Yes | Same session scope as retrieve. |
| `role` | string | Yes | `"user"` first, then `"assistant"` — the two turns are ingested sequentially so `created_at` reflects order. |
| `content` | string | Yes | The turn text. The user turn's content is the SAME current-user-message value used as the retrieve query and `conversation_capture.input_text`. Empty content is skipped. |

**Response:** HTTP 201 on success (`{stored, duplicate, id}`); HTTP 503 when `openagent-memory`'s embedder is unavailable. **NOT fail-open** — a non-201 surfaces as a `memory_ingest_error` event (with `turn` role, `error_type`, and `status_code`). Because ingest runs off the user's path, this never blocks or fails `/chat`.

**Timeout handling:** connect 5s; read 15s — longer than retrieve, because ingest is off the user's path and must accommodate `openagent-memory`'s own cold-embedder window (it bounds its embed at ~10s).

**No HMAC:** unlike the logger boundary, `/retrieve` and `/ingest` are **transport-key only**. The `MemoryClient` scaffolds signing for a future addition, but nothing is signed today.

---

## Outbound to openagent-logger

> **Authoritative reference** for the openagent-api → openagent-logger boundary, intended for downstream services (openagent-logger, auditors) that need to verify what openagent-api emits, when, and how. The protocol described here is implemented in `src/client/logger.py` and matched byte-for-byte by openagent-logger's ingress validator.

### Pattern: fire-and-forget

`openagent-api` emits events into an in-process `asyncio.Queue` and returns from each emit method in microseconds. A background `asyncio.Task` drains the queue and POSTs to `openagent-logger`. The `/chat` hot path is never blocked on logger availability. If the logger is unreachable or slow, events queue up (eventually dropping per the overflow policy) while `/chat` continues to serve normally.

This is the OPPOSITE of the `openagent-api` → `openagent-infra` pattern. The infra boundary is fully synchronous (block `/chat` until upstream responds, then stream byte-for-byte). The logger boundary is fully asynchronous (emit and forget). The two patterns reflect their different roles: infra produces the response, the logger captures the side-effect data. (The optional memory boundary is a third pattern again: retrieve is synchronous-but-bounded-and-fail-open on the hot path, while ingest is a detached background task — see §10.)

### Outbound endpoint

```text
POST {LOGGER_URL}/events
Content-Type: application/json
X-API-Key: <LOGGER_API_KEY>
```

All events go to the single `/events` endpoint regardless of `event_type`; `openagent-logger` routes them to the appropriate storage table internally.

### Event envelope (JSON body)

```json
{
  "request_id": "<uuid4 dashed>",
  "session_id": "<from MEMORY_SESSION_ID, or null>",
  "user_id": null,
  "source_service": "openagent-api",
  "client_timestamp": "<ISO 8601 UTC with Z>",
  "event_type": "<event-type string, see below>",
  "payload": {},
  "hmac_signature": "<lowercase hex, 64 chars>"
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `request_id` | string (UUID4 with dashes, 36 chars) | Yes | Generated at `/chat` ingress via `str(uuid.uuid4())`, e.g. `cf8108cb-958d-4f11-a265-d885ac52415d`. All events for one `/chat` call share the same request_id so they can be joined post-hoc by querying the logger's database. |
| `session_id` | string or null | Yes | Populated from the `MEMORY_SESSION_ID` env var (a single static value for now), or `null` when unset. **Independent of whether the memory boundary is enabled** — it threads onto every event whenever the env var is set; it additionally scopes memory retrieve/ingest when memory is active. The logger never validates it. The frontend will mint per-conversation ids later. |
| `user_id` | string or null | Yes | Reserved correlation field. Emitted as `null` — there is no per-user identity in the current stack. |
| `source_service` | string | Yes | Always `"openagent-api"` for events emitted from this service. The logger uses this to scope queries by emitter. |
| `client_timestamp` | string (ISO 8601 UTC) | Yes | UTC timestamp at the moment `openagent-api` enqueued the event. Format: `2026-05-15T19:53:14.123456Z` (always `Z`, microsecond precision). The logger uses this for the replay-window check (events older than 300 seconds are rejected). |
| `event_type` | string | Yes | One of `request_received`, `upstream_call`, `upstream_error`, `stream_complete`, `conversation_capture`, `memory_retrieve_degraded`, `memory_ingest_error`. |
| `payload` | object | Yes | Event-type-specific fields; see below. Always a JSON object. |
| `hmac_signature` | string (lowercase hex, 64 chars) | Yes | HMAC-SHA256 of the canonical string (see below), keyed with `LOGGER_HMAC_SECRET`. Verifiable offline by anyone with the secret. |

### Event types and routing

| event_type | Routed to (openagent-logger table) | Retention |
|---|---|---|
| `request_received` | `ops_events` | 90 days |
| `upstream_call` | `ops_events` | 90 days |
| `upstream_error` | `ops_events` | 90 days |
| `stream_complete` | `ops_events` | 90 days |
| `memory_retrieve_degraded` | `ops_events` | 90 days |
| `memory_ingest_error` | `ops_events` | 90 days |
| `conversation_capture` | `conversation_captures` | 180 days |

Routing is the logger's concern. `openagent-api` just sets the `event_type` value. The `audit_events` table (~7yr retention) in the logger is not currently written by `openagent-api`.

### Per-event payload shapes

#### `request_received`

Emitted at `/chat` ingress, after auth passes, before bio.txt prepending.

```json
{ "messages_count": 7, "reasoning_effort": "medium" }
```

| Field | Type | Description |
|---|---|---|
| `messages_count` | int | Number of messages in the inbound body (before server-side filtering). |
| `reasoning_effort` | string | The `reasoning_effort` from the inbound body, or `"unset"` when the frontend omitted it. |

#### `upstream_call`

Emitted in `sse_pump` immediately before opening the streaming POST to `openagent-infra`.

```json
{ "url": "http://host.docker.internal:8002", "reasoning_effort": "medium" }
```

| Field | Type | Description |
|---|---|---|
| `url` | string | The openagent-infra base URL about to be called. |
| `reasoning_effort` | string | Same as in `request_received`. |

#### `upstream_error`

Emitted in every exception handler in the SSE pump.

```json
{ "error_type": "ConnectTimeout", "status_code": null }
```

| Field | Type | Description |
|---|---|---|
| `error_type` | string | Exception class name as `type(exc).__name__` (e.g. `ConnectTimeout`, `ReadTimeout`, `ConnectError`, `RemoteProtocolError`, `Exception`), or `"upstream_non_200"` for a non-200 upstream response. |
| `status_code` | int or null | HTTP status code if applicable; null otherwise. |

When `upstream_error` is emitted, no `stream_complete`, no `conversation_capture`, and no memory ingest follow for this `request_id`. The chain for a failed call is: `request_received` → `upstream_call` → `upstream_error`.

#### `stream_complete`

Emitted after the SSE pump finishes. Two outcomes: clean success, or mid-stream client disconnect.

```json
{ "bytes_relayed": 8421, "latency_ms": 1735, "outcome": "success" }
```

| Field | Type | Description |
|---|---|---|
| `bytes_relayed` | int | Total bytes pumped from infra to the frontend. |
| `latency_ms` | int | Milliseconds from `/chat` ingress to emission. |
| `outcome` | string | `"success"` (clean completion through `[DONE]`) or `"client_disconnect"`. |

For `outcome=client_disconnect`, no `conversation_capture` and no memory ingest follow (the captured response would be partial).

#### `conversation_capture`

Emitted only after a `stream_complete` with `outcome=success`. The conversation snapshot for this turn, stored for operational record-keeping and offline integrity verification.

```json
{
  "input_text": "The current user message.",
  "output_text": "The visible answer, concatenated from delta.content tokens.",
  "input_hash": "<sha256 hex of input_text>",
  "output_hash": "<sha256 hex of output_text>",
  "model_used": "base-model",
  "reasoning_effort": "medium",
  "latency_ms": 1735,
  "input_tokens": null,
  "output_tokens": null
}
```

| Field | Type | Description |
|---|---|---|
| `input_text` | string | The current (last) user message from the inbound body — the fresh user input for this turn. Earlier turns are NOT concatenated; the gateway snapshots only the latest user message. This is the SAME value used as the `openagent-memory` retrieve query and, when memory is enabled, the user-turn ingest content. System and assistant messages are excluded. |
| `output_text` | string | The visible answer, accumulated from `choices[0].delta.content` tokens via the side-channel parser. The reasoning chain (`delta.reasoning`) is NOT included — see Design Decisions. |
| `input_hash` | string (sha256 hex) | `hashlib.sha256(input_text.encode("utf-8")).hexdigest()`. Lets consumers detect duplicates without storing the text twice. |
| `output_hash` | string (sha256 hex) | Same algorithm applied to `output_text`. |
| `model_used` | string | The model identifier the provider returned (from `choices[0].model` in the stream). |
| `reasoning_effort` | string | The effective value for this call (the frontend's value verbatim, or `null`/omitted to defer to the server-side default). |
| `latency_ms` | int | Same as in `stream_complete` for the same `request_id`. |
| `input_tokens` | int or null | From `usage.prompt_tokens` if the stream provides a final usage chunk; null otherwise. |
| `output_tokens` | int or null | From `usage.completion_tokens` if available; null otherwise. |

#### `memory_retrieve_degraded` (memory enabled only)

Emitted on the hot path, before `upstream_call`, when a `/retrieve` call fails open (memory unreachable, non-200, unparseable, timed out, or `degraded:true`). At most once per `/chat`. The prompt proceeds with "recent turns only".

```json
{ "reason": "memory_unavailable_or_embedder_cold" }
```

| Field | Type | Description |
|---|---|---|
| `reason` | string | Why retrieval degraded. Currently a single coarse value, `"memory_unavailable_or_embedder_cold"`. |

Emitted with `outcome="degraded"`.

#### `memory_ingest_error` (memory enabled only)

Emitted after `conversation_capture`, when a background ingest of a turn fails. At most twice per `/chat` (once per turn). Never blocks or fails `/chat`.

```json
{ "turn": "assistant", "error_type": "MemoryIngestError", "status_code": 503 }
```

| Field | Type | Description |
|---|---|---|
| `turn` | string | Which turn's ingest failed: `"user"` or `"assistant"`. |
| `error_type` | string | Exception class name (e.g. `MemoryIngestError`, `ReadTimeout`, `ConnectError`). |
| `status_code` | int or null | HTTP status if the failure was a non-201 response (e.g. `503` for a down embedder); `null` for transport-level failures. |

Emitted with `outcome="failure"`.

### Canonical string and HMAC signature

The `hmac_signature` in every envelope is computed as follows, implemented byte-for-byte in `src/client/logger.py` and reversed on ingress by `openagent-logger`.

**Step 1 — Canonical payload JSON.**

```python
canonical_payload_json = json.dumps(
    payload,
    sort_keys=True,
    separators=(",", ":"),
    default=str,
    ensure_ascii=False,
)
```

The four `json.dumps` keyword arguments are non-negotiable; changing any breaks the signature on both sides:

| kwarg | Value | Why |
|---|---|---|
| `sort_keys` | `True` | Key ordering must be byte-stable so two services serializing the same dict produce identical bytes. |
| `separators` | `(",", ":")` | Removes ALL whitespace between fields, so re-serialization can't introduce nondeterminism. |
| `default` | `str` | Safety net for non-JSON-serialisable values. |
| `ensure_ascii` | `False` | Allows Unicode through unencoded; both sides encode UTF-8 before hashing. |

**Step 2 — Payload hash.**

```python
payload_hash = hashlib.sha256(canonical_payload_json.encode("utf-8")).hexdigest()
```

**Step 3 — Canonical string.**

```python
canonical_string = f"{request_id}|{client_timestamp}|{event_type}|{payload_hash}"
```

Four pipe-separated components in this exact order: `request_id`, `client_timestamp`, `event_type`, `payload_hash`. Pipe `|` (U+007C) is the separator, no surrounding whitespace.

**Step 4 — HMAC.**

```python
hmac_signature = hmac.new(
    LOGGER_HMAC_SECRET.encode("utf-8"),
    canonical_string.encode("utf-8"),
    hashlib.sha256,
).hexdigest()
```

`openagent-logger` reverses this exact computation on ingress and compares (constant-time, via `hmac.compare_digest`) against the supplied `hmac_signature`. Mismatch returns HTTP 401 (`Invalid HMAC signature`) and the event is rejected.

**Verification property.** Because the signature is computed over (request_id, client_timestamp, event_type, sha256(canonical_payload_json)) — NOT the wire envelope — it survives lossless re-encoding. An auditor with the stored payload and `LOGGER_HMAC_SECRET` can re-verify any stored event offline, without trusting any transport claim. This is why `LOGGER_HMAC_SECRET` exists as a separate secret from `LOGGER_API_KEY` (transport) — see Security Model. (Note: this integrity guarantee applies to the **logger** boundary only; the memory boundary is transport-key only today and carries no payload signature.)

### Queue and background task

The `LoggerClient` in `src/client/logger.py` holds three pieces of runtime state:

1. **`_http_client`** — `httpx.AsyncClient` configured with `base_url=LOGGER_URL`, `headers={"X-API-Key": LOGGER_API_KEY}`, and short timeouts (5s connect, 10s read — fire-and-forget should fail fast). A SEPARATE instance from the `InfraClient`'s and `MemoryClient`'s internal httpx clients; the boundaries have different headers, base URLs, and timeout characteristics.
2. **`_queue`** — `asyncio.Queue` with capacity `OPENAGENT_LOGGER_QUEUE_MAX_SIZE` (env var, default 1000).
3. **`_drain_task`** — `asyncio.Task` running `_drain_loop()` until cancelled.

**Startup** (FastAPI lifespan): `LoggerClient(...)` is constructed and `await logger_client.start()` creates `_http_client` and launches `_drain_task`. No connectivity probe — `openagent-api` boots even if the logger is unreachable.

**Runtime** (during `/chat`): emit methods are synchronous, do no I/O, return in microseconds. Each emit call builds the envelope, computes the signature, and `put_nowait`s onto the queue. If the queue is full, it catches `asyncio.QueueFull`, pops the oldest event, logs a WARNING, calls `task_done()` to keep accounting clean, then enqueues the new envelope.

**Drain task** (`_drain_loop`): pulls envelopes, POSTs to `{LOGGER_URL}/events`. On non-2xx or network error, logs a single WARNING and returns — no retry, no re-enqueue. The drained event is gone.

**Shutdown** (FastAPI lifespan): the clients stop in the order **memory → logger → infra**. `await logger_client.stop()` runs *after* the MemoryClient has drained its in-flight ingests (so any `memory_ingest_error` events emitted during that drain still land on a live logger queue) and *before* the InfraClient stops. It waits up to 5 seconds for `_queue.join()`, then cancels `_drain_task` and closes `_http_client`. This gives any events still in the queue one last chance to land.

### Failure modes

| Failure | Detection | openagent-api behavior | Operator observability |
|---|---|---|---|
| logger unreachable (TCP/DNS fail) | `httpx.ConnectError` | Drop event, log WARNING | `WARNING | LoggerClient POST failed: ConnectError to {url}` |
| logger slow (timeout) | `httpx.ReadTimeout` | Drop event, log WARNING | `WARNING | LoggerClient POST failed: ReadTimeout to {url}` |
| logger 401 (transport key mismatch) | Non-2xx, "Invalid or missing API key" | Drop event, log WARNING | fix by aligning `LOGGER_API_KEY` in both `.env` files |
| logger 401 (HMAC mismatch) | Non-2xx, "Invalid HMAC signature" | Drop event, log WARNING | fix by aligning `LOGGER_HMAC_SECRET` in both `.env` files |
| logger 422 (malformed envelope) | Non-2xx | Drop event, log WARNING | should not happen if both sides match the wire contract |
| Queue full (sustained outage) | `asyncio.QueueFull` | Pop oldest, push new, log WARNING | `WARNING | LoggerClient queue full (max=N); dropped oldest event` |

Throughout all failure modes, `/chat` is completely unaffected. The frontend cannot tell whether the logger is up. That is the design.

### What this section does NOT cover

- **PII stripping**: `openagent-api` does none. `input_text`/`output_text` are stored raw.
- **User-id population**: always `null`; no user tracking in the current stack. (`session_id` IS now populated from `MEMORY_SESSION_ID`.)
- **Reasoning chain capture**: `output_text` is `delta.content` only; `delta.reasoning` is not captured.
- **Durable outbox / retry**: events lost during a logger outage are lost permanently.
- **The memory boundary**: `/retrieve` and `/ingest` are documented under [Outbound HTTP Contracts](#outbound-http-contracts-consumed) and §10, not here — they are not logger emissions.

---

## State Model

### Per-process state (populated at startup)

| Symbol | Type | Lifetime | Purpose |
|--------|------|----------|---------|
| `identity` | `str` | Process | `bio.txt` content, prepended as the system message on every `/chat`. |
| `infra_client` | `InfraClient` | Process | Encapsulates the outbound boundary to `openagent-infra`. Owns an internal `httpx.AsyncClient` with `base_url=OPENAGENT_INFRA_URL`, `X-API-Key: INFRA_API_KEY` pre-attached, connect 10s, read unbounded by default, write 10s, pool 5s. Exposes `start()`, `stop()`, `aclose()`, `stream_chat(payload)` (returns httpx's streaming context manager so `sse_pump`'s `async with` is unchanged), and `check_health()` (returns `Tuple[str, Optional[Dict]]`, never raises). Initialized at lifespan startup, stopped LAST at shutdown. |
| `logger_client` | `LoggerClient` | Process | Fire-and-forget emitter for `openagent-logger`. Holds a SEPARATE `httpx.AsyncClient` with `X-API-Key: LOGGER_API_KEY` pre-attached, `base_url=LOGGER_URL`, connect 5s, read 10s. Initialized at lifespan startup, stopped SECOND at shutdown. |
| `logger_client._queue` + `logger_client._drain_task` | `asyncio.Queue` + `asyncio.Task` | Process (internal) | In-memory pending-event buffer + background drain task. Capacity `OPENAGENT_LOGGER_QUEUE_MAX_SIZE` (default 1000), drop-oldest overflow. Drain task POSTs to the logger; failures logged as WARNING and event dropped with no retry. |
| `memory_client` | `MemoryClient` or `None` | Process (when memory enabled) | Encapsulates the optional outbound boundary to `openagent-memory`. `None` unless `MEMORY_URL` + `MEMORY_API_KEY` are both set. Owns its own `httpx.AsyncClient` with `base_url=MEMORY_URL`, `X-API-Key: MEMORY_API_KEY` pre-attached, connect 5s, retrieve read = `MEMORY_RETRIEVE_TIMEOUT` (default 5s), ingest read 15s. Exposes `start()`, `stop()`, `aclose()`, `retrieve()` (fail-open, returns `Tuple[List, bool]`, never raises), and `ingest_turn_pair_background()` (schedules a detached task). Initialized at lifespan startup when enabled, stopped FIRST at shutdown (order: memory → logger → infra). **No HMAC** on this boundary today. |
| `memory_client._inflight` | `set[asyncio.Task]` | Process (internal, when enabled) | In-flight background ingest tasks, held so `create_task` results are not garbage-collected mid-flight. Drained (with a timeout) then cancelled at shutdown. |

### Per-request state

None retained. The gateway is **stateless across requests**. Each `/chat` generates a fresh `request_id` (UUID4) to thread the emitted events together, and reads `session_id` from the static `MEMORY_SESSION_ID` env var (null when unset); neither is persisted in `openagent-api` — they live only on the events the logger receives and (for `session_id`) as the scope on memory retrieve/ingest.

### Persistent state

**None in openagent-api.** `bio.txt` is read-only and baked into the image. Restarting the container loses no data because there is none in the gateway — including any pending events in the LoggerClient queue and any in-flight memory ingests, which are lost on restart. When memory is enabled, the durable conversation store lives entirely in **openagent-memory**'s own database, not here. This in-process volatility is an acknowledged limitation.

---

## Configuration

All runtime configuration is loaded from `.env` at the repository root via `python-dotenv` and `docker-compose`'s `env_file:` directive.

### Required

| Variable             | Type   | Description                                                              |
|----------------------|--------|--------------------------------------------------------------------------|
| `OPENAGENT_API_KEY`  | string | Inbound secret. openagent-api refuses to start if unset.                 |
| `INFRA_API_KEY`      | string | Outbound secret to openagent-infra. Refuses to start if unset. Must match openagent-infra's `API_KEY` byte-for-byte. |
| `OPENAGENT_INFRA_URL`| string | Base URL of openagent-infra. No trailing slash.                          |
| `LOGGER_URL`         | string | Base URL of openagent-logger. No trailing slash. Refuses to start if unset. |
| `LOGGER_API_KEY`     | string | Outbound transport secret to openagent-logger. Must match its `LOGGER_API_KEY` byte-for-byte. |
| `LOGGER_HMAC_SECRET` | string | Outbound payload-signing secret for logger events. Must match its `LOGGER_HMAC_SECRET` byte-for-byte. |

### Optional

| Variable                              | Default                       | Description                                            |
|---------------------------------------|-------------------------------|--------------------------------------------------------|
| `OPENAGENT_FRONTEND_URL`              | —                             | Extra CORS origin for production deployments.          |
| `OPENAGENT_LOG_LEVEL`                 | `INFO`                        | DEBUG \| INFO \| WARNING \| ERROR \| CRITICAL          |
| `OPENAGENT_UPSTREAM_CONNECT_TIMEOUT`  | `10.0`                        | Seconds to wait for TCP connect to openagent-infra.    |
| `OPENAGENT_UPSTREAM_READ_TIMEOUT`     | `none`                        | Seconds (or `none` for unbounded) for streaming reads. |
| `OPENAGENT_HEALTH_TIMEOUT`            | `5.0`                         | Seconds for upstream /health probe.                    |
| `OPENAGENT_BIO_PATH`                  | `/app/src/prompt/bio.txt`     | Override path to bio.txt inside the container.         |
| `OPENAGENT_LOGGER_QUEUE_MAX_SIZE`     | `1000`                        | Max pending events in LoggerClient queue before drop-oldest. |

### Optional — openagent-memory (opt-in)

Memory is enabled only when **both** `MEMORY_URL` and `MEMORY_API_KEY` are set. Leave them unset to run without memory (full history forwarded).

| Variable                  | Default              | Description                                                          |
|---------------------------|----------------------|---------------------------------------------------------------------|
| `MEMORY_URL`              | — (unset = disabled) | Base URL of openagent-memory. No trailing slash. Enables memory together with `MEMORY_API_KEY`. |
| `MEMORY_API_KEY`          | — (unset = disabled) | Outbound transport secret to openagent-memory (no HMAC on this boundary). Must match openagent-memory's `MEMORY_API_KEY` byte-for-byte. |
| `MEMORY_SESSION_ID`       | — (empty = inactive) | Static session scope; also populates `session_id` on every emitted event. If empty while memory is enabled, retrieve/ingest stay inactive (startup warning) and full history is forwarded. |
| `MEMORY_RECENT_N`         | `10`                 | Most-recent messages kept verbatim in the assembled prompt, ahead of the current turn (counted in messages — each user or assistant turn = 1). Retrieved older turns are deduped against this window by SHA-256 content hash. |
| `MEMORY_TOP_K`            | — (memory's own, 5)  | Cap on retrieved turns per `/retrieve`. Unset → the `top_k` field is omitted and openagent-memory applies its own default. |
| `MEMORY_RETRIEVE_TIMEOUT` | `5.0`                | Seconds to wait for `/retrieve` (hot path, fail-open). Short so a cold embedder fails open fast. |

**Intentionally absent:** `OPENAGENT_DEFAULT_REASONING_EFFORT` (`openagent-api` is pure pass-through; `openagent-infra`'s `REASONING_EFFORT` is the single source of truth). Also `MEMORY_HMAC_SECRET` — the memory boundary has no HMAC today (transport-key auth only).

### `OPENAGENT_INFRA_URL` / `LOGGER_URL` / `MEMORY_URL` by deployment topology

| Scenario | `OPENAGENT_INFRA_URL` | `LOGGER_URL` | `MEMORY_URL` (optional) |
|---|---|---|---|
| Everything on host, no Docker | `http://localhost:8002` | `http://localhost:8003` | `http://localhost:8004` |
| openagent-api in Docker, upstreams on host | `http://host.docker.internal:8002` | `http://host.docker.internal:8003` | `http://host.docker.internal:8004` |
| All in Docker on a shared external network | `http://openagent-infra:8002` | `http://openagent-logger:8003` | `http://openagent-memory:8004` |
| External deployment | `https://infra.your-domain.com` | `https://logger.your-domain.com` | `https://memory.your-domain.com` |

All services attach to the shared `openagent-network` (created and owned by `openagent-logger`) in the shared-network topology, so container-name addressing Just Works.

---

## Security Model

This section documents the system-wide security posture for `openagent-api` and its position within the larger OpenAgent credential architecture. It complements the per-key descriptions in the ownership sections above; those describe each key in isolation, this describes how they fit together.

### Pattern: compartmentalization (least privilege for credentials)

The OpenAgent system uses **compartmentalization** (least privilege applied to credentials) for all service-to-service authentication. Each service holds only the secrets for the boundaries it directly touches; no key is forwarded or relayed unchanged through the chain. This is the standard pattern for multi-service architectures and is what bounds the blast radius of any single-service compromise.

The contrast — a **bearer token relay** / **shared-secret architecture** — uses one secret end-to-end, forwarded by each service to the next. That pattern is operationally simpler but catastrophic in compromise, because stealing the key from any one service grants the same access as stealing it from all of them. `openagent-frontend`, `openagent-api`, `openagent-infra`, `openagent-logger`, and `openagent-memory` all reject this pattern.

The logger boundary additionally rejects a weaker variant — **transport-only auth** (`X-API-Key` alone). Transport keys gate the wire boundary but provide no integrity guarantee for stored data. The HMAC signature stored on every row solves this: anyone with `LOGGER_HMAC_SECRET` can verify event integrity offline. That property survives forever; transport keys can rotate without invalidating it.

**The memory boundary is transport-only today** (`X-API-Key: MEMORY_API_KEY`, no HMAC) — the known exception to the above. This is an accepted current-state gap, not a design endorsement of transport-only auth: `openagent-memory` does not yet define an HMAC contract, and the `MemoryClient` scaffolds the signing helpers so adding payload integrity later is a localized change. Until then, the memory boundary has the weaker guarantee (wire access gated, no offline payload-integrity proof for stored turns).

### Per-service key inventory

| Service | Holds | Does not hold |
|---|---|---|
| `openagent-frontend` | `OPENAGENT_API_KEY` (outbound to openagent-api) | `INFRA_API_KEY`, `LOGGER_API_KEY`, `LOGGER_HMAC_SECRET`, `MEMORY_API_KEY`, `PROVIDER_API_KEY` |
| `openagent-api` | `OPENAGENT_API_KEY` (inbound), `INFRA_API_KEY` (outbound to infra), `LOGGER_API_KEY` (outbound transport to logger), `LOGGER_HMAC_SECRET` (outbound payload signing), `MEMORY_API_KEY` (outbound transport to memory, when enabled) | `PROVIDER_API_KEY` |
| `openagent-infra` | `INFRA_API_KEY` (inbound, named `API_KEY` on its side), `PROVIDER_API_KEY` (outbound to provider) | `OPENAGENT_API_KEY`, `LOGGER_API_KEY`, `LOGGER_HMAC_SECRET`, `MEMORY_API_KEY` |
| `openagent-logger` | `LOGGER_API_KEY` (inbound transport), `LOGGER_HMAC_SECRET` (inbound verification + stored on every row) | `OPENAGENT_API_KEY`, `INFRA_API_KEY`, `MEMORY_API_KEY`, `PROVIDER_API_KEY` |
| `openagent-memory` (optional) | `MEMORY_API_KEY` (inbound transport) | `OPENAGENT_API_KEY`, `INFRA_API_KEY`, `LOGGER_API_KEY`, `LOGGER_HMAC_SECRET`, `PROVIDER_API_KEY` |
| BYOC provider | `PROVIDER_API_KEY` (inbound) | None of the others |

The keys are independent values, not derivations of one master secret. `openagent-api` is the only service holding two secrets for the same boundary — `LOGGER_API_KEY` and `LOGGER_HMAC_SECRET` both protect the api ↔ logger boundary, with different rotation profiles documented below.

### Key generation

```bash
# OPENAGENT_API_KEY and INFRA_API_KEY (256 bits, hex)
python -c "import secrets; print(secrets.token_hex(32))"

# LOGGER_API_KEY, LOGGER_HMAC_SECRET, and MEMORY_API_KEY (URL-safe base64; the
# logger's and memory's convention)
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Each key is generated independently. Do not reuse one key as another, even temporarily. Do not use UUIDs, timestamps, or hand-typed strings. For `LOGGER_API_KEY`, `LOGGER_HMAC_SECRET`, and `MEMORY_API_KEY`, the service on the OTHER side of the boundary is the source of truth — copy the value from its `.env` rather than minting a new one. Placeholder values are acceptable only for "is the wiring connected" smoke tests against an isolated localhost stack; rotate to real values before the stack reaches any host another person could access, before any billable provider spend is incurred, and before any external party has visibility into the codebase or environment.

### Blast-radius analysis

| Compromised service | Keys exposed | Boundaries reachable | Boundaries protected |
|---|---|---|---|
| `openagent-frontend` | `OPENAGENT_API_KEY` | frontend → openagent-api | api → infra; api → logger; api → memory; infra → provider |
| `openagent-api` | `OPENAGENT_API_KEY`, `INFRA_API_KEY`, `LOGGER_API_KEY`, `LOGGER_HMAC_SECRET`, `MEMORY_API_KEY` (if memory enabled) | frontend → api; api → infra; api → logger (incl. forging events with valid HMAC); api → memory | infra → provider. Pre-compromise rows in the logger stay HMAC-verifiable as long as `LOGGER_HMAC_SECRET` is rotated post-compromise. |
| `openagent-infra` | `INFRA_API_KEY`, `PROVIDER_API_KEY` | api → infra; infra → provider | frontend → api; api → logger; api → memory. Capture layer keeps recording; infra's compromise becomes visible in the logger's `upstream_error` events. |
| `openagent-logger` | `LOGGER_API_KEY`, `LOGGER_HMAC_SECRET` (and DB access if the host itself is compromised) | reads captured `conversation_captures` (raw input/output text); could submit forged events the logger would accept | frontend → api; api → infra; api → memory; infra → provider. Compromise bounded to the capture layer. |
| `openagent-memory` (optional) | `MEMORY_API_KEY` (and its DB if the host is compromised) | reads/writes the stored conversation turns in memory's own database (raw user/assistant content) | frontend → api; api → infra; api → logger; infra → provider. Compromise bounded to the memory layer. Note memory's DB holds conversation content at rest — treat it with the same care as the logger's captures. |
| BYOC provider | `PROVIDER_API_KEY` | (provider's internal exposure) | All OpenAgent boundaries |

In every scenario, compromise stops at the layer below the compromised service. There is no single key whose theft compromises the entire chain.

### Service-to-service vs user-to-service auth

All keys above authenticate **services to services**, not users to services. There is no concept of user identity at the `openagent-api` layer — `OPENAGENT_API_KEY` is a shared secret between the frontend and the gateway, `MEMORY_SESSION_ID` is a single static session scope (not a per-user credential), and any client holding `OPENAGENT_API_KEY` gets full access. This is a deliberate scoping decision for a reference implementation. The practical implication: anyone with deployment access to `openagent-frontend` (or who can read its `.env`) can use the system fully. Acceptable for solo development and trusted internal use; not acceptable for public exposure without an auth layer in front.

### Key rotation procedure

Each rotation updates the secret in both services that share the boundary, then restarts both.

**Rotating `OPENAGENT_API_KEY`** (frontend ↔ api): generate a new value, update `openagent-api/.env` and `openagent-frontend/.env` with the same value, restart both containers, verify with `curl -H "X-API-Key: <new>" http://localhost:8001/health` → `{"status":"ok",...}`. The old key now returns 401.

**Rotating `INFRA_API_KEY`** (api ↔ infra): generate a new value, update `openagent-api/.env` and `openagent-infra/.env` (as its `API_KEY`) with the same value, restart both, verify `openagent-api`'s `/health` still reports `openagent_infra.status: ok`. If it becomes `unreachable`, the keys are out of sync.

**Rotating `LOGGER_API_KEY`** (api ↔ logger transport): generate via `token_urlsafe(48)`, update `openagent-logger/.env` and `openagent-api/.env`, restart both, verify by sending a `/chat` and checking `openagent-logger:/stats` row counts increment. **This rotation is cheap** — the transport key only gates wire access; existing rows are unaffected. Rotate freely on a calendar cadence or on exposure.

**Rotating `LOGGER_HMAC_SECRET`** (api ↔ logger signing): generate via `token_urlsafe(48)`, update both `.env`s, restart both, verify the same way. **This rotation is expensive** — the signature is stored on every row, and pre-rotation signatures cannot be re-verified with the new secret. Rotate only on known compromise; if you must, document the cutover timestamp so anyone re-verifying knows which secret applies to which rows.

**Rotating `MEMORY_API_KEY`** (api ↔ memory transport): generate via `token_urlsafe(48)`, update `openagent-memory/.env` and `openagent-api/.env`, restart both, verify by sending a `/chat` and confirming retrieve/ingest succeed (no `memory_retrieve_degraded` / `memory_ingest_error` from auth). **This rotation is cheap** — like `LOGGER_API_KEY`, it only gates future `/retrieve` and `/ingest` calls; stored turns are unaffected. There is no HMAC secret on the memory boundary to rotate today.

**Rotating `PROVIDER_API_KEY`** (infra ↔ provider): owned by `openagent-infra`; `openagent-api` is not involved.

### When to rotate

Suspected exposure (a `.env` committed, copied, or shared insecurely): rotate immediately. Device compromise (a machine that held keys is lost or stolen): rotate every key it held. Environment promotion (dev → staging → prod): always rotate; never let one secret span environments. Calendar cadence: routine rotation as a precaution. `LOGGER_HMAC_SECRET` is the exception — rotate only on known compromise, never routinely.

### Storage tiers

A general hierarchy of secret-storage practice, with where this reference sits and where a production deployment should move:

- **`.env` files on disk, gitignored** — current state, appropriate for local development. Risk: accidental commit or copy. Mitigation: `.gitignore` excludes `.env`; a pre-commit hook blocking `.env*` is a good addition.
- **Container orchestration secrets** — appropriate before any remote deployment. Docker Compose `secrets:`, Docker Swarm `docker secret`, Kubernetes `Secret` with encryption-at-rest. Scoped to specific services rather than visible to anyone who can read the host filesystem.
- **Dedicated secrets manager** — appropriate for any serious production use. AWS Secrets Manager, HashiCorp Vault, GCP Secret Manager, Azure Key Vault, Doppler. Secrets fetched at startup using the service's own identity; never on disk. Adds audit logging and rotation primitives natively.
- **Workload identity / mTLS** — no shared secrets at all; mutual TLS with per-service certificates or SPIFFE/SPIRE-style attestation.

Two non-negotiables regardless of tier: `.env` is never committed; placeholder values never reach a non-localhost stack.

### Transit security

- **TLS for any non-localhost transit.** Transport keys travel in HTTP headers. On `localhost`/`host.docker.internal`, traffic doesn't leave the host and plaintext HTTP is acceptable. Any deployment where a service runs on a separate host requires TLS end-to-end across all boundaries (frontend, infra, logger, and memory). Terminate TLS at a public-facing reverse proxy; use mTLS or VPC-internal traffic between backend services. HMAC signatures (logger boundary) travel in the JSON body and add a payload-integrity layer on top of TLS — complementary, not a substitute; the memory boundary has no such layer today, so TLS is its only integrity protection in transit.
- **Headers, never URLs.** Transport keys are sent as `X-API-Key` headers (or `Authorization: Bearer` for the provider). Never query strings — those are logged by web servers, proxies, browsers, and bug trackers.
- **Never echoed.** `openagent-api` does not log `INFRA_API_KEY`, `LOGGER_API_KEY`, `LOGGER_HMAC_SECRET`, or `MEMORY_API_KEY`, and does not include them in error or `/health` responses.

### Frontend-supplied system-message rejection (defense-in-depth)

A second defense layer separate from the auth chain: `openagent-api` drops any `"system"`-role messages in the `/chat` body, regardless of authentication. The canonical persona is `bio.txt` and only `bio.txt`. This protects against a tampered or out-of-date frontend that holds a valid `OPENAGENT_API_KEY` but attempts to override the identity. Auth alone cannot prevent prompt-override attacks; message-shape filtering does.

---

## Container / Deployment

### Image

- **Base:** `python:3.11-slim`
- **Tag:** `openagent-api:1.0.0`
- **Container name:** `openagent-api`
- **Size (approximate):** ~150–200 MB (pure Python, no CUDA, no BLAS)
- **User:** non-root (`openagent`, uid 1000)

### Build

```bash
# From repo root
docker-compose up -d --build
```

The `--build` flag is **required** when `src/backend/api.py`, `src/client/infra.py`, `src/client/logger.py`, `src/client/memory.py`, `src/prompt/bio.txt`, `requirements.txt`, or the Dockerfile change. (The single `COPY src/client/` directive picks up `memory.py` automatically — no Dockerfile change was needed to add it.)

### Port mapping

- **Host port 8001 → Container port 8001**
- uvicorn binds to `0.0.0.0:8001` inside the container (per the Dockerfile CMD)

### Volumes

None by default. The container is stateless; `bio.txt` is baked in at build time. For development, optionally mount `./src/prompt:/app/src/prompt:ro` to live-edit the persona without rebuilding.

### Restart policy

`unless-stopped`.

### Healthcheck

Defined in the Dockerfile via `HEALTHCHECK`. Probes `/health` every 30 seconds with the `X-API-Key` header from `OPENAGENT_API_KEY`. Marks the container unhealthy after three consecutive failures. The healthcheck does NOT validate the logger or memory boundaries — both are non-fatal (logger is fire-and-forget; memory fails open) and including either would couple `openagent-api`'s reported health to a non-essential dependency.

---

## File Structure

```text
openagent-api/
├── docker/
│   └── api/
│       └── Dockerfile              # Python 3.11 slim + non-root + healthcheck
├── src/
│   ├── backend/
│   │   └── api.py                  # The FastAPI app (single file)
│   ├── client/
│   │   ├── __init__.py             # Package marker / client enumeration
│   │   ├── infra.py                # InfraClient: streaming /chat + /health proxy to openagent-infra
│   │   ├── logger.py               # LoggerClient: fire-and-forget emitter to openagent-logger
│   │   └── memory.py               # MemoryClient: retrieve/ingest to openagent-memory (optional)
│   └── prompt/
│       └── bio.txt                 # Persona (baked into image)
├── docs/
│   └── DATASHEET.md                # This document
├── docker-compose.yml              # Single-service compose
├── requirements.txt                # fastapi, uvicorn, httpx, python-dotenv
├── .env                            # secrets — never committed
├── .env.example                    # template for .env
├── .dockerignore
├── .gitignore
└── README.md
```

The runtime is four Python files (`backend/api.py` + `client/infra.py` + `client/logger.py` + `client/memory.py`) plus the persona text and the deployment shell. Dependency footprint is deliberately tiny: **fastapi, uvicorn, httpx, python-dotenv** and nothing else. `InfraClient`, `LoggerClient`, and `MemoryClient` all use only the stdlib (`hmac`, `hashlib`, `json`, `asyncio`) plus the already-present `httpx` — no extra pip packages; the memory client added zero dependencies. The dependency direction is explicit: backend depends on client; client never depends on backend.

---

## Integration Notes for Other Services

### For openagent-frontend

`openagent-frontend` is the primary client. It:

- Points its backend URL config at `OPENAGENT_API_URL`.
- Carries no persona; sends only user/assistant turns (no `system` message).
- Optionally includes `reasoning_effort` on the request body (or omits it to let `openagent-infra` default), and can expose it as a UI toggle (Quick / Standard / Deep).
- Holds `OPENAGENT_API_KEY` and sends it as `X-API-Key` on `/chat` and `/health`.
- Polls `/health` to gate the chat input; the top-level `status` vocabulary (`ok` / `loading` / `unreachable`) is what its gate-open logic reads.
- Decodes each SSE event as a JSON ChatCompletion chunk, routing `choices[0].delta.reasoning` to the reasoning surface and `choices[0].delta.content` to the chat bubble; the stream terminates with an empty-delta `finish_reason: "stop"` chunk followed by `data: [DONE]`.
- Does NOT send a `session_id` yet — it comes from `MEMORY_SESSION_ID` server-side. When the frontend gains session management, the id moves to the request and `ChatRequest` gains the field.

### For openagent-infra

`openagent-api` is the only client of `openagent-infra`. It opens a streaming POST to `OPENAGENT_INFRA_URL/chat` with `X-API-Key: INFRA_API_KEY`, sending the **assembled** message list (bio prepended; retrieved + recent-N + current when memory is on). It never sets the `model` field, so every call routes to the base model. It proxies `openagent-infra`'s `/health` and translates `degraded` → `loading` for the frontend.

### For openagent-logger

`openagent-api` emits the core five event types per `/chat` call (plus the two memory-conditional types when memory is enabled) via the in-process `LoggerClient`, each HMAC-signed and stored with its signature for offline re-verification. `openagent-logger` owns event storage, partitioning, retention, replay-window enforcement, and the `/stats` and `/health` endpoints. `openagent-api` owns event GENERATION and the wire-side signing. The two MUST agree byte-for-byte on the envelope shape, payload schemas, and canonical-string protocol; a change on either side requires a coordinated release. The contract is fire-and-forget — no client-side retry, no backoff, no dead-letter queue — so a logger outage causes WARNING log lines but no `/chat` impact. Note the `session_id` field is now populated from `MEMORY_SESSION_ID` (previously always null); the logger already accepted it and need not change.

### For openagent-memory (optional)

When enabled, `openagent-api` is the only caller of `openagent-memory`. It calls `POST /retrieve` on the hot path (fail-open, bounded by `MEMORY_RETRIEVE_TIMEOUT`) and `POST /ingest` twice off-path after a successful stream, both with `X-API-Key: MEMORY_API_KEY` (transport-key only — no HMAC today). `openagent-api` owns prompt assembly; `openagent-memory` owns embedding, vector search, its own dedupe/storage, and its own `/health`. The two must agree on the `/retrieve` and `/ingest` request/response shapes; `openagent-memory` is the source of truth for `MEMORY_API_KEY`. `openagent-memory` may answer `degraded:true` on `/retrieve` (embedder cold) or `503` on `/ingest` (embedder down) — `openagent-api` handles both gracefully (degrade to recent-only; emit `memory_ingest_error`) and never lets either affect `/chat`. Memory is opt-in; with no configuration `openagent-api` does not call it at all.

---

## Design Decisions

### Why is the persona owned by openagent-api, not the frontend?

Identity is product-layer **backend** logic. The frontend is replaceable; the identity is not. Keeping `bio.txt` server-side means any client (mobile, CLI, alternate UI) shares one canonical agent, and a tampered or out-of-date frontend cannot override the persona.

### Why independent secrets at each boundary instead of one shared secret?

Isolation of compromise. With one shared key end-to-end, compromise of any service compromises all of them. With independent secrets at each boundary, each compromise is isolated to its layer (see the blast-radius table). All keys have 256+ bits of entropy; none is brute-forceable individually. The benefit is structural, not entropy.

### Why two secrets for openagent-logger (LOGGER_API_KEY + LOGGER_HMAC_SECRET)?

Different threat models and rotation profiles. `LOGGER_API_KEY` (transport) gates wire access; rotation is cheap (existing rows unaffected). `LOGGER_HMAC_SECRET` (payload signing) provides integrity — the stored signature lets a consumer verify any row offline without trusting the original transport; rotation is expensive (pre-rotation signatures can't be re-verified). Splitting them lets you rotate the transport key on a cadence without disturbing the integrity guarantee on stored data.

### Why is openagent-memory opt-in rather than required?

Memory is an enhancement, not a dependency of the core gateway. The gateway must keep serving `/chat` whether or not a RAG layer exists, so memory is enabled only by explicit configuration (`MEMORY_URL` + `MEMORY_API_KEY`) and is never a refuse-to-boot dependency. Deployments that don't want it pay nothing — full history is forwarded as before. This mirrors the discipline already applied to the logger boundary (non-essential to the response), taken one step further: memory is not even required to be configured.

### Why is retrieve fail-open but ingest is not?

They sit on different paths. Retrieve is on the hot path, so a slow or down memory must degrade the prompt to "recent turns only" rather than delay the user's first token — fail-open is the only acceptable behaviour there, bounded by `MEMORY_RETRIEVE_TIMEOUT`. Ingest runs after the user already has their answer, so it can afford to surface a real failure signal (`openagent-memory` returns 503 when its embedder is down) so a dropped turn is visible rather than silent — but because it's off-path, surfacing that signal (as a `memory_ingest_error` event) never costs the user anything.

### Why does prompt assembly live in openagent-api and not openagent-memory?

Separation of concerns. Memory's job is to embed a query and rank prior turns; the gateway's job is to decide what actually goes into the prompt — the persona, which retrieved turns to keep (deduped against the recent window by content hash), how many recent turns to keep verbatim, and the ordering. Keeping assembly in the gateway means `openagent-memory` can be swapped or reimplemented without touching prompt policy, and the gateway stays the single owner of the upstream payload shape.

### Why no HMAC on the memory boundary (yet)?

`openagent-memory` uses transport-key auth only today and defines no HMAC contract. Rather than invent one prematurely (and risk it disagreeing with memory's eventual verifier), the `MemoryClient` scaffolds the signing helpers — mirroring the logger's canonical-payload contract — but leaves them inert. Adding HMAC later, once `openagent-memory` defines the verifier, is then a localized change: wire a secret through the client and flip the inert `_sign` to active. The accepted current-state cost is that the memory boundary has wire-access control but no offline payload-integrity proof for stored turns.

### Why is `session_id` an env var for now?

The reference stack doesn't yet have a frontend that mints and manages per-conversation ids. `MEMORY_SESSION_ID` is a deliberate stopgap: a single static value that scopes memory and populates the previously-null `session_id` correlation field on events. When the frontend gains session management, the id moves out of the environment and into the request — `ChatRequest` intentionally has no `session_id` field yet so that move is clean. Note `session_id` threads onto events whenever `MEMORY_SESSION_ID` is set, independent of whether the memory boundary itself is enabled.

### Why fire-and-forget logger emission?

The `/chat` hot path must not block on the capture layer. A synchronous emit-per-event design would add round-trip latency per chat turn AND couple `openagent-api`'s reliability to the logger's. Fire-and-forget makes emit methods synchronous, no-I/O, microsecond-latency; a background task drains in parallel. Logger unavailability is invisible to the frontend. The trade-off is event loss when the logger is down — accepted for a reference implementation.

### Why an in-memory queue instead of a durable outbox?

Zero infrastructure: no new dependencies, no new services, no new failure modes. It composes naturally with the FastAPI lifespan and the existing httpx client. The cost is that events queued in memory at the moment of a container restart are lost (as are any in-flight memory ingests). A durable outbox would survive restarts at the cost of a new dependency, schema, and failure mode; for a reference implementation, in-memory is the right trade.

### Why drop-oldest queue overflow (not drop-newest)?

During a sustained logger outage, fresher data is more useful than stale data when the storm passes. Drop-oldest also pairs cleanly with the logger's 300-second replay window — the oldest events would be the most likely to be rejected as stale on any replay anyway.

### Why no reasoning chain capture?

`conversation_capture.output_text` contains only `delta.content` tokens (the visible answer), not `delta.reasoning` (the chain-of-thought). The reasoning format is model-specific and likely to change; the capture is meant to record user-facing behavior — the answers actually given — not the model's internal reasoning style. This is reversible: if reasoning capture becomes valuable, a new optional `reasoning_text` field can be added in a coordinated api + logger release.

### Why is `reasoning_effort` pure pass-through with no openagent-api default?

The setting has one source of truth: `openagent-infra`'s `REASONING_EFFORT` env var. A second default here would mean two places to check when debugging, and they could disagree.

### Why SSE relay instead of buffering and returning JSON?

Generations take seconds to minutes (plus provider cold-start). Buffering means a multi-minute spinner with no feedback. Streaming is the only viable UX for this latency profile.

### Why forward bytes instead of parse-and-re-emit?

Anything `openagent-api` parses on the relay path, it can break. Forwarding raw bytes means the frontend's parser keeps working unmodified and the relay stays simple. The cost is that mid-stream upstream errors must be surfaced as in-band SSE events rather than HTTP status codes, but the frontend handles that cleanly. The side-channel parser does parse the same stream, but in parallel (downstream of the yield) and only to extract `delta.content`; parse failures there are silently tolerated and never affect the relay.

### Why is `/health` authenticated?

It reveals operational state — whether the upstream is reachable, what version is running, whether the provider worker is warm. That's internal information. Auth adds zero friction for the frontend (it has the key) and keeps random scanners out.

### Why drop frontend-supplied system messages instead of rejecting?

Robust over strict. During a transition where an older frontend may still send a system message, dropping (with a warning log) keeps the request working; the operator sees the warning and updates the frontend.

### Why httpx instead of requests or aiohttp?

`httpx` is async-native (FastAPI's event loop can `await` it), supports streaming exactly as the SSE pump needs (`aiter_raw()`), and has a familiar API. `requests` is sync and would block the event loop. `InfraClient`, `LoggerClient`, and `MemoryClient` all reuse the same `httpx` library, each with a separate `AsyncClient` instance.

### Why three separate httpx.AsyncClient instances?

The infra, logger, and memory boundaries differ on every axis: headers (`INFRA_API_KEY` vs `LOGGER_API_KEY` vs `MEMORY_API_KEY`), base URLs, timeout profiles (infra: 10s connect, unbounded read for cold-start; logger: 5s connect, 10s read for fail-fast; memory: 5s connect, short retrieve read for fail-open + longer ingest read off-path), and connection-pool semantics (long-lived streams vs many short POSTs vs a mix). Sharing one client would force compromises on all of them. Each client lives in its own module under `src/client/`, with explicit dependency direction (backend → client, never the reverse).

### Why a non-root user in the container?

A container escape that gives root inside should not give root outside. The `openagent` user (uid 1000) is created in the Dockerfile and the runtime drops privilege before uvicorn starts.

### Why is the infra read timeout unbounded?

The first request after a serverless worker has scaled to zero waits for the worker to spin up; even on a warm worker, `high` reasoning effort can run for minutes. A finite read timeout would kill long but legitimate generations. Connect timeout stays short (10s) so an unreachable upstream fails fast. The logger boundary uses short timeouts (5s/10s) because fire-and-forget emissions should fail fast; the memory retrieve timeout is short for the same fail-open reason, while its ingest read is longer because it is off-path.

### Why python:3.11-slim?

Matches `openagent-frontend`'s base image for consistency. Pure-Python service, no GPU stack — slim is the right tier.

### Why a single-file FastAPI app (api.py) plus a client package?

The service is one HTTP gateway with two endpoints and three dedicated outbound client wrappers (`InfraClient`, `LoggerClient`, `MemoryClient`) kept separate from `api.py` to delimit concerns cleanly. Splitting `api.py` further into routers/services/schemas folders would be premature abstraction at this size. The `src/client/` layout is the right amount of separation: backend depends on client; client never depends on backend.

### Why port 8001?

Port convention: 8000 = frontend (user-facing), 8001 = api (the API the frontend calls), 8002 = infra (model layer, called only by api), 8003 = logger (capture layer, called only by api), 8004 = memory (RAG layer, called only by api, optional), provider = remote inference (called only by infra). The numbering reflects the request flow.

---

## Known Limitations

### Stateless across requests

No conversation history inside this layer. The frontend re-sends the full message list on every turn, and `openagent-api` forwards it (or, with memory on, an assembled subset). There is no persistence in the gateway; durable memory, when enabled, lives in openagent-memory's database.

### Single shared inbound key

`OPENAGENT_API_KEY` is one secret for one client. There is no concept of "user A vs user B." `openagent-api` emits `user_id: null` on every event.

### Memory is single-session and best-effort (when enabled)

Today `session_id` comes from a single static `MEMORY_SESSION_ID`, so all traffic shares one memory session until the frontend manages conversations, and `openagent-api` does not validate session lifecycle. Retrieval is fail-open: during a memory outage or embedder cold-start the prompt silently falls back to "recent turns only", dropping older context with no retrieval to compensate. Ingest can fail (surfaced as `memory_ingest_error`) and that turn won't be retrievable later. Memory's database stores conversation content at rest, and the boundary is transport-key only (no HMAC yet). With memory disabled, none of this applies.

### No rate limiting

The gateway trusts authenticated clients absolutely. If exposed beyond the OpenAgent product, rate limiting belongs at a reverse proxy.

### Event capture is best-effort

The capture pipeline is fire-and-forget with an in-memory queue, so events can be lost when the logger is unreachable (drop after a WARNING) or when the queue fills during a sustained outage (drop-oldest). `/chat` is unaffected by this. The captured events also do NOT include per-user attribution (`user_id` always null) or the reasoning chain (`output_text` is `delta.content` only). The `session_id` field is now populated from `MEMORY_SESSION_ID`, but it is a single static value, not a validated per-conversation id.

### Context window handling

Without memory, `openagent-api` forwards whatever messages the frontend sends; long enough conversations will eventually 400 from `openagent-infra` once the model's context window is exceeded — there is no truncation or summarisation at this layer. With memory enabled, the gateway caps verbatim history at the recent-N window and relies on retrieval for older context, which mitigates this — but during a degraded retrieve it falls back to recent-N only, so older context can be dropped silently.

### Cold-start latency inherited from the provider

Serverless workers scale to zero when idle; the first request after inactivity waits for the worker to spin up. `openagent-api` correctly reports `loading` during this time, but cannot make the worker spin up faster. Warm-path requests respond in seconds. A cold embedder behind `openagent-memory` similarly causes retrieval to fail open (recent-only) until it warms.

### Reasoning-format display variance

Some upstream serving stacks emit reasoning-format delimiters that can occasionally bleed into the stream, depending on the runtime's parser support. `openagent-api` forwards bytes byte-for-byte and does not normalise this — display is the frontend's concern. The side-channel parser depends on the same chunk shape to extract `delta.content`; a parser change there would also need to track upstream format changes.

### Mid-stream upstream errors are in-band

Once SSE headers go out (HTTP 200), `openagent-api` can no longer change the status code. Mid-stream upstream failures are surfaced as `data: [ERROR ...]\n\n` followed by `data: [DONE]\n\n`; the HTTP status stays 200. The corresponding `upstream_error` ops_event captures the operational signal regardless.

### bio.txt requires a rebuild

The persona is baked into the image at build time. Rotating it requires `docker-compose up -d --build` (or a dev volume mount over `/app/src/prompt`).

---

*openagent-api — part of the OpenAgent system*