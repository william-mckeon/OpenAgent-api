# openagent-api

> **The Identity Gateway for OpenAgent** — the FastAPI service that owns the OpenAgent persona, brokers every request between `openagent-frontend` and `openagent-infra`, emits structured operational events to `openagent-logger`, and — when configured — ranks relevant prior turns and stores new ones via `openagent-memory`.

---

## Overview

`openagent-api` is the product-layer backend in the OpenAgent system. It is the only HTTP boundary between [`openagent-frontend`](../openagent-frontend) and [`openagent-infra`](../openagent-infra), it emits structured events to [`openagent-logger`](../openagent-logger), and — optionally — it retrieves and ingests conversation turns through [`openagent-memory`](../openagent-memory). It owns five concerns (the fifth optional) that nothing else in the system owns:

- The **OpenAgent persona** (`src/prompt/bio.txt`) — the system prompt that defines who the agent is.
- The **compartmentalized auth chain** — `OPENAGENT_API_KEY` validates the frontend inbound; `INFRA_API_KEY` authenticates openagent-api outbound to openagent-infra; `LOGGER_API_KEY` + `LOGGER_HMAC_SECRET` authenticate and sign openagent-api's outbound emissions to openagent-logger; and, when memory is enabled, `MEMORY_API_KEY` authenticates openagent-api outbound to openagent-memory.
- The **SSE relay** — a transparent byte-for-byte pipe from openagent-infra's token stream to the frontend, with optional `reasoning_effort` pass-through.
- The **fire-and-forget logger emission** — every `/chat` call produces structured events (request_received, upstream_call, [upstream_error if applicable], stream_complete, conversation_capture — plus memory_retrieve_degraded / memory_ingest_error when memory is enabled and degrades) that openagent-api enqueues for a background drain task to deliver to openagent-logger. The `/chat` hot path is never blocked on logger availability.
- The **prompt assembly + optional memory** — openagent-api owns how the upstream prompt is built. When `openagent-memory` is configured, it retrieves relevant prior turns *before* assembling the prompt (on the hot path, but bounded and fail-open) and ingests the completed turn pair *afterward* (off the hot path, as a background task). Memory is **opt-in**: with no memory configuration the gateway forwards the full message list exactly as it did before memory existed. Memory only *ranks*; the gateway *builds* the prompt.

This repo is scoped to the gateway only. It has no model, no inference code, no database, no auth backend. All of those concerns live elsewhere — including the durable conversation store, which (when used) lives in openagent-memory's own database, not here.

The boundary is sharp by design: the frontend owns *how the agent looks*, openagent-api owns *who the agent is, who can talk to it, and how the prompt is built*, openagent-infra owns *how models are accessed*, openagent-logger owns *what was captured*, and openagent-memory (optional) owns *what is remembered across a session*. They communicate over stable HTTP/SSE/HMAC contracts and never share code.

---

## Where This Fits

```text
openagent-os
│
├── openagent-infra      ← separate repo, separate compose stack
│   └── Model proxy (port 8002 → BYOC Provider)
│       Forwards to base reasoning model and control layer model
│
├── openagent-api        ← YOU ARE HERE
│   └── The Identity Gateway (port 8001)
│       Owns the persona, the auth boundary,
│       the SSE relay between frontend and model,
│       the fire-and-forget event emitter to openagent-logger,
│       and (optional) retrieval/ingest via openagent-memory
│
├── openagent-frontend   ← separate repo, separate compose stack
│   └── The product UI (port 8000)
│       Streamlit chat interface, reaches openagent-api over HTTP
│
├── openagent-logger     ← separate repo, separate compose stack
│   └── The capture layer (port 8003)
│       Receives ops_events and conversation_captures from
│       openagent-api via fire-and-forget HMAC-signed HTTP;
│       stores in monthly-partitioned PostgreSQL
│
└── openagent-memory     ← separate repo, separate compose stack (OPTIONAL)
    └── The session-scoped RAG layer (port 8004)
        Receives /retrieve (hot path) and /ingest (off path) from
        openagent-api over transport-key HTTP; embeds and ranks
        prior turns; owns its OWN PostgreSQL + pgvector
```

**Port topology:**
```text
User → openagent-frontend (:8000) → openagent-api (:8001) → openagent-infra (:8002) → BYOC Provider
                                       │
                                       ├─→ openagent-memory (:8004)  [optional: retrieve before, ingest after]
                                       └─→ openagent-logger (:8003)  [fire-and-forget sibling]
```

Users only ever interact with port 8000. Port 8001 is internal to the product layer. Port 8002 is internal to the model layer and is reached only by openagent-api. Port 8003 is internal to the capture layer and is reached only by openagent-api. Port 8004 is internal to the memory layer and, when enabled, is reached only by openagent-api. The external compute provider is reached only by openagent-infra.

---

## Architecture

