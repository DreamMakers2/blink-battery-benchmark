# API Reference

The dashboard HTTP interface is primarily an implementation interface for the bundled browser UI. It is not promised as a stable third-party API.

Default base URL:

```text
http://127.0.0.1:8090
```

## Authentication

Loopback mode does not add a dashboard token.

When `server.allow_lan = true`, every route except `/healthz` requires either:

- the generated query token, or
- the authorized HTTP-only cookie established after visiting `/?token=<token>`.

The token is stored locally in `runtime/private/dashboard-access.token`.

Mutating `POST` routes also require:

- `Content-Type: application/json`
- an `Origin` header exactly matching the request scheme and host.

LAN mode uses HTTP, not TLS.

## Health

### `GET /healthz`

Returns the dashboard process health marker:

```json
{
  "service": "blink-battery-dashboard",
  "status": "ok"
}
```

In LAN mode this route intentionally remains unauthenticated so local process/readiness checks can work.

## Status

### `GET /api/status`

Returns the current experiment state, test/phase timing, latest battery data, counters, stream health, media availability, allowed controls, stop/error fields, and adapter-health data used by the UI.

The exact object shape is pre-release and may change.

## Measurements

### `GET /api/measurements`

Query parameters:

- `start`: optional ISO-8601 timestamp.
- `limit`: integer clamped to the server's supported range; the handler caps it at 100,000.

Response contains:

```json
{
  "measurements": [],
  "phases": [],
  "truncated": false
}
```

The UI's complete-history download uses:

```text
/api/measurements?limit=100000
```

## Recent errors

### `GET /api/errors`

Query parameter:

- `limit`: integer clamped to a maximum of 200.

Only warning/error events are returned.

## Media

### `GET /latest.jpg`

Returns the latest successfully validated JPEG. Returns `404` before a successful snapshot exists.

### `GET /stream/index.m3u8`

Serves the generated HLS manifest during stream operation. Segment files are served beneath `/stream/`.

## Experiment controls

All control routes require JSON.

### `POST /api/experiment/start`

Starts a not-yet-started experiment or resumes a manually stopped phase when that transition is valid.

### `POST /api/experiment/stop`

Stops the currently stoppable experiment/recovery state and checkpoints state for later resume.

### `POST /api/experiment/restart`

Preserves the prior run as history and creates a new run beginning at test 1.

### `POST /api/experiment/continue`

Available only after a low-battery stop. It performs a fresh battery read and advances only if the reading is not in a configured low/replacement state.

Successful controls return HTTP `202`. Invalid state transitions return HTTP `409` with an `invalid_transition` error object. Non-JSON controls return HTTP `415`.

## Internal adapter interface

The adapter interface is an internal loopback implementation detail, not a remote/public API.

Default HTTP:

```text
http://127.0.0.1:8080
```

Routes:

```text
GET /
GET /snapshot
GET /battery
```

Default stream socket:

```text
tcp://127.0.0.1:5000
```

The adapter rejects non-loopback bind hosts in code.
