# Contributing

This repository is a pre-release hardware-integration project. Changes should be conservative, testable, and explicit about what has and has not been verified.

## Before opening a change

1. Do not commit credentials, tokens, cookies, camera identifiers, serial numbers, private hostnames, real non-placeholder IP addresses, personal email addresses, machine/user names, or local runtime data.
2. Keep machine-specific settings in `config.local.toml`, which is ignored by Git.
3. Preserve loopback-only adapter behavior unless a security review specifically justifies a change.
4. Do not weaken DPAPI credential storage or introduce a plaintext credential fallback.
5. Do not describe hardware or software compatibility that has not actually been tested.

## Development setup

Create a virtual environment and install the test dependencies:

```text
python -m venv .venv
.venv/bin/python -m pip install -r requirements-test.lock
```

On Windows, use:

```text
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements-test.lock
```

Run the Python test suite:

```text
.venv/bin/python -m pytest -q
```

or on Windows:

```text
.venv\Scripts\python.exe -m pytest -q
```

If Node.js is already available, syntax-check the browser JavaScript:

```text
node --check static/app.js
```

Node.js is a development convenience for that syntax check; it is not a runtime dependency of the dashboard.

## Change expectations

- Add or update tests for behavior changes when practical.
- Keep error messages useful without including secrets, private upstream targets, or identifying environment details.
- Update README/docs whenever commands, configuration, endpoints, security boundaries, requirements, or architecture change.
- Keep examples generic. Use loopback addresses, reserved example domains, and placeholder values.
- Preserve third-party notices and bundled license material.

## Hardware claims

If a change is validated on real hardware, document only facts you actually observed. Include the relevant device class/model, host OS/build, Python version, FFmpeg version, and test scope when available, but remove personal names, account identifiers, serial numbers, MAC addresses, private network details, and exact physical location data.

## Reporting security problems

Do not place exploit details, credentials, private camera data, or tokens in a public issue. Follow [SECURITY.md](SECURITY.md).
