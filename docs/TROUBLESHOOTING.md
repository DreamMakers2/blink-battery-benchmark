# Troubleshooting

These cases are based on behaviors and guidance already present in the repository. They are not an invented compatibility list.

## Python was not found

The launcher requires Python 3.10 or newer through `py` or `python`.

Check:

```bat
py -3 --version
```

or:

```bat
python --version
```

After installing Python, open a new Command Prompt and relaunch.

## FFmpeg was not found

Check:

```bat
ffmpeg -version
```

If Windows cannot find the command, install FFmpeg, add its `bin` directory to `PATH`, open a new Command Prompt, and retry.

The repository does not pin a specific FFmpeg build.

## Encrypted credentials cannot be decrypted

`runtime/private/auth.dpapi` is encrypted for the Windows user that created it.

If it belongs to another Windows user or is damaged:

1. Stop the dashboard.
2. Remove only `runtime/private/auth.dpapi`.
3. Relaunch and authenticate again.

The experiment database is not removed by that action.

## Saved authentication keeps retrying

The current adapter deliberately treats saved-session startup failures as ambiguous because Blinkpy may not clearly distinguish invalid credentials from temporary network, service, throttling, or discovery failures.

Retry delays grow from 2 seconds and are capped at 60 seconds.

If the account credentials truly changed, stop the dashboard, remove only `runtime/private/auth.dpapi`, and relaunch for an intentional fresh sign-in.

## Browser did not open

Keep the launcher running and open:

```text
http://127.0.0.1:8090
```

manually, unless you changed the dashboard host/port in local configuration.

## Configured port is already in use

If another compatible dashboard is already listening on the configured dashboard port, the launcher may open it.

Otherwise choose a free dashboard port in `config.local.toml`.

The adapter's configured HTTP URL/port and stream port must stay internally consistent.

## Camera output is stale

The UI retains the last successfully received media rather than replacing it with a failed result.

During stream startup/reconnection, the page can fall back to the latest snapshot. Check the UI's stream reception and outage indicators to see whether new bytes are arriving.

## Where are detailed failures?

Start with the dashboard's recent warnings/errors view, then inspect:

```text
runtime/logs/application.log
runtime/logs/adapter.log
```

Treat logs as sensitive local data even though the project applies credential redaction.

## Known pre-release issue: manually paused recovery status

The latest revision is only partially tested and still has known bugs. One confirmed issue is that manually stopping during a recovery phase can make the status presentation describe remaining time/progress like an active camera test until the recovery is resumed. The persisted recovery deadline is retained, so this is primarily a status/reporting correctness issue in the current code path.

Do not use the dashboard's progress display as a scientific source of truth until this and any other pre-release issues are resolved and retested.

## Full-duration hardware behavior is not yet verified

The repository does not contain evidence of a complete latest-revision, real-device, four-stage long-duration acceptance run. If behavior differs on your hardware/account, capture sanitized logs and reproducible steps without publishing credentials, camera identifiers, private network information, or location data.