```text
┌─────────────────────────────────────────────────────────┐
│                    Browser (host)                       │
│                 http://localhost:8000                   │
└─────────────────────────────┬───────────────────────────┘
                              │ HTTPS / WebSocket (Streamlit)
                              ▼
┌─────────────────────────────────────────────────────────┐
│              openagent-frontend (:8000 host / :8501 ctr)│
│                                                         │
│  • Posts {messages, reasoning_effort?}                  │
│  • Sends X-API-Key: OPENAGENT_API_KEY                   │
│  • Consumes SSE stream, splits reasoning from answer    │
│  • Polls /health to gate the chat input                 │
└─────────────────────────────┬───────────────────────────┘
                              │ HTTP POST /chat (SSE response)
                              │ HTTP GET  /health
                              │ Headers:  X-API-Key (OPENAGENT_API_KEY)
                              ▼
┌─────────────────────────────────────────────────────────┐
│              Docker Container (openagent-api :8001)     │
│                                                         │
│  • Validates inbound X-API-Key (OPENAGENT_API_KEY)      │
│  • Drops frontend-supplied system messages              │
│  • (Optional) Retrieves prior turns from openagent-     │
│    memory, then assembles the upstream prompt as        │
│    [bio]+[retrieved]+[recent N]+[current]               │
│  • Prepends bio.txt as the system message               │
│  • Forwards reasoning_effort if frontend sent one       │
│  • Generates request_id (uuid4) per /chat call          │
│  • Threads session_id (from MEMORY_SESSION_ID) onto     │
│    every emitted event                                  │
│  • Opens streaming POST to openagent-infra              │
│  • Re-emits SSE byte-for-byte to the frontend           │
│  • Side-channel parse: accumulates output_text from     │
│    delta.content tokens for conversation_capture        │
│  • Emits events to openagent-logger (fire-and-forget)   │
│  • (Optional) Ingests the turn pair to openagent-memory │
│    after a successful stream (off the user's path)      │
│  • Detects mid-stream client disconnects                │
│                                                         │
└──┬──────────────────────────────────────────────┬───────┘
   │                                              │
   │  HOT PATH                                    │  FIRE-AND-FORGET
   │  (blocks /chat response)                     │  (never blocks /chat)
   │  HTTP POST /chat (SSE)                       │  HTTP POST /events
   │  HTTP GET  /health                           │  per /chat call
   │  Header:  X-API-Key (INFRA_API_KEY)          │  Headers:
   │  Target:  OPENAGENT_INFRA_URL                │    X-API-Key (LOGGER_API_KEY)
   │                                              │  + HMAC-SHA256 signature
   │                                              │  Signed with: LOGGER_HMAC_SECRET
   │                                              │  Target: LOGGER_URL
   ▼                                              ▼
┌──────────────────────────────┐    ┌──────────────────────────────────┐
│ openagent-infra (SEP. REPO)  │    │ openagent-logger (SEP. REPO)     │
│ FastAPI proxy on port 8002   │    │ Capture layer on port 8003       │
│                              │    │                                  │
│ • Injects "Reasoning: <X>"   │    │ • Validates X-API-Key            │
│   into system message        │    │ • Verifies HMAC signature        │
│ • Forwards to BYOC provider  │    │ • Replay-window check (300s)     │
│ • Stateless — full msgs      │    │ • Writes to partitioned PG       │
│   list per request           │    │ • Three tables:                  │
│                              │    │     ops_events                   │
│                              │    │     conversation_captures        │
│                              │    │     audit_events                 │
└──────────────┬───────────────┘    └──────────────┬───────────────────┘
               │                                   │
               │ HTTPS POST                        │ PostgreSQL TCP
               │ Authorization: Bearer             │ Schema: openagent_logger
               │   PROVIDER_API_KEY                │ Monthly RANGE partitioning
               ▼                                   ▼
┌──────────────────────────────┐    ┌──────────────────────────────────┐
│  BYOC Compute Provider       │    │  PostgreSQL (shared instance)    │
│  (e.g., RunPod, OpenAI)      │    │                                  │
│  base reasoning model        │    │                                  │
│  control layer model         │    │                                  │
└──────────────────────────────┘    └──────────────────────────────────┘
```

The diagram above shows the two always-present boundaries — the openagent-infra hot path and the openagent-logger fire-and-forget sibling. The **optional** openagent-memory boundary is documented separately, just below, so the core diagram stays readable.

### Memory boundary (optional)

When `MEMORY_URL` and `MEMORY_API_KEY` are both set, openagent-api adds a third outbound boundary to openagent-memory. It is consulted twice per turn — once on the hot path (retrieve, before assembly) and once off the user's path (ingest, after a clean stream):

```text
   openagent-api (:8001)
     │
     ├─[HOT PATH, before assembly]──▶ POST /retrieve {session_id, query, top_k?}
     │     • query = the latest user message
     │     • bounded by MEMORY_RETRIEVE_TIMEOUT, FAIL-OPEN: any timeout / error /
     │       degraded response ⇒ ([], degraded) ⇒ the prompt falls back to
     │       "recent turns only" and the user's first token is never delayed
     │     • returns {retrieved:[{id, role, content, score, created_at}], degraded}
     │
     └─[OFF-PATH, after a clean stream]──▶ POST /ingest ×2 {session_id, role, content}
           • user turn first, then assistant turn (a detached background task,
             so created_at reflects turn order)
           • NOT fail-open: a 503/error surfaces as a memory_ingest_error
             ops_event — but it runs after the user already has their answer,
             so it never blocks or fails /chat

   openagent-memory (:8004) — session-scoped RAG; owns its OWN PostgreSQL + pgvector.
   Auth: X-API-Key: MEMORY_API_KEY  (transport-key only — NO HMAC on this boundary
   today; the MemoryClient scaffolds signing for a future addition).
```

When memory is **active**, the upstream prompt is assembled as:

```text
   [ system: bio ]
   + [ retrieved older turns, deduped vs the recent window by SHA-256 content hash ]
   + [ the most recent N turns, verbatim ]   (N = MEMORY_RECENT_N, default 10)
   + [ the current user turn ]
```

When memory is **disabled** (or enabled but no `MEMORY_SESSION_ID` is set), the gateway forwards `[ system: bio ] + [ the full message list ]` exactly as before — it never truncates without retrieval to compensate.

### Request flow

1. The user sends a message in the openagent-frontend chat UI.
2. The frontend appends it to its in-session message list and POSTs `{messages: [...], reasoning_effort?: "..."}` to `openagent-api/chat` with `X-API-Key: OPENAGENT_API_KEY`.
3. `openagent-api` validates the inbound key (returns 401 if missing or wrong).
4. `openagent-api` generates a fresh `request_id` (UUID4) for the call and reads `session_id` from `MEMORY_SESSION_ID` (null when unset). Every subsequent event emitted to openagent-logger for this `/chat` carries the same `request_id` and `session_id`, so the events can be joined post-hoc by querying openagent-logger's database.
5. `openagent-api` emits a `request_received` ops_event to openagent-logger (fire-and-forget, microseconds).
6. `openagent-api` filters out any `"system"` messages the frontend tried to send (defense in depth — the canonical persona lives here, not in the client).
7. **(Optional — memory enabled and a `session_id` is set.)** `openagent-api` calls openagent-memory's `/retrieve` with the latest user message as the query. The call is awaited on the hot path but **bounded** (`MEMORY_RETRIEVE_TIMEOUT`) and **fail-open**: any timeout, transport error, non-200, or `degraded:true` response yields no retrieved turns, emits a `memory_retrieve_degraded` ops_event, and proceeds with "recent turns only". Retrieval never blocks or fails `/chat`.
8. `openagent-api` assembles the upstream prompt and forwards it to openagent-infra at `OPENAGENT_INFRA_URL/chat` with `X-API-Key: INFRA_API_KEY`. With memory active the prompt is `[bio] + [retrieved older turns, deduped against the recent window by SHA-256 content hash] + [recent N turns verbatim] + [current user turn]`; otherwise it is `[bio] + [full message list]`. `reasoning_effort` is included only when the frontend sent one. Immediately before the upstream POST, it emits an `upstream_call` ops_event.
9. openagent-infra injects `Reasoning: <level>` into the system message and forwards to the compute provider.
10. The provider streams tokens back through openagent-infra to openagent-api as SSE events.
11. `openagent-api` re-emits each event byte-for-byte to the frontend via `StreamingResponse`. The `data:` prefix and the `[DONE]` sentinel pass through unchanged. While the bytes pass through, a **side-channel parser** accumulates the `delta.content` tokens into an `output_text` buffer for the eventual conversation capture. The yield to the frontend happens BEFORE the parse in every iteration, so the parse adds zero latency to the user-visible stream.
12. When the upstream stream finishes cleanly, `openagent-api` emits a `stream_complete` ops_event (with `bytes_relayed` and `latency_ms` in details), then emits a `conversation_capture` carrying the assembled `output_text`, model identifier, `reasoning_effort`, latency, and the `session_id`. If the upstream fails at any point, `openagent-api` emits an `upstream_error` ops_event with the exception class name in details — no `stream_complete`, no `conversation_capture`, no ingest.
13. **(Optional — memory enabled.)** After a successful stream, `openagent-api` fires a background ingest of the user turn and then the assistant turn to openagent-memory, off the user's path. Ingest failures are logged and surfaced as `memory_ingest_error` ops_events; they never affect `/chat`. A client-disconnect or errored turn ingests neither side.
14. If the frontend disconnects mid-stream, `openagent-api` detects it, abandons the upstream connection so the compute provider stops generating tokens nobody will read, and emits a `stream_complete` ops_event with `outcome=client_disconnect` (no `conversation_capture` and no ingest for partial streams).

