# YouTube Upload Service

Publishes rendered videos to YouTube using per-tenant Google OAuth2 credentials and asynchronous, resumable upload jobs. Port 6315.

## What YouTube Upload Service Does

This service is the final publish step of the w2v pipeline. When the orchestrator finishes rendering a video, it calls `POST /api/upload` with a `file_source` (a local path or a URL to a rendered artifact in MinIO/storage_service) plus the target YouTube metadata — title, description, tags, category, and privacy status. The service records an `upload_jobs` row, hands the work to a bounded background worker pool, and returns a `job_id` immediately so the caller never blocks on the (potentially multi-minute, multi-gigabyte) transfer. Callers poll `GET /api/upload/jobs/{job_id}` to watch the job move through `pending → processing → completed | failed`.

Publishing to YouTube requires Google OAuth2 authorization, and this service manages those credentials on a per-tenant basis. A tenant connects a channel by calling `POST /api/auth/connect`, which mints a one-time `state` token (stored in Redis for 600s), returns a Google consent URL, and — after the operator grants consent — receives the redirect at the public `GET /api/auth/callback`. The callback exchanges the authorization code for access and refresh tokens, fetches the channel's id and title, and persists the credentials in the `oauth_tokens` table with the access and refresh tokens Fernet-encrypted at rest. When an upload runs, the worker loads that tenant's stored refresh token, transparently refreshes the access token if it has expired, and streams the file to the YouTube Data API v3 in resumable 10 MB chunks with exponential-backoff retries. Custom thumbnails and soft closed-caption tracks are attached best-effort after a successful insert; because a thumbnail needs a verified channel and captions need the `youtube.force-ssl` scope, a failure there is swallowed and never fails the upload.

The service tracks two distinct tenant identities on each job and must not conflate them. `tenant_id` is the YouTube *channel* owner — the orchestrator's service-account tenant under which OAuth credentials are stored — and is the same value for every real caller, used only to load credentials. `owner_tenant_id` is the *real* caller tenant, forwarded by the orchestrator as the `X-Owner-Tenant-Id` header; all list and get operations scope to it so one tenant never sees another's jobs. When a tenant has no grant of its own, uploads go to the platform channel (`youtube_platform_channel` table) only if that tenant is allowed to use it (`tenant_youtube_settings.platform_channel_allowed`, NULL = the platform default); otherwise they fail with "connect YouTube". The service does no re-encoding — the uploaded file's resolution is the cap, and YouTube derives its adaptive tiers from there.

The service is stateful, backed by PostgreSQL 18 over psycopg 3 (schema owned by Alembic, applied in an initContainer — never `create_all` at runtime) and Redis for OAuth state. Row-Level Security policies are enabled on both tables from the shared `w2v_common` source and become enforcing when the runtime connects as the restricted `w2v_youtube_app` role with `TENANT_RLS_MODE=enforce`. It runs in Kubernetes in the `w2v` namespace with no external ingress beyond the OAuth callback path.

## Features

- **Per-tenant OAuth2 credential management** — connect, status, and disconnect flows store one YouTube channel per tenant so each tenant publishes to its own account without sharing credentials
- **Encrypted token storage** — access and refresh tokens are Fernet-encrypted at rest (`ENCRYPTION_KEY`, required in production) so a database compromise does not leak live Google credentials
- **Automatic token refresh** — the worker refreshes an expired access token from the stored refresh token before each upload so long-lived connections keep working without re-consent
- **Asynchronous upload jobs** — `POST /api/upload` returns a `job_id` immediately and the transfer runs in a bounded background worker pool so callers never block on large files
- **Resumable chunked upload** — streams to the YouTube Data API v3 in 10 MB chunks with up to 5 retries and exponential backoff so transient network failures do not restart the whole upload
- **Shared-fallback channel** — a single pre-authorized refresh token publishes on the project's own channel when a tenant has no per-tenant row, making dev/test publishing work out of the box
- **Best-effort thumbnails and soft captions** — custom thumbnails and SRT/VTT caption tracks are attached after a successful insert and failures are swallowed so the core upload always succeeds
- **Strict per-tenant job scoping** — list and get scope to `owner_tenant_id` (the real caller), never the shared channel tenant, so no tenant can see another's upload jobs
- **SSRF-guarded downloads** — URL `file_source` fetches are re-validated per redirect hop against a private-IP/port guard, with an explicit trusted-host allow-list for the in-cluster artifact host
- **License-token authentication** — every route except the OAuth callback and health probes requires an introspected license token so the service is tenant-isolated by default
- **Health probes** — liveness, readiness (database-checked), and combined component health for Kubernetes orchestration

