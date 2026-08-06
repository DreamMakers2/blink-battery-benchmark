# Blink Doorbell Battery Test Dashboard

A Windows-first, local dashboard for running a restart-safe four-stage battery-usage experiment against one Blink doorbell. It schedules snapshots or a continuous live view, records fresh battery readings and operational counters in SQLite, and presents camera output, status, history, and errors in a responsive browser UI.

The normal entry point is parameter-free:

```text
launch.cmd
```

Double-click `launch.cmd` from File Explorer. No command-line parameters are required.

## What the launcher does

On its first run, `launch.cmd`:

1. Finds a Windows Python 3.10 or newer installation.
2. Creates the project-local `.venv` environment.
3. Installs the pinned Python application dependencies.
4. Downloads the exact managed `blinkliveview` source revision and verifies its SHA-256 checksum.
5. Verifies that `ffmpeg.exe` is available on `PATH`.
6. Prompts in the launcher window for Blink credentials and MFA when Blink requires them.
7. Prompts for a camera when the account has multiple cameras, or when a previously selected camera is no longer available.
8. Starts both loopback services and opens `http://127.0.0.1:8090` in the default browser.

Later launches reuse the encrypted Blink session, selected camera, virtual environment, and managed dependency. If Blink invalidates the saved session, the launcher prompts again. MFA one-time codes are never saved.

The first setup needs internet access for Python packages, `blinkliveview`, and Blink authentication. Browser UI libraries, icons, styles, and fonts are served locally after checkout; the dashboard itself has no CDN dependency.

## Prerequisites

- Windows 10 or 11.
- Python 3.10 or newer installed for the current user and available through `py` or `python`.
- FFmpeg available as `ffmpeg.exe` on `PATH`.
- A Blink account with access to the doorbell under test.
- Internet access to Blink while the experiment is operating.

To verify FFmpeg from a new Command Prompt:

```bat
ffmpeg -version
```

If that command is not found, install an FFmpeg Windows build, add its `bin` directory to `PATH`, and open a new Command Prompt before launching the dashboard.

## Credential and local-data security

Authentication state is stored at:

```text
runtime/private/auth.dpapi
```

That file is encrypted with Windows DPAPI in current-user scope. It can only be decrypted by the Windows account that created it. There is deliberately no plaintext or cross-platform credential fallback. Passwords, tokens, authorization values, and MFA fields are redacted from application logging.

The following local/mutable paths are excluded by `.gitignore`:

- `.venv/`
- `runtime/`, including encrypted credentials, logs, SQLite data, media, and fetched upstream code
- `config.local.toml`
- Python/test caches and coverage output

Do not move `auth.dpapi` to another Windows account expecting it to work. To force a clean sign-in, close the dashboard and delete only `runtime/private/auth.dpapi`; the next launch will prompt again. This does not delete experiment results.

Both the adapter and dashboard bind to loopback by default. Exposing the dashboard to a LAN requires both changing `server.dashboard_host` and explicitly setting `server.allow_lan = true` in `config.local.toml`. LAN mode protects the UI, camera media, JSON endpoints, and controls with a random bearer stored in `runtime/private/dashboard-access.token`; the launcher uses it once in a redirecting URL to establish an HTTP-only, same-site browser cookie. Mutating controls additionally require same-origin JSON requests. The Blink adapter remains loopback-only.

LAN transport is plain HTTP and is intended only for a private, trusted network. The access token prevents unauthenticated viewing and cross-site control requests, but it does not encrypt network traffic. Use a trusted reverse proxy with TLS if traffic may traverse an untrusted network.

## Experiment sequence

The committed defaults run these tests in order:

1. A fresh snapshot every 300 seconds for up to 12 active hours.
2. A fresh snapshot every 60 seconds for up to 12 active hours.
3. A fresh snapshot every 30 seconds for up to 12 active hours.
4. A continuously consumed live stream for up to 12 active hours.

A 12-hour recovery period separates completed tests. Recovery generates no snapshots and does not connect to the live stream, but battery polling and measurement recording continue.

Active-test duration uses monotonic elapsed time and pauses while the application is closed or manually stopped. Recovery deadlines use wall-clock UTC, so a recovery period continues while the application is closed. The current run, open phase, counters, measurements, errors, and previous runs are durable in SQLite.

When a fresh Blink reading reports a configured low/replacement state, camera activity stops immediately and the run enters `stopped_low_battery`. Automatic recovery monitoring continues. “Continue with next test” first requires a new successful, non-low Blink reading; it cannot override a battery that is still reported low.

Transient snapshot, battery, stream, and FFmpeg errors are retained and retried. A continuous critical outage longer than the configured fatal-outage interval moves the run to `stopped_error`.

## Controls

