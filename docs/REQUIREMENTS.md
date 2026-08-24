# Requirements

This document separates repository-verified requirements from hardware/environment facts that are **not recorded**. It intentionally does not invent minimum hardware specifications or compatibility claims.

## Verification status

The repository contains code, configuration, tests, and setup logic, but it does **not** contain a trustworthy hardware acceptance record identifying the exact host computer, doorbell model/revision, battery chemistry, firmware version, network equipment, Windows build, CPU, RAM, storage capacity, or FFmpeg build used in a successful full-duration experiment.

Therefore:

- **Exact hardware/configuration actually verified to work:** not recoverable from repository evidence.
- **Evidence-based minimum CPU/RAM/storage specifications:** not established.
- **Evidence-based recommended CPU/RAM/storage specifications:** not established.
- **Full Windows-version compatibility matrix:** not established.
- **Complete real-device, four-stage, long-duration validation of the latest revision:** not established.

Those gaps should be filled only after a documented acceptance run.

## Hardware requirements supported by repository evidence

### Blink device

The application is designed to operate against **one Blink camera/doorbell selected from the authenticated Blink account**. Existing documentation describes the intended target as one Blink doorbell, but the exact model and hardware revision are not recorded.

The project does not communicate with the device through USB, serial, GPIO, Bluetooth, or another direct peripheral interface. Camera operations are requested through Blink-facing Python libraries/services.

### Host computer

The production credential backend requires Windows current-user DPAPI and the normal launcher is `launch.cmd`, so the repository's production path is Windows-oriented.

The code does not enforce a specific CPU vendor or architecture. The practical architecture must be supported by the chosen Windows/Python/FFmpeg builds and the pinned Python dependencies.

No repository evidence establishes a minimum or recommended core count, clock speed, RAM amount, or storage capacity.

### GPU / accelerator

No GPU or hardware accelerator is required by project logic in the repository. The FFmpeg command uses stream copy (`-c copy`) when creating HLS output rather than an explicit video encoder. No CUDA, DirectML, Quick Sync, NVENC, or other accelerator interface is configured.

This is an implementation fact, not a guarantee that every host can process every stream without performance issues.

### Storage

The application writes:

- a Python virtual environment;
- fetched managed dependency source;
- SQLite experiment state/history;
- one latest JPEG;
- rolling HLS media files;
- rotating application/adapter logs;
- encrypted local authentication state.

The experiment database has no project-level size cap, so required storage depends on run duration and measurement/event volume. The repository does not contain measurements sufficient to state a safe minimum disk capacity.

Application logs rotate at 5 MiB with five backups per configured log file. That bound does not include SQLite data, media, the virtual environment, or fetched dependencies.

### Networking

Default local listeners:

| Component | Default | Scope |
| --- | --- | --- |
| Dashboard HTTP | `127.0.0.1:8090` | Loopback by default; optional explicit LAN mode |
| Blink adapter HTTP | `127.0.0.1:8080` | Loopback only |
| Blink adapter stream TCP | `127.0.0.1:5000` | Loopback only |

Internet/Blink connectivity is required for Blink authentication and camera/battery operations. Initial setup also requires access to Python package sources and the pinned public `blinkliveview` source archive.

The repository does not establish minimum bandwidth, latency, Wi-Fi standard, router model, or internet-speed requirements.

### Peripherals and interfaces

No project-specific USB, serial, GPIO, microphone, display, capture card, or local camera interface is required by the code. A web browser is used to view the dashboard.

## Software requirements

### Operating system

The existing launcher and credential design target Windows.

The prior README named Windows 10 or 11 as prerequisites, but the repository does not record the exact Windows edition/build used for a successful latest-revision hardware acceptance run. Treat Windows 10/11 as the documented target, not as a fully verified compatibility matrix.

### Python

`pyproject.toml` requires:

```text
Python >= 3.10
```

The launcher rejects older Python versions.

### Python runtime dependencies

Pinned project dependencies:

| Dependency | Version |
| --- | --- |
| `aiohttp` | `3.14.3` |
| `blinkpy` | `0.25.9` |
| `Pillow` | `11.3.0` |
| `tomli` | `2.2.1` only when Python < 3.11 |
| `hatchling` | `1.27.0` build backend |

Pinned test dependencies:

| Dependency | Version |
| --- | --- |
| `pytest` | `8.4.2` |
| `pytest-asyncio` | `1.2.0` |

The adapter additionally enforces Blinkpy `0.25.9` at runtime for the expected authentication contract.

### Managed `blinkliveview` source

Setup fetches exactly:

```text
commit: d8f0a02180efce003de690055b87e8e2d5482e12
archive SHA-256: 27e5fe91a6f4e0ffe8c55c2b226bda744e1e628fa5810fdc10f87a8ac710a050
```

The archive is rejected if its SHA-256 digest does not match.

### FFmpeg

FFmpeg must be available as:

```text
ffmpeg
```

and on Windows therefore normally as `ffmpeg.exe` on `PATH`.

The repository does not pin or record a verified FFmpeg version. Do not infer a minimum FFmpeg release from this documentation.

### Browser

The UI uses locally bundled JavaScript/CSS assets and browser APIs, including HLS playback support through bundled `hls.js` when needed. No exact browser/version compatibility matrix is recorded.

Bundled browser assets documented in the repository include:

- Web Awesome 3.9.0
- Font Awesome Free 7.2.0
- Chart.js 4.5.1
- hls.js 1.6.16

### Node.js

Node.js is **not** a runtime requirement. The README uses `node --check static/app.js` as an optional development-time JavaScript syntax check. No Node.js version is pinned.

### Drivers and firmware

The repository installs no project-specific hardware driver and does not pin doorbell firmware. Blink device firmware and service compatibility are external dependencies and are not asserted here.

## Recommended acceptance record for a future verified release

Before claiming a verified hardware baseline, record a sanitized acceptance matrix containing:

- Blink model/product type, with serial/account identifiers removed;
- host Windows edition and build;
- CPU architecture and model family;
- installed RAM;
- free storage before/after the run;
- Python version;
- FFmpeg version/build;
- relevant dependency versions;
- network connection type;
- test duration and which stages completed;
- observed failures/restarts;
- confirmation that no private network identifiers or physical location data are published.
