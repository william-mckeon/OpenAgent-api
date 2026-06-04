# ============================================================================
# openagent-api - Dockerfile
# Maintainer: William McKeon
# The identity gateway for OpenAgent
# ============================================================================
#
# ROLE:
#   Builds the runtime image for the openagent-api service. Produces a lean
#   pure-Python container with FastAPI + uvicorn + httpx and the persona
#   (bio.txt) baked in. No GPU, no CUDA, no compilers — this service does no
#   inference work. All it does is auth, identity injection, SSE relay between
#   openagent-frontend and openagent-infra, and fire-and-forget event emission
#   to openagent-logger.
#
# IMAGE SIZE TARGET:
#   ~150-200 MB total. Pure-Python, slim base. Comparable to the
#   openagent-frontend image and to openagent-infra (which is also a thin
#   FastAPI proxy). The logger and infra clients add zero MB to the image:
#   they use Python stdlib (hmac, hashlib, json, logging) for signing and
#   diagnostics and reuse the already-installed httpx for HTTP, so no new
#   pip packages, no new apt packages, and no new compilers are needed.
#
# BASE IMAGE CHOICE:
#   python:3.11-slim — same Python major.minor as openagent-frontend so the
#   two product-layer services run on identical interpreters. openagent-infra
#   runs on python:3.12-slim per its own Dockerfile (it is also a pure-Python
#   proxy with no GPU stack). openagent-api stays on 3.11 to match
#   openagent-frontend rather than openagent-infra because openagent-api and
#   openagent-frontend are deployed and operated together as the product layer.
#
# BUILD CONTEXT:
#   Build from the repo root:
#     docker build -f docker/api/Dockerfile -t openagent-api:latest .
#   docker-compose handles this automatically via the build: directive
#   in docker-compose.yml.
#
# LAYER ORDERING:
#   Layers are ordered from least-frequently-changed to most-frequently
#   changed so the Docker layer cache reuses as much as possible on
#   rebuilds:
#     1. OS package install        — almost never changes
#     2. requirements.txt copy     — changes when deps change
#     3. pip install               — re-runs only when requirements.txt changes
#     4. bio.txt copy              — changes when the persona is rotated
#     5. src/client/ copy          — changes when an outbound client is
#                                    added or modified
#     6. application source copy   — changes on every code edit
#   This means a typical code-only change to api.py rebuilds in seconds
#   because layers 1-5 are pulled from the cache. Editing a client module
#   (client/logger.py or client/infra.py) invalidates layers 5-6 only.
#   Editing bio.txt invalidates 4-6.
#
# REBUILD TRIGGERS:
#   Changing src/backend/api.py    → fast rebuild (last COPY layer only)
#   Changing src/client/*.py        → rebuild last two COPY layers
#   Changing src/prompt/bio.txt     → rebuild last three COPY layers
#   Changing requirements.txt       → slower (pip install re-runs)
#   Changing this Dockerfile        → full rebuild
#
# NON-ROOT USER:
#   The container runs as a dedicated 'openagent' user (uid 1000) rather
#   than root. This matches the security posture of the other services
#   and means a container escape does not give root on the host.
#
# HEALTHCHECK:
#   Docker-level HEALTHCHECK uses the same /health endpoint the frontend
#   polls. It supplies the X-API-Key header from the OPENAGENT_API_KEY env
#   var because /health requires auth (per api.py design). curl is installed
#   minimally for this purpose.
#
#   /health requires only OPENAGENT_API_KEY. The three logger env vars
#   (LOGGER_URL, LOGGER_API_KEY, LOGGER_HMAC_SECRET) are validated inside
#   api.py's lifespan startup; if any is missing the container refuses to
#   boot and HEALTHCHECK is never reached. If HEALTHCHECK reports healthy,
#   all six runtime secrets/URLs the gateway needs are by definition present
#   and valid. The /health endpoint's upstream probe is handled by
#   InfraClient.check_health(); the endpoint contract — auth requirement,
#   response shape, status vocabulary — is independent of that internal call.
#
# PORT:
#   Container listens on 8001 (per the port convention in api.py's
#   architecture diagram: 8000=frontend, 8001=openagent-api,
#   8002=openagent-infra, 8003=openagent-logger). docker-compose.yml maps
#   host 8001 → container 8001.
#
# RUNTIME ENV VARS EXPECTED FROM docker-compose.yml / .env:
#   OPENAGENT_API_KEY    — inbound auth (validated by /chat and /health,
#                          also used by HEALTHCHECK in this Dockerfile)
#   INFRA_API_KEY        — outbound to openagent-infra (same value as
#                          openagent-infra's own API_KEY)
#   OPENAGENT_INFRA_URL  — openagent-infra base URL
#   LOGGER_URL           — openagent-logger base URL
#   LOGGER_API_KEY       — outbound to openagent-logger
#   LOGGER_HMAC_SECRET   — payload-signing secret
#   Plus optional tunables (OPENAGENT_LOG_LEVEL, OPENAGENT_FRONTEND_URL,
#   OPENAGENT_UPSTREAM_CONNECT_TIMEOUT, OPENAGENT_UPSTREAM_READ_TIMEOUT,
#   OPENAGENT_HEALTH_TIMEOUT, OPENAGENT_BIO_PATH,
#   OPENAGENT_LOGGER_QUEUE_MAX_SIZE).
# ============================================================================

