# Ledgerlock API image.
#
# Two stages so that build tooling (compilers, pip's caches, wheels) never
# reaches the runtime image. The runtime layer carries the interpreter, the
# installed dependencies, and the application: nothing else.

# ---------------------------------------------------------------------
# Stage 1: build the virtual environment
# ---------------------------------------------------------------------
# Pinned to the same patch version the test suite runs on locally, so
# "works on my machine" and "works in the container" mean the same thing.
FROM python:3.12.9-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

# Dependencies are copied and installed before the application code, so a
# change to a source file does not invalidate the (slow) dependency layer.
COPY requirements.txt ./
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip \
    && /opt/venv/bin/pip install -r requirements.txt

# ---------------------------------------------------------------------
# Stage 2: runtime
# ---------------------------------------------------------------------
FROM python:3.12.9-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

# Run as an unprivileged user. The application never needs to write to the
# filesystem: it has no uploads, no local cache, and logs to stdout.
RUN groupadd --system --gid 1001 ledgerlock \
    && useradd --system --uid 1001 --gid ledgerlock --no-create-home ledgerlock

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
COPY --chown=ledgerlock:ledgerlock app ./app

USER ledgerlock

EXPOSE 8000

# Liveness from inside the container, using the interpreter that is already
# present rather than adding curl to the image for one purpose. Hits /health,
# which deliberately does not touch MongoDB: a container should not be
# reported unhealthy (and restarted) because the database is briefly away.
# Readiness, which does check for a transaction-capable primary, is what the
# Kubernetes readinessProbe uses in Phase 7.
HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0) if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else sys.exit(1)"]

# One uvicorn worker per container. Scaling is horizontal (compose replicas,
# or the Kubernetes Deployment and HPA in Phase 7), which keeps each
# container single-purpose and means the in-process rate limiter's
# known limitation is stated in one place rather than varying with a worker
# count. See MEMORY.md.
CMD ["uvicorn", "app.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--workers", "1", \
     "--no-access-log"]