## Authentication

Two independent authentication layers apply: inbound callers authenticate to this service with a w2v license token, and outbound this service authenticates to Google with per-tenant OAuth2 credentials.

**Inbound (w2v license token).** Auth is enabled by default (`AUTH_ENABLED=true`). The `AuthMiddleware` (subclassed from `w2v_common`) extracts the license token from `X-Tenant-Id: Bearer <license_token>` — never `Authorization`, which is reserved for Keycloak JWTs — and introspects it against the licenses service, caching results for 5 minutes. The resolved channel tenant comes from the token; the real caller tenant is read from the trusted `X-Owner-Tenant-Id` header forwarded by the orchestrator.

| Priority | Method | Details |
|----------|--------|---------|
| 1 | License token | Introspection of `X-Tenant-Id: Bearer <token>` against the licenses service, 5-minute cache |
| 2 | Trusted network | Synthetic in-cluster identity for RFC 1918 source IPs — OFF by default (`trust_incluster_identity=false`, fail-closed) |

Exempt paths (no token required): `/`, `/health`, `/health/live`, `/health/ready`, `/healthz`, `/readyz`, `/ready`, `/docs`, `/redoc`, `/openapi.json`, `/favicon.ico`, `/metrics`, and `/api/auth/callback`. The callback is public because Google redirects there with no license token; the tenant is recovered from the one-time OAuth `state` token stored in Redis.

**Outbound (per-tenant Google OAuth2).** To publish, the service uses Google OAuth2 credentials scoped to `youtube.upload`, `youtube.readonly`, and `youtube.force-ssl`. Each tenant's access and refresh tokens are stored Fernet-encrypted in `oauth_tokens`; the worker loads them at upload time and refreshes the access token from the refresh token when it has expired. Tenants without a stored row fall back to the shared pre-authorized channel.

Row-Level Security is enabled (not forced) on both tables and enforces tenant isolation once the app connects as the restricted `w2v_youtube_app` role with `TENANT_RLS_MODE=enforce`.

## Quick Start

```bash
uv sync
uv run uvicorn src.asgi:app --host 0.0.0.0 --port 6315
uv run pytest tests/ -v --cov=src
uv run ruff check src/ tests/ && uv run ruff format --check src/ tests/
```

## API Endpoints

### Auth `/api/auth`

| Method | Path | Description |
|--------|------|-------------|
| POST | `/connect` | Mint an OAuth state token and return a Google consent URL |
| GET | `/callback` | OAuth2 redirect target — exchange code for tokens, store them (PUBLIC) |
| GET | `/status` | Report whether the tenant has a connected YouTube channel |
| DELETE | `/disconnect` | Remove the tenant's stored OAuth2 tokens |

### Upload `/api/upload`

| Method | Path | Description |
|--------|------|-------------|
| POST | `` | Create an upload job, returns a `job_id` immediately |
| GET | `/jobs/{job_id}` | Poll the status of a specific upload job |
| GET | `/jobs` | List upload jobs for the calling tenant (paginated) |

### Health

| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | Service info and endpoint listing |
| GET | `/health` | Combined health probe (database + redis + api) |
| GET | `/health/live` | Liveness probe |
| GET | `/health/ready` | Readiness probe (checks database) |
| GET | `/healthz` | Liveness alias |
| GET | `/readyz` | Readiness alias |

## Configuration

All configuration via environment variables.

