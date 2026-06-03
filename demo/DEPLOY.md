# Deploying The Company Brain — public sandbox

`sandbox_server.py` is the **hardened, public-hosting** variant of the demo
backend. Unlike `brain_server.py` (local-dev convenience, longer timeouts, no
limits) the sandbox is built to sit on the open internet in front of a shared
Orbital instance without getting abused. This doc covers what it needs, how to
run it, and why the guard-rails are shaped the way they are.

## What it serves

Exactly three fixed, parameterised query shapes — no arbitrary TaxiQL:

| Endpoint                         | Param(s)        | Backed by (via Orbital)                          |
| -------------------------------- | --------------- | ------------------------------------------------ |
| `GET /api/brain?name=`           | company name    | GLEIF + Wikidata + Open-Meteo + Police.uk + EA   |
| `GET /api/trading?ticker=`       | ticker          | Yahoo Finance (`markets.StockQuote`)             |
| `GET /api/coding?owner=&repo=`   | owner, repo     | GitHub (`coding.GithubRepo`)                     |
| `GET /healthz`                   | —               | liveness/readiness (cache stats), rate-exempt    |

`/` serves `demo/index.html` if present, else a small JSON banner. `POST` is
**405** by design — there is no surface that accepts a client-supplied query.

## The Orbital dependency

The sandbox is a thin, safe **proxy**. It does not talk to any upstream API
directly; it forwards server-authored TaxiQL to an Orbital stack and returns the
result. Orbital must be reachable at `ORBITAL_URL` (its `/api/taxiql` endpoint).

- Orbital is a stateful service that file-watches `build/gov-uk/taxi/*.taxi`.
  Deploy/keep it running separately; the sandbox is stateless and horizontally
  scalable in front of it.
- If Orbital is unreachable, seeded company/ticker/repo lookups still answer
  from the warm cache (see below); everything else returns `502`/`504` JSON.

## Environment variables

| Var                   | Default                          | Purpose                                            |
| --------------------- | -------------------------------- | -------------------------------------------------- |
| `ORBITAL_URL`         | `http://localhost:9022`          | Base URL of the Orbital stack.                     |
| `SANDBOX_HOST`        | `127.0.0.1` (`0.0.0.0` in Docker)| Bind interface.                                    |
| `SANDBOX_PORT`        | `8905`                           | Bind port.                                         |
| `CACHE_TTL`           | `300`                            | Seconds a cached lookup stays fresh.               |
| `CACHE_MAX`           | `2000`                           | Max cache entries (oldest evicted).                |
| `RATE_CAPACITY`       | `30`                             | Per-IP token-bucket burst size.                    |
| `RATE_REFILL_PER_SEC` | `0.5`                            | Per-IP refill rate (≈30 req/min steady-state).     |
| `UPSTREAM_TIMEOUT`    | `20`                             | Per-query timeout against Orbital (seconds).       |
| `WARM_CACHE`          | `1`                              | Set `0` to skip seeding the cache on boot.         |
| `TRUST_XFF`           | `0`                              | Set `1` ONLY behind a proxy you control (see below).|

## Run it

### Directly (stdlib only, no install)

```bash
ORBITAL_URL=http://localhost:9022 \
SANDBOX_HOST=0.0.0.0 SANDBOX_PORT=8905 \
python3 demo/sandbox_server.py
```

### Docker

```bash
# build context is the repo root so the image can COPY ./demo
docker build -t company-brain-sandbox -f demo/Dockerfile .

docker run --rm -p 8905:8905 \
  -e ORBITAL_URL=http://host.docker.internal:9022 \
  company-brain-sandbox

curl 'http://localhost:8905/api/trading?ticker=AAPL'
```

On Linux hosts without `host.docker.internal`, point `ORBITAL_URL` at the
Orbital container's address (e.g. a shared Docker network or its service DNS
name) instead.

## Why the hardening (rationale)

- **Caching (TTL, in-memory).** A live demo gets bursty, repetitive traffic —
  everyone clicks "TESCO PLC". A 5-minute TTL cache makes repeats instant and,
  crucially, **shields flaky free upstreams** (GLEIF, Police.uk, GDELT) and your
  egress quota from the crowd. It is per-process and self-evicting (`CACHE_MAX`),
  so memory is bounded and no external store (Redis) is required.
- **Seeded allow-list / warm cache.** On boot a background thread pre-loads ~12
  well-known UK companies (plus a few tickers/repos) from live Orbital, falling
  back to **baked public-register snapshots** (LEI + Companies House number +
  registered office) when an upstream is down. The headline demo therefore always
  answers instantly, even during an upstream outage. Seeded entries get a long
  TTL (≥1 day) so they survive flaky windows.
- **Per-IP rate limit (token bucket).** Prevents a single client from draining
  the shared Orbital backend or your API quotas. Burst `RATE_CAPACITY`, steady
  `RATE_REFILL_PER_SEC`; over-limit requests get `429` + `Retry-After`. Idle
  buckets are garbage-collected so the limiter's memory stays bounded.
- **Fixed parameterised queries only.** Query templates live server-side; user
  input is **whitelisted** (`clean_name`/`clean_ticker`/`clean_repo`) before
  substitution so it can't escape the TaxiQL string literal, and there is no
  endpoint that proxies raw TaxiQL. This is the single most important control:
  the public can never run an arbitrary federated query through your router.
- **Request timeouts.** Every upstream call has a hard `UPSTREAM_TIMEOUT`, so a
  slow API returns `504` instead of pinning a worker thread indefinitely.
- **Hardening headers + non-root container.** `nosniff`, `X-Frame-Options:DENY`,
  `Referrer-Policy:no-referrer`; runs as UID 10001 in the image.

## Key / secret safety

- The sandbox **holds no API keys**. All credentials (e.g. Companies House,
  NHS) live in Orbital's auth config, server-side, and never reach the browser
  or this proxy. Keep them out of the image and out of `ORBITAL_URL`.
- The demo's live chain is deliberately built from **no-auth** upstreams (GLEIF,
  Postcodes.io, Police.uk, Environment Agency, Open-Meteo, Wikidata, Yahoo,
  public GitHub), so the public sandbox needs zero secrets to function.
- Do not enable `TRUST_XFF` unless the sandbox sits behind a proxy/load-balancer
  **you control** that sets `X-Forwarded-For`; otherwise clients can spoof it to
  evade the per-IP rate limit.
- Same-origin by default (no `Access-Control-Allow-Origin`). If you must allow
  cross-origin browser calls, terminate that at a reverse proxy you trust rather
  than widening CORS in the app.

## Recommended production posture

Put a TLS-terminating reverse proxy (Caddy/nginx/Cloud Run) in front, keep
Orbital on a private network reachable only by the sandbox, and run multiple
stateless sandbox replicas if needed — they share nothing but the (separate)
Orbital backend. The container `HEALTHCHECK` and `/healthz` endpoint support
orchestrator liveness/readiness probes.
