# ============================================================================
# openagent-api - Client Package
# Maintainer: William McKeon
# Outbound HTTP clients for upstream services
# ============================================================================
#
# ROLE:
#   This package holds the outbound client modules openagent-api uses to
#   talk to its upstream dependencies. One module per upstream service,
#   named by the service being called:
#
#     client/logger.py  — fire-and-forget client for openagent-logger
#     client/infra.py   — streaming client for openagent-infra
#     client/memory.py  — session-scoped RAG client for openagent-memory
#                         (optional; used only when memory is configured)
#
# IMPORT PATH:
#   PYTHONPATH inside the container is /app/src per Dockerfile, so modules
#   in this directory are importable as:
#
#     from client.logger import LoggerClient
#     from client.infra import InfraClient
#     from client.memory import MemoryClient
#
#   That matches the existing `from backend.api import app` style used in
#   the Dockerfile CMD.
#
# WHY A SEPARATE PACKAGE FROM backend/:
#   backend/ holds the FastAPI application — endpoint handlers, lifespan,
#   the inbound side of the gateway. client/ holds outbound HTTP code — the
#   things openagent-api CALLS rather than the things it SERVES. Keeping
#   them in separate packages makes the dependency direction explicit
#   (backend depends on client; client never depends on backend) and makes
#   testing easier (client modules can be mocked from backend's tests
#   without circular imports).
# ============================================================================