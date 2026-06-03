# openagent-api — Datasheet

> Reference document for building on top of, or integrating with, openagent-api.
> Intended audience: developers and integrators needing to understand what openagent-api
> is, what it owns, and how it talks to the rest of the system.

---

## Quick Reference

| Item | Value |
|---|---|
| Role | Identity Gateway for OpenAgent |
| Framework | FastAPI |
| Language | Python 3.11 |
| Protocol out (downstream) | HTTP/1.1 + Server-Sent Events (SSE relayer) |
| Protocol in (upstream) | HTTP/1.1 + Server-Sent Events |
| Port | `8001` |
| Upstream Model Proxy | `openagent-infra` (`OPENAGENT_INFRA_URL`, typically `:8002`) |
| Upstream Logger | `openagent-logger` (`LOGGER_URL`, typically `:8003`) |
| Auth in | `X-API-Key: OPENAGENT_API_KEY` |
| Auth out (model) | `X-API-Key: INFRA_API_KEY` |
| Auth out (logger) | `X-API-Key: LOGGER_API_KEY` + HMAC-SHA256 signature |
| Persona | `src/prompt/bio.txt` |
| Version | 1.0.0 |

---

## Overview

`openagent-api` is the product-layer backend in the OpenAgent system. It is the only HTTP boundary between `openagent-frontend` and `openagent-infra`, and it emits structured events to `openagent-logger`.

It owns four concerns:
1. **The persona** — the system prompt that defines who the agent is.
2. **The compartmentalized auth chain** — validating inbound UI requests and authenticating outbound model and logging requests.
3. **The SSE relay** — a transparent pipe from the model proxy's token stream to the frontend.
4. **The fire-and-forget logger emission** — asynchronously sending ops and capture events to the logger.

---

## Where This Service Fits

```text
┌──────────────────────────────────────────────────────────────┐
│                    Browser (user)                            │
│                 http://localhost:8000                        │
└───────────────────────────┬──────────────────────────────────┘
                            │
                            ▼
┌──────────────────────────────────────────────────────────────┐
│    openagent-frontend                                        │
│    Streamlit on :8000                                        │
└───────────────────────────┬──────────────────────────────────┘
                            │ HTTP POST /chat   (SSE response)
                            │ HTTP GET  /health
                            │ X-API-Key: OPENAGENT_API_KEY
                            ▼
┌──────────────────────────────────────────────────────────────┐
│    openagent-api    ←── YOU ARE READING THIS DATASHEET       │
│    FastAPI gateway on :8001                                  │
│                                                              │
│    Owns: persona, auth chain, OpenAI messages list           │
│          construction, SSE relay, event emission             │
└───────┬────────────────────────────────────────────┬─────────┘
        │ HTTP POST /chat                            │ HTTP POST /events
        │ X-API-Key: INFRA_API_KEY                   │ X-API-Key: LOGGER_API_KEY
        │                                            │ + HMAC Signature
        ▼                                            ▼
┌─────────────────────────────┐        ┌─────────────────────────────┐
│    openagent-infra          │        │    openagent-logger         │
│    FastAPI proxy on :8002   │        │    Capture layer on :8003   │
│    → BYOC Compute Provider  │        │    → PostgreSQL             │
└─────────────────────────────┘        └─────────────────────────────┘
```

---

## What This Service Owns

### 1. The Persona (System Prompt)

The system prompt lives in `src/prompt/bio.txt`. `openagent-api` reads this file once at startup and prepends it as the first `{"role": "system", "content": ...}` message on every `/chat` call.

If the frontend attempts to send its own system message, `openagent-api` drops it with a warning. This ensures the frontend cannot override the canonical agent identity.

### 2. Compartmentalized Auth Chain

`openagent-api` validates `OPENAGENT_API_KEY` on inbound requests. It uses `INFRA_API_KEY` when calling the model proxy, and `LOGGER_API_KEY` + `LOGGER_HMAC_SECRET` when calling the logger. Keys are not passed through unchanged.

### 3. SSE Relay & Side-Channel Parsing

Tokens received from `openagent-infra` stream back to the frontend byte-for-byte. `openagent-api` adds zero latency to this visible stream.

Simultaneously, an internal side-channel parser accumulates `delta.content` tokens. When the stream completes, the assembled text is submitted to `openagent-logger` as a `conversation_capture`.

### 4. Fire-and-Forget Event Emission

Every `/chat` request emits up to five events to `openagent-logger`:
- `request_received`
- `upstream_call`
- `upstream_error` (if applicable)
- `stream_complete`
- `conversation_capture`

Emission is asynchronous via an in-memory queue. The `/chat` hot path never blocks waiting for the logger.

---

## API Reference

### `POST /chat`

Forward a list of user/assistant turns and receive a streamed response via Server-Sent Events.

**Request:**
```text
POST /chat
Content-Type: application/json
X-API-Key: <OPENAGENT_API_KEY>

{
  "messages": [
    {"role": "user", "content": "Hello"}
  ],
  "reasoning_effort": "medium"
}
```

The `reasoning_effort` field is optional. If omitted, the default is applied upstream.

**Response:** `text/event-stream`

Streams OpenAI ChatCompletion chunks. Reasoning tokens arrive in `choices[0].delta.reasoning`, followed by content tokens in `choices[0].delta.content`.

### `GET /health`

Proxied health check. 

**Request:**
```text
GET /health
X-API-Key: <OPENAGENT_API_KEY>
```

**Response:**
```json
{
  "status": "ok" | "loading" | "unreachable",
  "openagent_api": {"version": "...", "identity_loaded": true},
  "openagent_infra": {"url": "...", "status": "...", "raw": {...}}
}
```

---

## Configuration

| Variable | Required | Description |
|---|---|---|
| `OPENAGENT_API_KEY` | Yes | Inbound auth secret |
| `INFRA_API_KEY` | Yes | Outbound auth secret to openagent-infra |
| `OPENAGENT_INFRA_URL` | Yes | Base URL of openagent-infra |
| `LOGGER_URL` | Yes | Base URL of openagent-logger |
| `LOGGER_API_KEY` | Yes | Outbound transport secret to openagent-logger |
| `LOGGER_HMAC_SECRET` | Yes | Payload-signing secret for events |
| `OPENAGENT_LOGGER_QUEUE_MAX_SIZE` | No | Max pending events before drop-oldest (default 1000) |
| `OPENAGENT_BIO_PATH` | No | Path to persona file (default `/app/src/prompt/bio.txt`) |

---

## Integration Notes for Other Services

### For openagent-frontend

`openagent-frontend` interacts solely with this service. It relies on the `/chat` SSE stream and polls `/health` during cold starts.

### For openagent-infra

`openagent-api` is the primary client of `openagent-infra`. It passes the `INFRA_API_KEY` and forwards the assembled messages list.

### For openagent-logger

`openagent-api` emits events via HMAC-signed POST requests to `openagent-logger/events`. If the logger is unreachable, `openagent-api` drops events locally (drop-oldest queue policy) rather than crashing the chat.

---

## Design Decisions

### Why fire-and-forget logging?

The /chat hot path must not block on the capture layer. Adding HTTP round-trips to the DB write path would add latency to every chat turn and couple gateway reliability to logger reliability.

### Why side-channel parsing?

To capture the final assistant response for logging without delaying the token stream to the user.

### Why doesn't the gateway manage conversation state?

`openagent-api` is stateless across requests. Moving state to the gateway requires a persistent datastore, which complicates scaling. The frontend manages session state for now.

---

## License

Copyright © 2026 William McKeon.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