### Separation of concerns

| Concern                       | Lives in         | Why                                                    |
|-------------------------------|------------------|--------------------------------------------------------|
| Persona / system prompt       | `openagent-api`  | Identity is product-layer backend logic; not UI logic  |
| Prompt assembly (RAG when memory enabled) | `openagent-api` | The gateway builds the final prompt; memory only ranks |
| Inbound auth (frontend → api) | `openagent-api`  | Auth boundaries belong on the server, not the client   |
| Outbound auth (api → infra)   | `openagent-api`  | Upstream secret never leaves the backend network       |
| Outbound auth (api → logger)  | `openagent-api`  | Logger secret never leaves the backend network         |
| Outbound auth (api → memory)  | `openagent-api`  | Memory secret never leaves the backend network (optional) |
| Request correlation ID        | `openagent-api`  | `request_id` (UUID4) generated per /chat, threads through every event emitted to openagent-logger |
| Session correlation ID        | `openagent-api`  | `session_id` (from `MEMORY_SESSION_ID`) threads through every event when set; scopes memory retrieve/ingest |
| Event emission (fire-and-forget) | `openagent-api`| The /chat hot path must not block on the capture layer |
| Event capture & storage       | `openagent-logger`| Sibling service; partitioned PostgreSQL, append-only   |
| Session-scoped retrieval & storage (optional) | `openagent-memory` | Sibling service; owns its own PostgreSQL + pgvector |
| Conversation history          | `openagent-frontend` | Held in-session by the frontend                 |
| Reasoning-format display      | `openagent-frontend` | UX policy decision, not an inference concern           |
| Reasoning effort default      | `openagent-infra`| Server-side default lives upstream; openagent-api passes through only |
| Reasoning effort injection    | `openagent-infra`| openagent-infra writes "Reasoning: <level>" into system msg |
| Model serving & inference     | BYOC Provider    | Heavy, GPU-dependent                                   |

### Compartmentalized auth chain

The auth model is **defense in depth** across multiple boundaries: independently generated secrets, validated at each boundary, with no key relayed unchanged across services.

```text
frontend ──[OPENAGENT_API_KEY]──> openagent-api ──[INFRA_API_KEY]──> openagent-infra
                                       │  │                                │
                                       │  │                       [PROVIDER_API_KEY]
                                       │  │                                ▼
                                       │  │                          BYOC Provider
                                       │  │
                                       │  └──[LOGGER_API_KEY + LOGGER_HMAC_SECRET]──> openagent-logger
                                       │
                                       └──[MEMORY_API_KEY]──> openagent-memory   (optional; transport-key only, no HMAC)
```

- `OPENAGENT_API_KEY` lives in `openagent-frontend/.env` and `openagent-api/.env`. openagent-api validates it on every inbound request.
- `INFRA_API_KEY` lives in `openagent-api/.env` and `openagent-infra/.env`. openagent-infra validates it on every inbound request.
- `LOGGER_API_KEY` lives in `openagent-api/.env` and `openagent-logger/.env`. openagent-logger validates it on every inbound `/events` request.
- `LOGGER_HMAC_SECRET` lives in `openagent-api/.env` and `openagent-logger/.env`. openagent-api signs every event payload with it; openagent-logger re-derives and verifies the signature on every received event AND stores the signature on the row for offline re-verification.
- `MEMORY_API_KEY` lives in `openagent-api/.env` and `openagent-memory/.env` (only when memory is enabled). openagent-memory validates it on every inbound `/retrieve` and `/ingest` request. **Unlike the logger boundary, there is no HMAC on the memory boundary today** — openagent-memory uses transport-key auth only; the MemoryClient scaffolds signing for a future addition.
- `PROVIDER_API_KEY` lives in `openagent-infra/.env` only. openagent-api never sees it.

Each value is **independent**. The frontend never sees `INFRA_API_KEY`, `LOGGER_API_KEY`, `LOGGER_HMAC_SECRET`, `MEMORY_API_KEY`, or `PROVIDER_API_KEY`. The keys are not relayed.

---

## Tech Stack

| Layer              | Technology                                    |
|--------------------|-----------------------------------------------|
| Base image         | `python:3.11-slim`                            |
| API framework      | FastAPI + uvicorn                             |
| HTTP client        | `httpx` (async, with streaming support)       |
| Env loading        | `python-dotenv`                               |
| Validation         | Pydantic (bundled with FastAPI)               |
| HMAC signing       | `hmac` + `hashlib` (Python stdlib)            |
| Content hashing (dedup) | `hashlib` (Python stdlib)                |
| Event queue / background tasks | `asyncio` (Python stdlib)         |
| Containerization   | Docker + Docker Compose                       |
| Port (internal)    | 8001                                          |
| Port (host)        | 8001                                          |
| Auth (inbound)     | API key via `X-API-Key` header                |
| Auth (outbound to infra)  | API key via `X-API-Key` header         |
| Auth (outbound to logger) | API key via `X-API-Key` header + HMAC-SHA256 payload signature |
| Auth (outbound to memory) | API key via `X-API-Key` header (transport-key only; no HMAC today) |
| Communication in   | HTTP/1.1 (frontend → openagent-api)           |
| Communication out  | HTTP/1.1 + SSE (openagent-api → openagent-infra)      |
| Communication out  | HTTP/1.1 (openagent-api → openagent-logger, fire-and-forget JSON POST) |
| Communication out  | HTTP/1.1 (openagent-api → openagent-memory, hot-path retrieve + off-path ingest) |

