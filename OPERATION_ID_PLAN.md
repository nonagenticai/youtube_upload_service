# Operation ID Plan: youtube-upload-service

Canonical registry of `operation_id` values for every route in
`youtube-upload-service`. These IDs feed the org-wide n8n custom-node
generator and the `ultrareview` cross-service catalogue, so they must be:

- **stable** — never rename without coordinating downstream,
- **`verb_noun` snake_case** — matches the org standard (nlp_service,
  worker_management, kag, analysis_service),
- **globally unique** — disambiguated against the rest of the fleet's verbs.

This service exposes two domains (auth, upload) plus the standard health
surface and the service-banner root. Domain verbs are suffixed with
`_youtube_` where the bare verb (`start_upload`, `get_status`,
`disconnect_auth`) would collide with other services in the fleet.

## Files to edit

- `src/asgi.py` (root endpoint)
- `src/api/health.py`
- `src/api/auth_routes.py`
- `src/api/upload_routes.py`

## Mapping Tables

### `src/asgi.py` (tag: `health`)

| Method | Function | operation_id   | Rationale                              |
|--------|----------|----------------|----------------------------------------|
| GET    | `root`   | `service_root` | Service banner + endpoint index probe. |

### `src/api/health.py` (tag: `health`)

| Method | Function       | operation_id   | Rationale                                                       |
|--------|----------------|----------------|-----------------------------------------------------------------|
| GET    | `health_check` | `health_check` | Combined health (DB + Redis + API).                             |
| GET    | `health_live`  | `health_live`  | Liveness — process-up only.                                     |
| GET    | `health_ready` | `health_ready` | Readiness — gated on database connectivity.                     |

### `src/api/auth_routes.py` (tag: `auth`)

| Method | Function         | operation_id                   | Rationale                                                                 |
|--------|------------------|--------------------------------|---------------------------------------------------------------------------|
| POST   | `auth_connect`   | `connect_youtube_auth`         | Start OAuth2 flow — namespaced to YouTube to avoid collisions.            |
| GET    | `auth_callback`  | `handle_youtube_auth_callback` | Google redirect target (PUBLIC — no license token); excluded from BearerAuth. |
| GET    | `auth_status`    | `get_youtube_auth_status`      | Tenant's YouTube connection state.                                        |
| DELETE | `auth_disconnect`| `disconnect_youtube_auth`      | Remove the tenant's stored OAuth2 tokens.                                 |

### `src/api/settings_routes.py` (tags: `settings`, `admin`)

| Method | Path | operation_id | Rationale |
|--------|------|--------------|-----------|
| GET    | `/api/settings` | `get_youtube_settings` | Owner tenant's YouTube settings (no secrets). |
| PUT    | `/api/settings/oauth-client` | `put_youtube_oauth_client` | Store the tenant's own Google OAuth client. |
| DELETE | `/api/settings/oauth-client` | `delete_youtube_oauth_client` | Remove it. |
| GET    | `/api/admin/platform-channel` | `get_youtube_platform_channel` | Operator: platform channel (null when unconfigured). |
| PUT    | `/api/admin/platform-channel` | `put_youtube_platform_channel` | Operator: create/update it. |
| PUT    | `/api/admin/tenants/{tenant_id}/platform-channel` | `put_tenant_youtube_platform_channel` | Operator: per-tenant permission. |
| GET    | `/api/admin/tenant-settings` | `list_youtube_tenant_settings` | Operator: all tenants' settings (no secrets). |

### `src/api/upload_routes.py` (tag: `upload`)

| Method | Function           | operation_id                | Rationale                                                                  |
|--------|--------------------|-----------------------------|----------------------------------------------------------------------------|
| POST   | `start_upload`     | `start_youtube_upload`      | Create async upload job. Verb prefixed `youtube_` against future `start_upload` collisions (e.g. a hypothetical S3 / Drive uploader). |
| GET    | `get_upload_job`   | `get_youtube_upload_job`    | Poll a single job by id.                                                   |
| POST   | `retract_upload_job` | `retract_youtube_upload`  | Set a published job's video to private (retraction only; never raises visibility). |
| POST   | `set_upload_job_privacy` | `set_youtube_upload_privacy` | Raise a published job's video to unlisted/public (governed release; never lowers, never deletes). |
| GET    | `list_upload_jobs` | `list_youtube_upload_jobs`  | List jobs for the tenant.                                                  |

## Verification

```bash
cd youtube_upload_service
uv run pytest tests/test_openapi.py -x
```

`tests/test_openapi.py` loads `app.openapi()` and asserts that the set of
`operationId` values equals the union of every right-hand column above.
Drift (rename, missed decorator, fallback) fails the test loudly.

## Disambiguation rationale

Concrete renames where the function name was generic enough to collide
across the fleet:

- `auth_connect` → `connect_youtube_auth`,
  `auth_callback` → `handle_youtube_auth_callback`,
  `auth_status` → `get_youtube_auth_status`,
  `auth_disconnect` → `disconnect_youtube_auth` — generic auth verbs
  would collide with any other OAuth2-integrating service in the fleet
  (e.g. a future drive / dropbox / slack integration).
- `start_upload` / `get_upload_job` / `list_upload_jobs` → `*_youtube_upload*`
  for the same reason: an upload verb without a target service does not
  describe its intent at the catalogue level.

## Future routes

When adding a route in this service, **always** set `operation_id="…"`
explicitly. The `generate_unique_id_function` configured in `asgi.py` is a
safety net for forgotten kwargs, never the primary identifier. New IDs go
in the table above as part of the same PR (no out-of-band updates).
