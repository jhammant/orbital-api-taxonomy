# The Company Brain — public sandbox container (Phase 9)
#
# Builds a tiny, dependency-free image that runs the HARDENED sandbox backend
# (demo/sandbox_server.py). It serves only fixed, parameterised TaxiQL queries
# and proxies them to an Orbital stack reachable at $ORBITAL_URL.
#
#   docker build -t company-brain-sandbox -f demo/Dockerfile .
#   docker run --rm -p 8905:8905 \
#       -e ORBITAL_URL=http://host.docker.internal:9022 \
#       company-brain-sandbox
#
# Then: curl 'http://localhost:8905/api/trading?ticker=AAPL'
#
# Build context must be the repo root (so this COPY sees ./demo). The -f flag
# above points at this file while keeping the context at the project root.

FROM python:3.12-slim

# No third-party packages: the server is pure stdlib. We only set sane defaults.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    SANDBOX_HOST=0.0.0.0 \
    SANDBOX_PORT=8905 \
    ORBITAL_URL=http://host.docker.internal:9022 \
    CACHE_TTL=300 \
    RATE_CAPACITY=30 \
    RATE_REFILL_PER_SEC=0.5 \
    UPSTREAM_TIMEOUT=20

WORKDIR /app

# Copy the demo assets (sandbox_server.py + index.html if you want the SPA).
COPY demo/ /app/demo/

# Run as an unprivileged user — least privilege for a public-facing process.
RUN useradd --create-home --uid 10001 sandbox \
    && chown -R sandbox:sandbox /app
USER sandbox

EXPOSE 8905

# Container-level healthcheck hits the unauthenticated, rate-exempt probe.
HEALTHCHECK --interval=30s --timeout=4s --start-period=10s --retries=3 \
    CMD python3 -c "import os,urllib.request,sys; \
url='http://127.0.0.1:%s/healthz' % os.environ.get('SANDBOX_PORT','8905'); \
sys.exit(0 if urllib.request.urlopen(url, timeout=3).status==200 else 1)"

CMD ["python3", "demo/sandbox_server.py"]