# ----------------------------------------------------------------------------
# BASE IMAGE
# ----------------------------------------------------------------------------
# python:3.11-slim is a Debian-based slim image with Python 3.11 only and
# no extra tooling. Matches openagent-frontend's base image so both product-
# layer services run on identical interpreters and base OS.
FROM python:3.11-slim

# ----------------------------------------------------------------------------
# IMAGE METADATA
# ----------------------------------------------------------------------------
# OCI labels make the image self-describing for registries, scanners, and
# operators running `docker inspect`.
LABEL org.opencontainers.image.title="openagent-api"
LABEL org.opencontainers.image.description="Identity gateway for OpenAgent — SSE relay between openagent-frontend and openagent-infra, with fire-and-forget event emission to openagent-logger"
LABEL org.opencontainers.image.vendor="William McKeon"
LABEL org.opencontainers.image.version="1.0.0"
LABEL org.opencontainers.image.licenses="Apache-2.0"

# ----------------------------------------------------------------------------
# PYTHON RUNTIME ENVIRONMENT
# ----------------------------------------------------------------------------
# PYTHONDONTWRITEBYTECODE=1   Don't write .pyc files to disk — saves space
#                              and avoids stale bytecode warnings on rebuild.
# PYTHONUNBUFFERED=1          Don't buffer stdout — log lines appear in
#                              `docker logs` immediately, matching the
#                              behaviour the other two services rely on.
# PYTHONPATH=/app/src         Lets imports resolve as `from backend.api ...`,
#                              `from client.logger ...`, and
#                              `from client.infra ...` per the import path
#                              rule in api.py's header.
# PIP_NO_CACHE_DIR=1          pip doesn't cache wheels — keeps the image
#                              small. We won't reinstall in this container.
# PIP_DISABLE_PIP_VERSION_CHECK=1  Suppresses the pip self-update warning
#                              on every invocation.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# ----------------------------------------------------------------------------
# OS PACKAGES
# ----------------------------------------------------------------------------
# We install only what the container truly needs at runtime:
#   curl   — required by the HEALTHCHECK directive below. Without it,
#            Docker's healthcheck cannot probe /health and the container
#            shows as unhealthy in `docker ps` even when fully working.
# ca-certificates is already present in python:3.11-slim, so we don't
# install it explicitly.
#
# `--no-install-recommends` keeps the install minimal. The apt cache is
# cleared in the same RUN layer to avoid bloating the image. No additional
# apt packages are needed: the logger and infra clients sign and call out
# using Python stdlib (hmac, hashlib, json, logging) and the already-present
# httpx — no cryptography wheel, no compilers, no OpenSSL beyond what
# python:3.11-slim already provides for httpx's TLS.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# ----------------------------------------------------------------------------
# WORKING DIRECTORY
# ----------------------------------------------------------------------------
# /app is the convention used by openagent-infra and openagent-frontend.
# Source code lives at /app/src/, matching the in-repo layout.
WORKDIR /app

# ----------------------------------------------------------------------------
# NON-ROOT USER
# ----------------------------------------------------------------------------
# Create the 'openagent' user up front so we can chown directories as we
# create them. uid/gid 1000 is the conventional first non-system user on
# Debian and matches host-side defaults so volume permissions work without
# surprises.
#
# We don't switch to this user yet — installs as root, then USER below
# drops privilege for the runtime.
RUN groupadd --system --gid 1000 openagent \
    && useradd --system --uid 1000 --gid openagent --create-home --shell /bin/bash openagent

# ----------------------------------------------------------------------------
# PYTHON DEPENDENCIES
# ----------------------------------------------------------------------------
# Copy requirements.txt FIRST and install in its own layer so changes to
# application source don't invalidate the pip-install cache. This is the
# single biggest reason rebuilds are fast. The logger and infra clients
# reuse the existing httpx dependency; HMAC signing uses Python stdlib
# (hmac, hashlib, json) which ships with python:3.11-slim.
COPY requirements.txt /app/requirements.txt
RUN pip install --upgrade pip \
    && pip install -r /app/requirements.txt

# ----------------------------------------------------------------------------
# PERSONA
# ----------------------------------------------------------------------------
# bio.txt is baked into the image at build time per the design decision in
# api.py — it is loaded once at startup by load_identity() and prepended as
# the first system message on every /chat call.
#
# Path inside the container is /app/src/prompt/bio.txt, which matches the
# default OPENAGENT_BIO_PATH in api.py's Config class. Operators who want to
# override this can mount a volume over /app/src/prompt/ in
# docker-compose.yml or set OPENAGENT_BIO_PATH to a different path.
COPY src/prompt/ /app/src/prompt/