| Variable | Default | Description |
|----------|---------|-------------|
| `API_HOST` | `0.0.0.0` | Listen host |
| `API_PORT` | `6315` | Listen port (8000 is rejected at validation) |
| `API_DEBUG` | `false` | Enable /docs and /redoc debug mode |
| `AUTH_ENABLED` | `true` | Toggle license-token auth middleware |
| `AUTH_DEFAULT_TENANT_ID` | `00000000-0000-0000-0000-000000000000` | Nil-UUID unattributed fallback tenant |
| `LOG_LEVEL` | `INFO` | Logging level |
| `LOG_JSON_LOGS` | `false` | Structured JSON logging |
| `DATABASE_URL` | — (required) | PostgreSQL connection string (psycopg 3) |
| `DATABASE_POOL_SIZE` | `10` | Connection pool size |
| `DATABASE_MAX_OVERFLOW` | `20` | Max connections beyond pool size |
| `DATABASE_POOL_TIMEOUT` | `30` | Pool checkout timeout (seconds) |
| `REDIS_URL` | — (required) | Redis connection URL (OAuth state store) |
| `REDIS_MAX_CONNECTIONS` | `10` | Redis pool size |
| `ENCRYPTION_KEY` | `` | Fernet key for token encryption at rest (required in production) |
| `YOUTUBE_CLIENT_ID` | `` | Google OAuth2 Web client ID |
| `YOUTUBE_CLIENT_SECRET` | `` | Google OAuth2 Web client secret |
| `YOUTUBE_REDIRECT_URI` | `` | Registered OAuth2 callback URL |
| `YOUTUBE_SCOPES` | `youtube.upload, youtube.readonly, youtube.force-ssl` | Requested YouTube API scopes |
| `YOUTUBE_FALLBACK_CLIENT_ID` | `` | Boot-seed only (copied into `youtube_platform_channel` when it is empty) |
| `YOUTUBE_FALLBACK_CLIENT_SECRET` | `` | Boot-seed only (copied into `youtube_platform_channel` when it is empty) |
| `YOUTUBE_FALLBACK_REFRESH_TOKEN` | `` | Boot-seed only (copied into `youtube_platform_channel` when it is empty) |
| `YOUTUBE_FALLBACK_CHANNEL_ID` | `` | Boot-seed only (copied into `youtube_platform_channel` when it is empty) |
| `YOUTUBE_FALLBACK_CHANNEL_TITLE` | `` | Boot-seed only (copied into `youtube_platform_channel` when it is empty) |
| `UPLOAD_MAX_CONCURRENT` | `4` | Max concurrent upload workers |
| `UPLOAD_MAX_FILE_SIZE_MB` | `5120` | Max upload file size (5 GB) |
| `UPLOAD_CHUNK_SIZE_BYTES` | `10485760` | Resumable upload chunk size (10 MB) |
| `UPLOAD_MAX_RETRIES` | `5` | Max upload retry attempts |
| `UPLOAD_TEMP_DIR` | `<tmp>/youtube_uploads` | Temp dir for downloaded sources |
| `UPLOAD_TRUSTED_DOWNLOAD_HOSTS` | `` | Comma-separated hosts exempt from SSRF checks (empty = strict) |

## Architecture

```
src/
├── asgi.py                          # FastAPI app, lifespan, middleware, OpenAPI, root
├── config.py                        # Pydantic settings (port 6315, YouTube/upload/encryption)
├── logging_config.py                # Structured logging configuration
├── api/
│   ├── auth_routes.py               # OAuth2 connect/callback/status/disconnect endpoints
│   ├── upload_routes.py             # Upload create/poll/list endpoints
│   ├── health.py                    # Liveness / readiness / combined health probes
│   ├── dependencies.py              # get_tenant_id / get_owner_tenant_id + service injectors
│   └── schemas.py                   # Request/response Pydantic DTOs
├── auth/
│   └── middleware.py                # License-token AuthMiddleware (+callback public exemption)
├── middleware/
│   └── request_id.py                # Request-ID ContextVar middleware (outermost)
├── services/
│   ├── oauth_service.py             # OAuth2 flow, token store/load/refresh/delete
│   ├── youtube_service.py           # YouTube Data API v3 resumable upload client
│   ├── upload_service.py            # Job creation, worker dispatch, status tracking
│   ├── encryption_service.py        # Fernet encrypt/decrypt for tokens at rest
│   └── url_guard.py                 # SSRF guard for URL file_source downloads
├── infra/
│   ├── database.py                  # Async engine + session factory with retry
│   └── task_manager.py              # Bounded background upload worker pool
└── database/
    ├── models.py                    # OAuthToken + UploadJob ORM models (+RLS DDL)
    ├── connection.py                # Engine/session helpers
    ├── session.py                   # Session management
    └── migrations/
        └── versions/                # 001_initial → 005_add_owner_tenant_id (Alembic)
```

