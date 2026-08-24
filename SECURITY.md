# Security Policy

## Project status

This project is pre-release and only partially tested. It should not be treated as a hardened security product or as a dependable security-camera monitoring system.

## Supported code

Security fixes should target the current default branch and the current pre-release line. Older commits are not promised security maintenance.

## Reporting a vulnerability

Prefer GitHub's private security-advisory workflow for this repository when it is available to you.

If private reporting is unavailable, open a minimal issue asking the maintainers for a private contact path. Do **not** include exploit details, credentials, access tokens, private live-view URLs, camera serial numbers, account information, or other sensitive data in a public issue.

## Security boundaries

The intended local security model is:

- Blink credentials/authentication state are stored in a Windows DPAPI envelope under `runtime/private/auth.dpapi`.
- The adapter binds its HTTP and TCP services to loopback only.
- The dashboard binds to loopback by default.
- Explicit LAN mode requires both a non-loopback dashboard host and `server.allow_lan = true`.
- LAN mode uses a random token in `runtime/private/dashboard-access.token`; browser access is converted to an HTTP-only SameSite cookie.
- Mutating browser controls require same-origin JSON requests.
- LAN traffic is plain HTTP unless the operator adds a separately validated encrypted transport layer.
- Generated runtime data, credentials, tokens, logs, media, SQLite files, and local configuration are excluded from source control.

The dashboard exposes camera media and experiment data to an authorized browser. Anyone who can access an authorized LAN browser session can therefore view that data and use the available experiment controls.

## Operational guidance

- Keep the default loopback configuration unless remote access is necessary.
- Treat `runtime/private/`, `runtime/data/`, and `runtime/logs/` as sensitive local data.
- Do not synchronize `runtime/` into shared cloud folders or source-control it.
- Do not share `auth.dpapi`; it is intentionally bound to the Windows user that created it.
- If a dashboard LAN token is exposed, stop the dashboard, remove `runtime/private/dashboard-access.token`, and relaunch to generate a new one.
- If Blink credentials or reusable authentication material are suspected compromised, rotate them through the relevant Blink/Amazon account process rather than relying only on local file deletion.
- Do not expose the local adapter ports through port-forwarding or a reverse proxy.

## Dependency security

Runtime Python dependencies are pinned in `pyproject.toml`. `blinkliveview` is fetched at an exact commit and verified against a pinned SHA-256 archive digest before use. Bundled browser assets are recorded in `static/vendor/THIRD_PARTY_NOTICES.md`.

Pinned versions improve reproducibility but do not guarantee absence of vulnerabilities. Dependency updates should be reviewed and retested rather than applied blindly.