Intentionally absent: `groq`, `openai`, `torch`, `transformers`, `sqlalchemy`, `streamlit`, `cryptography`, `pynacl`, `arq`, `redis` — none of them belong in a stateless gateway. HMAC signing uses Python stdlib; the event queue and the memory background ingest tasks use `asyncio` from stdlib; content-hash dedup uses `hashlib` from stdlib. The memory client adds **zero** new dependencies — it reuses the existing httpx + stdlib stack.

---

## Prerequisites

- **Docker Desktop** (macOS / Windows) or **Docker Engine + Compose v2** (Linux)
- **`openagent-infra` running and reachable** — either on the host, in another Docker container, or deployed elsewhere. openagent-infra in turn requires a BYOC provider (e.g., RunPod, OpenAI).
- **`openagent-logger` running and reachable** — either on the host, in another Docker container, or deployed elsewhere. openagent-logger in turn requires a PostgreSQL 13+ instance.
- **`openagent-memory` running and reachable (OPTIONAL)** — only if you enable memory. It runs in its own stack and owns its own PostgreSQL + pgvector instance. If you do not configure memory, the gateway runs without it and forwards full history.
- **Four valid secrets** — `OPENAGENT_API_KEY`, `INFRA_API_KEY`, `LOGGER_API_KEY`, `LOGGER_HMAC_SECRET` — plus `MEMORY_API_KEY` if (and only if) you enable memory.