## Testing

```bash
uv run pytest tests/ -v --cov=src          # All tests with coverage
uv run pytest tests/unit/ -v               # Unit tests
uv run pytest tests/ -m integration -v     # Integration-marked tests
uv run ruff check src/ tests/              # Lint
uv run ruff format --check src/ tests/     # Format check
```

Top-level suites cover auth middleware (`test_auth_middleware.py`), the OpenAPI security surface (`test_openapi.py`), and RLS tenant isolation (`test_rls_isolation.py`). Coverage threshold: 80% (enforced in pyproject.toml). `./scripts/ci-local.sh` mirrors the archetype `pre-build-checks` job exactly.

## CI/CD

`.github/workflows/ci-public.yml` is self-contained and runs on GitHub-hosted runners: a betterleaks secret scan, the `no-private-refs` guard, then `ruff check`, `ruff format --check`, `pytest` against a Postgres 18 service container, and `scripts/pip-audit.sh`.

No image is published from this repository; build it locally with the Dockerfile.

## Docker

```bash
docker build -t youtube-upload-service .
docker run -p 6315:6315 youtube-upload-service
docker run -p 6315:6315 -e LOG_LEVEL=DEBUG -e DATABASE_URL=postgresql+psycopg://host/db -e REDIS_URL=redis://host:6379/0 -e ENCRYPTION_KEY=<fernet-key> youtube-upload-service
```

## Dependencies

- **w2v_orchestrator** — upstream caller; supplies the rendered video (`file_source`) plus metadata and forwards `X-Owner-Tenant-Id`
- **licenses service** — validates inbound license tokens via introspection
- **storage_service / MinIO** — source of URL `file_source` artifacts downloaded before upload
- **PostgreSQL 18** — persistent storage (`oauth_tokens`, `upload_jobs`) via psycopg 3, through PgBouncer (`pgbouncer.pgbouncer:6432`) in-cluster
- **Redis** — one-time OAuth `state` token store (600s TTL)
- **Google YouTube Data API v3** — external upload target (`videos.insert`; ~1600 of 10,000 daily quota units per upload)
- **FastAPI** — web framework
- **Uvicorn** — ASGI server
- **Pydantic v2 / pydantic-settings** — validation and settings
- **SQLAlchemy (async)** — ORM over psycopg 3
- **Alembic** — schema migrations
- **google-api-python-client / google-auth / google-auth-oauthlib** — YouTube API client and OAuth2 flow
- **cryptography (Fernet)** — token encryption at rest
- **httpx** — async HTTP client for artifact downloads

## Known Issues

- **In-product "Connect" OAuth flow can fail on a GCP project mismatch** — the interactive Web-OAuth "Connect" path can fail when the OAuth client belongs to a different GCP project than expected. A tenant can now register its OWN Google OAuth client (`PUT /api/settings/oauth-client`) and connect with it. The platform channel (`youtube_platform_channel`, seeded once from `YOUTUBE_FALLBACK_*`) is used only for tenants allowed onto it; a tenant's own grant always takes precedence.
- **Soft captions and custom thumbnails are best-effort** — caption attach needs the `youtube.force-ssl` scope and thumbnails need a verified channel; tokens minted before that scope lack it, so the resulting 403 is swallowed and the upload still succeeds.
- **YouTube quota ceiling** — `videos.insert` costs 1600 of 10,000 daily units, capping a single OAuth project at roughly 6 uploads per day.
```

## License

Apache 2.0 — see [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE).

## Licensing

This service contains no licence enforcement of its own and runs no licence
server. Tenant and licence identity is forwarded from the caller. It runs with
no licence switch set; unset means off.