All mutating browser controls require confirmation:

- **Start** begins a new run or resumes a manually stopped phase.
- **Stop** halts camera activity and checkpoints the current run.
- **Restart** preserves the old run as history and starts a new run from test 1.
- **Continue with next test** is shown only after a low-battery stop and only succeeds after a fresh recovered battery reading.

Closing a browser tab does not stop the experiment; the runner belongs to the launcher process. Use **Stop** when you want to pause intentionally. Press `Ctrl+C` in the launcher window for a clean application shutdown.

## Configuration

Committed production defaults live in [`config.toml`](config.toml). Put machine-specific overrides in an untracked `config.local.toml`; the loader deep-merges it over the committed file.

For a short exercise, copy `config.dev.example.toml` to `config.local.toml`:

```bat
copy config.dev.example.toml config.local.toml
```

The example uses one-minute tests, short recovery, and frequent polling. Delete `config.local.toml` to return to production defaults.

Configurable groups include:

- `[server]`: dashboard host/port and explicit LAN opt-in.
- `[blink]`: loopback HTTP/TCP endpoints, request timeouts, reconnect delay, and low-battery state names.
- `[experiment]`: test/recovery durations, battery and measurement intervals, checkpoint cadence, fatal outage threshold, and the ordered test definitions.
- `[paths]`: runtime, SQLite, latest JPEG, HLS, private, and log locations.
- `[media]`: FFmpeg executable and HLS segment/list settings.

Unknown keys, invalid ports, non-positive intervals, or a test order other than three snapshot tests followed by one stream test stop startup with a clear configuration error.

## Local files and retention

The runtime layout is created automatically:

```text
runtime/
  data/
    experiment.db
    latest.jpg
    hls/
  deps/
    blinkliveview/
  logs/
    application.log
    adapter.log
  private/
    auth.dpapi
    dashboard-access.token    # created only when LAN mode is enabled
```

SQLite uses WAL mode and schema migrations. It retains all run, phase, measurement, and event rows. The webpage bounds its recent error display, while the database and rotating application logs retain the underlying history. Application log files rotate at 5 MiB with five backups.

The dashboard exposes the following loopback endpoints:

```text
GET  /healthz
GET  /api/status
GET  /api/measurements?start=<ISO-8601>&limit=<n>
GET  /api/errors?limit=<n>
GET  /latest.jpg
GET  /stream/index.m3u8
POST /api/experiment/start
POST /api/experiment/stop
POST /api/experiment/restart
POST /api/experiment/continue
```

`/api/measurements?limit=100000` is also linked from the accessible history panel as a complete JSON download. Raw Blink battery fields remain labelled as raw values; only raw voltage is converted using `raw / 100` and displayed as volts.

## Development and tests

The normal user does not need these commands. For development from Bash/WSL or another shell with Python available:

```text
python -m venv .venv
.venv/bin/python -m pip install -r requirements-test.lock
.venv/bin/python -m pytest -q
node --check static/app.js
```

On Windows, use `.venv\Scripts\python.exe` instead of `.venv/bin/python`.

The test suite covers configuration validation, SQLite migrations and rollback, restart recovery, snapshot scheduling, low-battery transitions, timeout/failure classification, stream checkpoints, encrypted secret envelopes, adapter contracts, JPEG/media behavior, HTTP APIs, supervisor readiness, static accessibility, local icons, and transitive stylesheet imports.

## Troubleshooting

**The launcher says Python was not found**

Install Python 3.10 or newer from python.org, enable the launcher/`PATH` option, then reopen Command Prompt.

**Setup says FFmpeg was not found**

Confirm `ffmpeg -version` works in a newly opened Command Prompt. Merely placing FFmpeg elsewhere on disk is not enough; its `bin` directory must be on `PATH`.

**Credentials cannot be decrypted**

The encrypted file belongs to a different Windows user, or it is damaged. Close the application, remove `runtime/private/auth.dpapi`, and launch again to authenticate. Experiment data in `runtime/data/experiment.db` is unaffected.

**The browser did not open**

Leave the launcher running and open `http://127.0.0.1:8090` manually.

**The configured port is already in use**

If another compatible dashboard is already running, the launcher opens it. Otherwise, set a free `server.dashboard_port` in `config.local.toml`. Adapter ports must remain consistent with `blink.http_base_url` and `blink.stream_port`.

**Camera output is stale**

The timestamp below the camera is the last successfully received media time. Snapshot failures never replace the last valid JPEG. During stream startup or reconnection, the page intentionally falls back to the latest snapshot.

**Where are detailed failures?**

Use the collapsible warnings/errors section first, then inspect `runtime/logs/application.log` and `runtime/logs/adapter.log`. Secrets and upstream private stream targets are redacted or suppressed.