You do **not** need:
- A GPU (the gateway does no inference)
- Python installed on the host (Docker handles it)
- A compute provider API key (those belong to openagent-infra)
- Direct PostgreSQL access (that's openagent-logger's and openagent-memory's concern)
- openagent-memory at all, unless you want session-scoped retrieval/ingest

---

## Project Structure

```text
openagent-api/
├── docker/
│   └── api/
│       └── Dockerfile              # Python 3.11 slim, non-root, healthcheck'd
├── src/
│   ├── backend/
│   │   └── api.py                  # The FastAPI app (single file)
│   ├── client/
│   │   ├── __init__.py             # Package marker / client enumeration
│   │   ├── infra.py                # InfraClient — proxy to openagent-infra
│   │   ├── logger.py               # LoggerClient — fire-and-forget to openagent-logger
│   │   └── memory.py               # MemoryClient — retrieve/ingest to openagent-memory (optional)
│   └── prompt/
│       └── bio.txt                 # OpenAgent system prompt (persona)
├── docker-compose.yml              # Single-service compose
├── requirements.txt                # fastapi, uvicorn, httpx, python-dotenv
├── .env                            # secrets — never commit
├── .env.example                    # template for .env
├── .dockerignore                   # keeps secrets / caches out of build context
├── .gitignore                      # keeps secrets / caches out of git
└── README.md                       # this file
```

---

## Setup

### 1. Clone the repo

```bash
git clone https://github.com/william-mckeon/openagent-api.git
cd openagent-api
```

### 2. Create your `.env` file

```bash
cp .env.example .env
```

Open `.env` and set the **six required values**:

```env
OPENAGENT_API_KEY=<long random hex>
INFRA_API_KEY=<long random hex, MUST match openagent-infra's API_KEY>
OPENAGENT_INFRA_URL=http://host.docker.internal:8002

LOGGER_URL=http://host.docker.internal:8003
LOGGER_API_KEY=<copy from openagent-logger/.env LOGGER_API_KEY>
LOGGER_HMAC_SECRET=<copy from openagent-logger/.env LOGGER_HMAC_SECRET>
```

**Optionally** enable session-scoped memory by also setting the memory block (it adds no required values — leave it unset to run without memory):

```env
MEMORY_URL=http://host.docker.internal:8004
MEMORY_API_KEY=<copy from openagent-memory/.env MEMORY_API_KEY>
MEMORY_SESSION_ID=dev-session-001
# MEMORY_RECENT_N=10
# MEMORY_TOP_K=
# MEMORY_RETRIEVE_TIMEOUT=5.0
```

Memory is enabled only when **both** `MEMORY_URL` and `MEMORY_API_KEY` are set. `MEMORY_SESSION_ID` scopes retrieve/ingest and populates `session_id` on emitted events; if memory is enabled but the session id is unset, retrieve/ingest stay inactive (with a one-line startup warning) and full history is forwarded.

**Generate fresh keys**:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

The keys MUST be different values. Reusing one defeats the entire compartmentalized auth design. As with the logger secrets, copy `MEMORY_API_KEY` from openagent-memory's own `.env` (it is the source of truth) rather than minting a new one.

### 3. Update `bio.txt` with your persona

The persona lives in `src/prompt/bio.txt`. The repo ships with a generic starter persona; replace it with your own before running anything you care about. The file is read once at container startup and prepended as the first `system` message on every `/chat` call.

The file is baked into the Docker image at build time. **Rebuilding is required to pick up changes** — or mount a volume over `/app/src/prompt` in `docker-compose.yml` for live edits during development.

### 4. Make sure `openagent-infra` and `openagent-logger` are running (and `openagent-memory` if enabled)

`openagent-api` is a thin gateway. Without openagent-infra reachable at `OPENAGENT_INFRA_URL`, `/health` will report `unreachable`.

`openagent-api` refuses to boot if `LOGGER_URL`, `LOGGER_API_KEY`, or `LOGGER_HMAC_SECRET` is missing. It does NOT refuse to boot if openagent-logger is unreachable at startup — fire-and-forget is graceful about transient logger unavailability.

Memory is different again: openagent-api does **not** refuse to boot when memory is unconfigured, and it does **not** refuse to boot (or fail `/chat`) when memory is configured but unreachable — retrieval fails open and ingest is off-path. If you have enabled memory, make sure openagent-memory is reachable at `MEMORY_URL` to actually get retrieval/ingest; otherwise the gateway silently falls back to "recent turns only" / no ingest.

### 5. Build and start

```bash
docker-compose up -d --build
```

### 6. Verify

```bash
# Confirm /health is reachable and authenticated.
curl -H "X-API-Key: <your OPENAGENT_API_KEY>" http://localhost:8001/health
```

To verify the logger integration is working end-to-end, send a real `/chat` call and then query openagent-logger:

```bash
# Send a /chat:
curl -N -X POST \
  -H "X-API-Key: <OPENAGENT_API_KEY>" \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"say hi"}],"reasoning_effort":"low"}' \
  http://localhost:8001/chat

# Then check openagent-logger received the events:
curl -fsS -H "X-API-Key: <LOGGER_API_KEY>" http://localhost:8003/stats
```

Note: `/health` intentionally reports only openagent-infra's readiness — openagent-logger and openagent-memory are non-essential to serving a `/chat` response (logger is fire-and-forget; memory retrieval fails open), so they are deliberately excluded from the gate-open signal. To check those directly, query their own `/health` endpoints.

---

## How It Works

### System prompt ownership

The persona is owned by this repo and nowhere else. The frontend does not carry it. openagent-infra has no knowledge of it.

Concretely:
1. At container startup, it reads `/app/src/prompt/bio.txt` once into memory.
2. On every `/chat` call, that content is prepended as `{"role": "system", "content": <bio>}` before the request is forwarded upstream.
3. If a frontend tries to send its own system message in the request body, it is dropped with a warning log.

### Inbound auth (frontend → openagent-api)

Every `/chat` and `/health` request must include `X-API-Key` matching `OPENAGENT_API_KEY`. The validation is implemented as a FastAPI dependency.

### Outbound auth (openagent-api → openagent-infra)

The outbound boundary is owned by the `InfraClient` class in `src/client/infra.py`. The client's internal `httpx.AsyncClient` is constructed once at `start()` time with `headers={"X-API-Key": INFRA_API_KEY}`. Every outbound call inherits this header automatically.

### Outbound auth (openagent-api → openagent-logger)

The `LoggerClient` constructs its own `httpx.AsyncClient` at startup with `headers={"X-API-Key": LOGGER_API_KEY}` and `base_url=LOGGER_URL`. Every event POST inherits the header automatically.

In addition to the transport key, every event body carries an `hmac_signature` field — the HMAC-SHA256 of the canonical string `{request_id}|{client_timestamp_iso}|{event_type}|sha256(canonical_payload_json)`, keyed with `LOGGER_HMAC_SECRET`.

### Outbound auth (openagent-api → openagent-memory) — optional

The `MemoryClient` in `src/client/memory.py` constructs its own `httpx.AsyncClient` at startup with `headers={"X-API-Key": MEMORY_API_KEY}` and `base_url=MEMORY_URL`. Every `/retrieve` and `/ingest` call inherits the header automatically. There is **no** HMAC on this boundary today — openagent-memory uses transport-key auth only. The client scaffolds signing (mirroring the logger's canonical-payload helpers) so that adding HMAC later, when openagent-memory defines a contract, is a localized change.

### Session-scoped memory (optional)

When `MEMORY_URL` + `MEMORY_API_KEY` are set, openagent-api enables memory. It is consulted at two points per turn:

- **Retrieve (hot path, fail-open).** Before assembling the prompt, openagent-api calls `/retrieve` with the latest user message as the query, bounded by `MEMORY_RETRIEVE_TIMEOUT`. Any timeout, error, non-200, or `degraded:true` response returns no turns, emits `memory_retrieve_degraded`, and the prompt falls back to "recent turns only". A memory problem degrades answer quality but never delays the first token or fails `/chat`.
- **Assemble.** With retrieval results in hand, openagent-api builds `[bio] + [retrieved older turns, deduped against the recent-N window by SHA-256 content hash] + [recent N turns verbatim] + [current user turn]`. Memory only ranks; the gateway builds the prompt.
- **Ingest (off the user's path, after a clean stream).** Once the stream completes successfully, openagent-api fires a detached background task that ingests the user turn then the assistant turn (sequentially, so `created_at` reflects order). Ingest is *not* fail-open — openagent-memory answers 503 when its embedder is down so the loss is signalled — but because it runs after the user already has their answer, the failure surfaces only as a `memory_ingest_error` ops_event and never affects `/chat`.

Memory is **opt-in** and **not a refuse-to-boot dependency**. With no memory configuration (or an unset `MEMORY_SESSION_ID`), the gateway forwards the full message list exactly as before.

### Reasoning effort pass-through

`reasoning_effort` is an optional field on the `/chat` request body. Three accepted values: `low`, `medium`, `high`. The behaviour is **pure pass-through**:

- When the frontend sends a value, openagent-api includes it in the upstream payload to openagent-infra.
- When the frontend omits the field, openagent-api also omits it from the upstream payload — openagent-infra applies its own server-side default.

### SSE relay

The `/chat` endpoint is a `StreamingResponse` driven by an async generator (`sse_pump`). The generator opens a streaming POST to openagent-infra, loops on `response.aiter_raw()`, and yields each chunk straight to the frontend.

### Health gate (proxied)

The frontend's `/health` polling loop talks to openagent-api. openagent-api forwards the question to openagent-infra and returns a status the frontend already understands. The endpoint reports only openagent-infra's readiness; the logger and memory boundaries are deliberately excluded (both are non-essential to serving `/chat`).

### Statelessness

openagent-api is **stateless across requests**. It holds no conversation memory, no session store, no per-user state in process. The frontend sends the full message list on every turn. When memory is disabled, the gateway forwards that list without adding or remembering anything. When memory is enabled, the durable conversation store lives in **openagent-memory's** database (reached over HTTP) — never in openagent-api itself; the gateway still keeps no per-session state of its own beyond the static `MEMORY_SESSION_ID` it reads from the environment.

---

## Security Model

The system uses a **compartmentalization** architecture for service-to-service authentication. Each service holds only the secrets for the boundaries it directly touches; nothing is "passed through" the chain unchanged. The pattern makes single-service compromise containable rather than catastrophic.

### Blast-radius analysis

- **If `openagent-frontend` is compromised**: the attacker gets `OPENAGENT_API_KEY` only. They cannot reach openagent-infra, openagent-logger, openagent-memory, or the compute provider directly.
- **If `openagent-api` is compromised**: the attacker gets `OPENAGENT_API_KEY`, `INFRA_API_KEY`, `LOGGER_API_KEY`, `LOGGER_HMAC_SECRET`, and — if memory is configured — `MEMORY_API_KEY`. They still don't have `PROVIDER_API_KEY` — they cannot bypass openagent-infra to bill the compute provider directly.
- **If `openagent-infra` is compromised**: the attacker gets `INFRA_API_KEY` and `PROVIDER_API_KEY`. The model layer is exposed, but openagent-api's inbound boundary stays safe, openagent-logger and openagent-memory stay safe, and the frontend stays safe.
- **If `openagent-logger` is compromised**: the attacker gets `LOGGER_API_KEY` and `LOGGER_HMAC_SECRET`. They can read whatever events have been captured, but they cannot reach openagent-api's inbound, openagent-infra, openagent-memory, or the compute provider.
- **If `openagent-memory` is compromised**: the attacker gets `MEMORY_API_KEY` and the conversation turns stored in memory's own database. They cannot reach openagent-api's inbound, openagent-infra, openagent-logger, or the compute provider. Note that memory's database holds conversation **content** (user and assistant turns) at rest — treat it with the same care as openagent-logger's conversation captures.

### Service-to-service vs user-to-service auth

The keys above all authenticate **services to services**, not users to services. They identify which service is talking, not which person. There is no concept of a user identity in the current system — `OPENAGENT_API_KEY` is a single shared secret between openagent-frontend and openagent-api, and `MEMORY_SESSION_ID` is a single static session scope, not a per-user credential.

### Key rotation procedure

**Rotating `LOGGER_API_KEY` (transport)**: Rotation is **cheap** — the key only protects future submissions; existing rows in openagent-logger's database are unaffected.
**Rotating `LOGGER_HMAC_SECRET` (payload signing)**: Rotation is **expensive** — pre-rotation signatures cannot be re-verified with the new secret. Rotate only on known compromise.
**Rotating `MEMORY_API_KEY` (transport)**: Rotation is **cheap** — like `LOGGER_API_KEY`, it only gates future `/retrieve` and `/ingest` calls; stored turns are unaffected. There is no HMAC secret on the memory boundary to rotate today.

---

## Integration with openagent-logger

The integration is **fire-and-forget by design**: openagent-api emits events into an in-process queue and returns immediately; a background asyncio task drains the queue and POSTs to openagent-logger. The `/chat` hot path is never coupled to openagent-logger availability. If openagent-logger is unreachable, events queue up while `/chat` continues to serve normally.

### What gets emitted

The core events per `/chat` call:
1. `request_received`
2. `upstream_call`
3. `upstream_error` (if applicable)
4. `stream_complete`
5. `conversation_capture` (after a successful stream_complete only)

When memory is enabled, two additional event types can appear, conditionally: `memory_retrieve_degraded` (when a retrieve fails open) and `memory_ingest_error` (when a background ingest fails). Both carry the same `request_id` / `session_id` correlation as the rest.

### Queue overflow: drop-oldest

The queue is bounded. When it fills (sustained openagent-logger outage, or massive burst of traffic), the policy is **drop-oldest with WARNING log**.

### Required configuration

Three env vars are required at startup: `LOGGER_URL`, `LOGGER_API_KEY`, `LOGGER_HMAC_SECRET`.

---

## Integration with openagent-memory (optional)

The memory integration is **opt-in** and **non-blocking**. openagent-api enables it only when both `MEMORY_URL` and `MEMORY_API_KEY` are set; otherwise the gateway behaves exactly as it did before memory existed.

### Retrieve vs ingest — two different failure policies

- **Retrieve is fail-open.** It runs on the hot path before prompt assembly, bounded by `MEMORY_RETRIEVE_TIMEOUT`. Any timeout, transport error, non-200, or `degraded:true` response yields no turns and a `memory_retrieve_degraded` ops_event — the prompt simply falls back to "recent turns only". Retrieval can never delay the first token or fail `/chat`.
- **Ingest is not fail-open, but it is off-path.** After a clean stream, openagent-api ingests the user turn then the assistant turn on a detached background task. openagent-memory deliberately answers 503 when its embedder is unavailable (a silently-dropped ingest would remove a turn from all future retrieval), so the failure is surfaced — as a `memory_ingest_error` ops_event — but it runs after the user already has their answer, so it never blocks or fails `/chat`.

### Prompt assembly

openagent-api owns assembly. With memory active the upstream prompt is `[bio] + [retrieved older turns, deduped vs the recent-N window by SHA-256 content hash] + [recent N turns verbatim] + [current user turn]`. Memory only returns ranked candidate turns; the gateway decides what goes into the prompt and in what order (retrieved turns are placed in chronological order ahead of the recent window).

### Required configuration (only to enable)

`MEMORY_URL` + `MEMORY_API_KEY` enable memory. `MEMORY_SESSION_ID` is needed for retrieve/ingest to actually run (it scopes them and populates the event `session_id`). `MEMORY_RECENT_N`, `MEMORY_TOP_K`, and `MEMORY_RETRIEVE_TIMEOUT` are optional tunables.

### Shutdown ordering

At shutdown the clients stop in the order **memory → logger → infra**, so any `memory_ingest_error` events emitted while draining in-flight ingests can still be enqueued onto a live logger.

---

## API Reference

### `POST /chat`

The main endpoint. Forward a list of user/assistant turns and receive a streamed response via Server-Sent Events.

**Request headers:**
```text
Content-Type: application/json
X-API-Key: <OPENAGENT_API_KEY>
```

**Request body:**
```json
{
  "messages": [
    {"role": "user",      "content": "What is the Fibonacci sequence?"},
    {"role": "assistant", "content": "The Fibonacci sequence is..."},
    {"role": "user",      "content": "Show me in Python"}
  ],
  "reasoning_effort": "medium"
}
```

The request body is unchanged by the memory integration — there is intentionally **no** `session_id` field yet. `session_id` comes from the `MEMORY_SESSION_ID` env var today; it will move to a request field (or header) once the frontend manages conversations. Memory retrieval/ingest is transparent to the API contract.

**Response:** `text/event-stream`

### `GET /health`

Proxied health check. Reports openagent-infra readiness only (logger and memory are excluded as non-essential).

**Request headers:**
```text
X-API-Key: <OPENAGENT_API_KEY>
```

### `GET /docs`

Auto-generated Swagger UI. Open in a browser:
```text
http://localhost:8001/docs
```

---

## Configuration

All configuration is loaded from `.env` at the repository root via `python-dotenv` and `docker-compose`'s `env_file:` directive. See `.env.example` for the full template.

### Required

| Variable             | Type   | Description                                                              |
|----------------------|--------|--------------------------------------------------------------------------|
| `OPENAGENT_API_KEY`  | string | Inbound auth secret. Validated on every /chat and /health request.       |
| `INFRA_API_KEY`      | string | Outbound auth secret to openagent-infra. Sent as X-API-Key on every call.|
| `OPENAGENT_INFRA_URL`| string | Base URL of openagent-infra. No trailing slash.                          |
| `LOGGER_URL`         | string | Base URL of openagent-logger. No trailing slash.                         |
| `LOGGER_API_KEY`     | string | Outbound transport secret to openagent-logger.                           |
| `LOGGER_HMAC_SECRET` | string | Payload-signing secret for events.                                       |

### Optional

| Variable                          | Default                       | Description                                            |
|-----------------------------------|-------------------------------|--------------------------------------------------------|
| `OPENAGENT_FRONTEND_URL`          | —                             | Extra CORS origin for production deployments.          |
| `OPENAGENT_LOG_LEVEL`             | `INFO`                        | DEBUG \| INFO \| WARNING \| ERROR \| CRITICAL          |
| `OPENAGENT_UPSTREAM_CONNECT_TIMEOUT`| `10.0`                      | Seconds to wait for TCP connect to openagent-infra.    |
| `OPENAGENT_UPSTREAM_READ_TIMEOUT` | `none`                        | Seconds (or `none` for unbounded) for streaming reads. |
| `OPENAGENT_HEALTH_TIMEOUT`        | `5.0`                         | Seconds for upstream /health probe.                    |
| `OPENAGENT_BIO_PATH`              | `/app/src/prompt/bio.txt`     | Override path to bio.txt inside the container.         |
| `OPENAGENT_LOGGER_QUEUE_MAX_SIZE` | `1000`                        | Max pending events in LoggerClient queue before drop-oldest. |

### Optional — openagent-memory (opt-in)

Memory is enabled only when **both** `MEMORY_URL` and `MEMORY_API_KEY` are set. Leave them unset to run without memory (full history forwarded).

| Variable                  | Default              | Description                                                          |
|---------------------------|----------------------|---------------------------------------------------------------------|
| `MEMORY_URL`              | — (unset = disabled) | Base URL of openagent-memory. No trailing slash. Enables memory together with `MEMORY_API_KEY`. |
| `MEMORY_API_KEY`          | — (unset = disabled) | Outbound transport secret to openagent-memory (no HMAC on this boundary). Must match openagent-memory's `MEMORY_API_KEY`. |
| `MEMORY_SESSION_ID`       | — (empty = inactive) | Static session scope; also populates `session_id` on emitted events. If empty while memory is enabled, retrieve/ingest stay inactive (boot warning). |
| `MEMORY_RECENT_N`         | `10`                 | Most-recent messages kept verbatim in the assembled prompt (counted in messages). |
| `MEMORY_TOP_K`            | — (memory's own, 5)  | Cap on retrieved turns per /retrieve. Unset → openagent-memory applies its own default. |
| `MEMORY_RETRIEVE_TIMEOUT` | `5.0`                | Seconds to wait for /retrieve (hot path, fail-open).                 |

Note: there is intentionally **no `OPENAGENT_DEFAULT_REASONING_EFFORT`**. openagent-api is pure pass-through for `reasoning_effort`. There is also intentionally **no `MEMORY_HMAC_SECRET`** — the memory boundary has no HMAC today.

---

## Local Development (without Docker)

For faster iteration than a Docker rebuild, run uvicorn directly on the host:

```bash
# 1. Create a virtualenv
python3.11 -m venv .venv
source .venv/bin/activate   # Linux/macOS
# .venv\Scripts\activate    # Windows

# 2. Install dependencies
pip install -r requirements.txt

# 3. Export env or rely on .env (python-dotenv picks it up)
export OPENAGENT_API_KEY=...
export INFRA_API_KEY=...
export OPENAGENT_INFRA_URL=http://localhost:8002
export LOGGER_URL=http://localhost:8003
export LOGGER_API_KEY=...        # copy from openagent-logger/.env
export LOGGER_HMAC_SECRET=...    # copy from openagent-logger/.env
export OPENAGENT_BIO_PATH=$(pwd)/src/prompt/bio.txt
# Optional — enable memory:
# export MEMORY_URL=http://localhost:8004
# export MEMORY_API_KEY=...      # copy from openagent-memory/.env
# export MEMORY_SESSION_ID=dev-session-001

# 4. Run uvicorn on port 8001
PYTHONPATH=src uvicorn backend.api:app --host 0.0.0.0 --port 8001 --reload
```

---

## Design Decisions

### Why does the persona live here and not in the frontend?

Identity is product-layer **backend** logic, not UI logic. The frontend is replaceable; the identity is not.

### Why two API keys instead of one shared secret?

Defense in depth. With one shared key, compromise of any of the services compromises all of them.

### Why two secrets for openagent-logger (LOGGER_API_KEY + LOGGER_HMAC_SECRET)?

Different threat models and different rotation profiles. `LOGGER_API_KEY` (transport) gates wire-level access. `LOGGER_HMAC_SECRET` (payload signing) provides payload integrity for downstream consumers to verify event integrity offline.

### Why fire-and-forget logger emission?

The /chat hot path must not block on the capture layer.

### Why drop-oldest queue overflow?

When the queue fills (sustained openagent-logger outage), fresher data is more useful than stale data when the storm passes.

### Why no reasoning chain capture?

`conversation_capture.output_text` contains only `delta.content` tokens (the visible answer), not `delta.reasoning` tokens (the chain-of-thought). Capturing the chain couples the capture schema to a specific model's emission format.

### Why is `reasoning_effort` pass-through with no api default?

The setting has one source of truth: openagent-infra's env var.

### Why is openagent-memory opt-in rather than required?

Memory is an enhancement, not a dependency of the core gateway. The gateway must keep serving `/chat` whether or not a RAG layer exists, so memory is enabled only by explicit configuration (`MEMORY_URL` + `MEMORY_API_KEY`) and is never a refuse-to-boot dependency. Deployments that don't want it pay nothing — full history is forwarded as before.

### Why is retrieve fail-open but ingest is not?

They sit on different paths. Retrieve is on the hot path, so a slow or down memory must degrade the prompt to "recent turns only" rather than delay the user's first token — fail-open is the only acceptable behaviour there. Ingest runs after the user already has their answer, so it can afford to surface a real failure signal (openagent-memory returns 503 when its embedder is down) so a dropped turn is visible rather than silent — but because it's off-path, surfacing that signal never costs the user anything.

### Why does prompt assembly live in openagent-api and not openagent-memory?

Separation of concerns. Memory's job is to embed a query and rank prior turns; the gateway's job is to decide what actually goes into the prompt — the persona, which retrieved turns to keep (deduped against the recent window), how many recent turns to keep verbatim, and the ordering. Keeping assembly in the gateway means memory can be swapped or reimplemented without touching prompt policy.

### Why no HMAC on the memory boundary (yet)?

openagent-memory uses transport-key auth only today and defines no HMAC contract. Rather than invent one prematurely, the MemoryClient scaffolds the signing helpers (mirroring the logger's canonical-payload contract) but leaves them inert, so adding HMAC later — once openagent-memory defines the verifier — is a localized change.

### Why is `session_id` an env var for now?

The reference stack doesn't yet have a frontend that mints and manages per-conversation ids. `MEMORY_SESSION_ID` is a deliberate stopgap: a single static value that scopes memory and populates the previously-null `session_id` correlation field on events. When the frontend gains session management, the id moves out of the environment and into the request — `ChatRequest` intentionally has no `session_id` field yet so that move is clean.

### Why SSE relay instead of buffering and returning JSON?

Buffering means a multi-minute spinner with no user feedback. Streaming gives them tokens as they arrive — the only sane UX for this latency profile.

### Why no conversation history at this layer?

Statelessness. openagent-api is deliberately a pure gateway with no per-session memory of its own. When memory is enabled, the durable store lives in openagent-memory's database, reached over HTTP — never in the gateway.

### Why httpx instead of requests or aiohttp?

`httpx` is async-native (FastAPI's event loop can `await` it), supports streaming exactly as the SSE pump needs, and has a familiar API.

### Why an in-memory queue instead of a durable outbox?

Simplicity vs. durability trade-off. An **in-memory `asyncio.Queue`** is zero infrastructure: no new dependencies, no new services, no new failure modes. The cost is that events queued in memory at the moment of a container restart are lost.

### Why is the `/health` endpoint authenticated?

It reports operational state. That's internal information.

### Why a non-root user in the container?

A container escape that gives root on the container should not give root on the host. The user (uid 1000) is created in the Dockerfile and the runtime drops to it before uvicorn starts.

### Why an unbounded read timeout?

A finite read timeout would kill long but legitimate generations. Connect timeout is short (10s) because if openagent-infra itself is unreachable, we want to fail fast.

### Why port 8001?

8000 = openagent-frontend (user-facing), 8001 = openagent-api (the API the frontend talks to), 8002 = openagent-infra (model layer, internal), 8003 = openagent-logger (capture layer, internal), 8004 = openagent-memory (RAG layer, internal, optional). The numbering reflects the request flow.

---

## Known Limitations

These are present-tense limitations I'm aware of and accept for now.

### No persistent conversation history in the gateway

History is held client-side by the frontend; when memory is enabled, a durable copy lives in openagent-memory's database. The gateway itself remembers nothing across requests. Closing the browser tab loses the frontend's in-session history; whether anything persists depends on whether memory is enabled.

### No per-user authentication

`OPENAGENT_API_KEY` is a shared secret between the frontend and the gateway, not a per-user credential, and `MEMORY_SESSION_ID` is a single static session scope, not a per-user identity. There is no "user A vs user B" at this layer. Not appropriate for exposure beyond a trusted operator without an auth layer in front.

### Memory is single-session and best-effort (when enabled)

Today `session_id` comes from a single static env var, so all traffic shares one memory session until the frontend manages conversations. Retrieval is fail-open: during a memory outage or embedder cold-start the prompt silently falls back to "recent turns only", dropping older context with no retrieval to compensate. Ingest can fail (logged as `memory_ingest_error`) and that turn won't be retrievable later. Memory's database also stores conversation content at rest — treat it as sensitive.

### No rate limiting or abuse protection

The gateway trusts authenticated clients. Safe because the frontend is the only client. If exposed more widely, rate limiting would belong at a reverse proxy in front of the gateway.

### Event capture is best-effort

The capture pipeline is fire-and-forget with an in-memory queue, so events can be lost if openagent-logger is unreachable when an event is emitted, or if the queue fills during a sustained outage (drop-oldest). `/chat` is unaffected by this — losing capture events never degrades the user-facing response.

### Context-window handling

Without memory, the gateway forwards whatever message list the frontend sends, without truncation — a long enough conversation will eventually hit the upstream model's context window and fail upstream. With memory enabled, the gateway caps the verbatim history at the recent-N window and relies on retrieval for older context, which mitigates this — but during a degraded retrieve it falls back to recent-N only, so older context can be dropped silently.

### Mid-stream upstream errors are in-band

Once SSE headers go out (HTTP 200), the gateway can no longer change the status code. Mid-stream upstream failures are surfaced as `data: [ERROR ...]` followed by `data: [DONE]`; the HTTP status stays 200. The corresponding `upstream_error` ops_event captures the operational signal regardless.

---

## Troubleshooting

### `🔐 401 Unauthorized` on every request
`OPENAGENT_API_KEY` in openagent-frontend's environment doesn't match `OPENAGENT_API_KEY` in this repo's `.env`.

### `🔌 502 / "Cannot reach openagent-infra"`
openagent-api cannot establish a TCP connection to `OPENAGENT_INFRA_URL`. Check if openagent-infra is running.

### `openagent-api` won't start: "OPENAGENT_API_KEY is required"
`.env` is missing or `OPENAGENT_API_KEY` is blank.

### `WARNING | openagent-logger returned HTTP 401`
Every event POST is returning 401 from openagent-logger. `LOGGER_API_KEY` mismatch or `LOGGER_HMAC_SECRET` mismatch.

### `WARNING | LoggerClient queue full (max=1000); dropped oldest event`
The in-memory queue has reached its capacity and is shedding old events to make room for new ones. openagent-logger is down/slow, or there is a massive burst of traffic.

### Events aren't showing up in openagent-logger even though /chat works
The fire-and-forget contract means /chat works regardless of whether events land. Check openagent-api's logs for `WARNING | LoggerClient POST failed`.

### `memory_retrieve_degraded` events / answers missing older context
Retrieval is failing open. openagent-memory is unreachable, or its embedder is cold/down, so the prompt fell back to "recent turns only". Check that openagent-memory is up at `MEMORY_URL` and its embedder is warm. This never fails `/chat` — it only degrades answer quality.

### `memory_ingest_error` events / turns not being remembered
A background ingest failed — most commonly openagent-memory answering 503 because its embedder is unavailable. The turn was not stored and won't be retrievable later. `/chat` is unaffected. Check openagent-memory's logs and embedder health.

### Memory is enabled but nothing is ever retrieved or ingested
Most likely `MEMORY_SESSION_ID` is unset — openagent-api logs a one-line WARNING at startup and forwards full history without touching memory. Set a session id. (On the very first turn of a session there is also nothing stored yet, so retrieval is legitimately empty.)

### `401` from openagent-memory on every /retrieve and /ingest
`MEMORY_API_KEY` doesn't match openagent-memory's `MEMORY_API_KEY`. Copy the value from openagent-memory's `.env` byte-for-byte.

### `HTTP 422` on /chat with a valid request
Most common cause: invalid `reasoning_effort` value. Pydantic only accepts `low`, `medium`, or `high`.

### Port 8001 already in use
Change the left side of `"8001:8001"` in `docker-compose.yml` to a free port.

### Healthcheck shows `(unhealthy)` in `docker ps`
The container's HEALTHCHECK probes `/health` with the X-API-Key from `OPENAGENT_API_KEY`. If that env var isn't set, the probe fails. (Note `/health` reflects only openagent-infra readiness — a down logger or memory does not make the container unhealthy.)

### Reasoning chain shows up as JSON instead of clean text in the chat bubble
The frontend isn't decoding the OpenAI ChatCompletion chunk wrapper. This is a frontend bug, not a gateway bug.

---

## License

Copyright © 2026 William McKeon.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

```text
http://www.apache.org/licenses/LICENSE-2.0
```

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.

---

## Maintainer

**William McKeon** ([github.com/william-mckeon](https://github.com/william-mckeon))