# ----------------------------------------------------------------------------
# OUTBOUND HTTP CLIENTS
# ----------------------------------------------------------------------------
# src/client/ holds the modules openagent-api uses to talk to upstream
# services. Two clients live here:
#   client/logger.py  — fire-and-forget event emission to openagent-logger
#   client/infra.py   — openagent-infra streaming relay + health proxy
# The single `COPY src/client/ /app/src/client/` directive below picks up
# both files because it copies the entire directory; further clients added
# to the directory are picked up automatically, with no Dockerfile change.
#
# Placed BEFORE the src/backend/ COPY because client modules change less
# frequently than the FastAPI app. A typical edit to api.py invalidates only
# the next COPY layer; src/client/ stays cached. Conversely, an edit to a
# client module invalidates this layer and the next one, leaving everything
# before this point cached.
COPY src/client/ /app/src/client/

# ----------------------------------------------------------------------------
# APPLICATION SOURCE
# ----------------------------------------------------------------------------
# Copy the FastAPI app last so a typical edit to api.py only invalidates this
# single layer. Subsequent rebuilds reuse all preceding layers from cache.
#
# We deliberately do NOT use a wildcard `COPY src/ /app/src/` because that
# would couple bio.txt, the client modules, and the backend source into a
# single cache layer, defeating the layering. Three explicit COPY directives
# keep them independently invalidated.
COPY src/backend/ /app/src/backend/

# ----------------------------------------------------------------------------
# OWNERSHIP
# ----------------------------------------------------------------------------
# Hand /app to the non-root user so the runtime process can read its own
# files. Done in one layer at the end so the chown happens after every COPY,
# ensuring no root-owned files remain.
RUN chown -R openagent:openagent /app

# ----------------------------------------------------------------------------
# DROP PRIVILEGES
# ----------------------------------------------------------------------------
# From here on, every process in the container runs as 'openagent'.
USER openagent

# ----------------------------------------------------------------------------
# NETWORK
# ----------------------------------------------------------------------------
# Document the listening port. EXPOSE is informational — it does not publish
# the port to the host. docker-compose.yml does that. Port 8001 per the
# architecture diagram in api.py.
EXPOSE 8001

# ----------------------------------------------------------------------------
# HEALTHCHECK
# ----------------------------------------------------------------------------
# Docker probes /health every 30 seconds once the container has been running
# for 10 seconds. Three consecutive failures mark the container unhealthy.
#
# /health requires the X-API-Key header (per api.py design) so the probe
# pulls OPENAGENT_API_KEY from the container environment. The env var is set
# by docker-compose.yml from .env, so it is available inside the container by
# the time HEALTHCHECK fires.
#
# Why not just check that the port is open? Because that would mark the
# container healthy even when openagent-infra is unreachable and the gateway
# is effectively useless. /health does a real upstream probe (via
# InfraClient.check_health()) and returns `status: ok | loading | unreachable`
# — exactly the signal we want.
#
# We accept any status the endpoint reports as "healthy from openagent-api's
# perspective" because the container itself is functioning correctly even
# when openagent-infra is loading (e.g. a serverless provider worker
# cold-starting). The frontend's gate-open loop handles the ok/loading
# distinction at the user-facing layer.
#
# HEALTHCHECK requires only OPENAGENT_API_KEY. The three logger env vars
# (LOGGER_URL, LOGGER_API_KEY, LOGGER_HMAC_SECRET) are validated in api.py's
# lifespan startup — if any is missing, the container refuses to boot and
# this HEALTHCHECK is never reached. Therefore a passing HEALTHCHECK implies
# all six runtime secrets/URLs are present and the container has successfully
# wired up both the LoggerClient and the InfraClient.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl --silent --fail \
        --header "X-API-Key: ${OPENAGENT_API_KEY}" \
        http://localhost:8001/health \
        || exit 1

# ----------------------------------------------------------------------------
# ENTRYPOINT
# ----------------------------------------------------------------------------
# Run uvicorn directly (not via shell) so signals reach the Python process
# unchanged. SIGTERM from `docker stop` triggers FastAPI's lifespan shutdown
# handler in api.py, which:
#   - Stops the LoggerClient (draining its queue with timeout)
#   - Stops the InfraClient (closing its internal httpx.AsyncClient cleanly)
# Both stops happen during the lifespan shutdown sequence so any pending
# logger events get a final drain attempt and the openagent-infra connection
# pool is released before the process exits.
#
# --host 0.0.0.0  Bind to all interfaces inside the container so
#                 docker-compose's port mapping reaches it.
# --port 8001     Match EXPOSE above and the architecture diagram.
# --proxy-headers Trust X-Forwarded-* headers when running behind a
#                 reverse proxy (nginx, traefik). Harmless when not.
# --no-access-log Disable uvicorn's per-request access log because api.py
#                 emits its own structured INFO line per /chat call. Two log
#                 lines per request would be noise. Application logs
#                 (logger.info / .warning / .error) still flow through.
CMD ["uvicorn", "backend.api:app", \
     "--host", "0.0.0.0", \
     "--port", "8001", \
     "--proxy-headers", \
     "--no-access-log"]

# ============================================================================
# END OF FILE
# ============================================================================
