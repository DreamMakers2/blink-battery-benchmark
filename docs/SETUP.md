# Setup

This guide describes the repository's intended Windows setup path. The latest revision is pre-release, only partially tested, and may still contain bugs.

## 1. Confirm prerequisites

You need:

- Windows with current-user DPAPI available.
- Python 3.10 or newer, callable through `py` or `python`.
- FFmpeg available as `ffmpeg.exe` on `PATH`.
- A Blink account that can access the single doorbell you want to test.
- Internet connectivity for initial Python/dependency setup and Blink authentication; Blink connectivity is also required while the experiment performs camera/battery operations.

Check Python:

```bat
py -3 --version
```

If `py` is unavailable:

```bat
python --version
```

Check FFmpeg:

```bat
ffmpeg -version
```

The repository does not pin an FFmpeg version. If `ffmpeg` is not found, install a Windows FFmpeg build, add its `bin` directory to `PATH`, and open a new Command Prompt.

## 2. Obtain the repository

Clone or download the repository into a directory owned by your Windows user. Do not place credentials or runtime files into the Git working tree manually.

From a Git clone:

```bat
git clone <your-repository-url>
cd <repository-directory>
```

## 3. Review configuration

The committed defaults are in `config.toml`. They bind the dashboard and Blink adapter to loopback and store mutable data under `runtime/`.

For machine-specific changes, create `config.local.toml`. It is intentionally ignored by Git.

For a short development exercise:

```bat
copy config.dev.example.toml config.local.toml
```

Delete `config.local.toml` to return to the committed defaults.

Do not put Blink usernames, passwords, tokens, cookies, camera serial numbers, or private upstream service details in either committed configuration file.

## 4. Launch

Run:

```text
launch.cmd
```

You can double-click `launch.cmd` from File Explorer or run it from Command Prompt.

On first launch the script:

1. Finds Python 3.10 or newer.
2. Creates `.venv` if needed.
3. Installs the pinned Python project dependencies.
4. Downloads the exact managed `blinkliveview` source revision.
5. Verifies the downloaded archive against the pinned SHA-256 digest.
6. Verifies that FFmpeg is available.
7. Starts the interactive Blink authentication flow.
8. Starts the local adapter and dashboard.
9. Opens the dashboard in the default browser.

## 5. Authenticate to Blink

The launcher prompts in its console window for the Blink username/email and password when no saved encrypted session is available.

If Blink explicitly requests MFA, enter the current one-time code in the launcher window. The project does not intentionally persist the MFA code.

After successful authentication, reusable auth state is encrypted to:

```text
runtime/private/auth.dpapi
```

The production secret backend uses Windows current-user DPAPI. There is no plaintext or cross-platform credential fallback.

## 6. Select a camera

If the account contains exactly one camera on first setup, it can be selected automatically. Otherwise choose the intended camera when prompted.

The selected camera identity is stored inside the encrypted auth payload so it can be matched on later launches. Do not copy that encrypted file into the repository.

## 7. Open the dashboard

The committed default is:

```text
http://127.0.0.1:8090
```

If the browser does not open automatically, keep the launcher running and open that address manually.

## 8. Start the experiment

The UI provides Start, Stop, Restart, and conditionally Continue controls. Each mutating action requires confirmation.

The committed sequence is three snapshot tests followed by one continuous-stream test, with recovery periods in between. See the [README](../README.md) for the default timing model.

## Optional: LAN dashboard access

LAN mode is disabled by default. To enable it, create `config.local.toml` with an appropriate local bind address and explicit opt-in, for example:

```toml
[server]
dashboard_host = "0.0.0.0"
allow_lan = true
```

Do not replace that example with a real private address in committed documentation.

When LAN mode starts, the project creates:

```text
runtime/private/dashboard-access.token
```

The launcher uses the token to establish an authorized browser cookie. The Blink adapter itself still binds to loopback.

LAN traffic is plain HTTP. The token prevents unauthenticated dashboard access but does not encrypt media, experiment data, or control traffic. Use LAN mode only on a network you trust. If you place another proxy or TLS layer in front of the dashboard, validate the same-origin control behavior for that deployment; proxy compatibility is not asserted by this repository.

## Resetting saved authentication

To intentionally force a fresh Blink sign-in:

1. Stop the dashboard.
2. Delete only `runtime/private/auth.dpapi`.
3. Run `launch.cmd` again.

This does not delete the experiment database.

## Development setup

For contributors:

```bat
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements-test.lock
.venv\Scripts\python.exe -m pytest -q
```

If Node.js is already installed:

```bat
node --check static/app.js
```

See [CONTRIBUTING.md](../CONTRIBUTING.md) and [TROUBLESHOOTING.md](TROUBLESHOOTING.md).
