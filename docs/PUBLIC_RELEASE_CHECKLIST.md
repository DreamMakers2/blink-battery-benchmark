# Public Release Checklist

This checklist records the release-preparation work performed against all Git refs fetched from GitHub through `main` commit `8ec1309e3e920ea4afff5db4f11729dad73def78`, plus this final documentation-only verification update.

It is intentionally conservative: incomplete verification is left incomplete rather than inferred.

## Repository privacy and history

- [x] Repository visibility was checked and left unchanged.
- [x] The fetched refs were enumerated: `main`, `public-ready-review`, and merged pull request 1's head ref all pointed to `8ec1309e3e920ea4afff5db4f11729dad73def78` before this checklist update.
- [x] All commits reachable from every fetched ref were traversed and scanned.
- [x] Git and GitHub tag enumeration both found no tags; GitHub release enumeration also found no releases.
- [x] A fresh remote mirror contained no unreachable objects according to `git fsck --full --unreachable --no-reflogs`; no removed commit history was identified outside the retained refs.
- [x] Gitleaks 8.30.1 and TruffleHog 3.97.1 reported no secrets across all fetched refs and history.
- [x] Current project-authored configuration uses only generic loopback addresses and relative runtime paths.
- [x] Current project-authored files inspected for credentials, real/non-placeholder IP addresses, private DNS/domain names, MAC addresses, hostnames, GPS/location data, usernames, email addresses, identifying home paths, machine names, and internal service identifiers.
- [x] Historical versions and diffs inspected for the same categories. Matches outside vendored public documentation were reserved `.test` email placeholders used by tests.
- [x] No sensitive historical material was found that justified rewriting retained history.
- [x] No unrestricted force push was used.

## Current tree hygiene

- [x] Runtime/private data paths are excluded by `.gitignore`.
- [x] Local override configuration is excluded.
- [x] Environment files, logs, caches, editor metadata, and local agent/tool state are excluded.
- [x] Obsolete `AGENTS.md` agent instruction removed from the release tree.
- [x] Generic loopback examples retained because they are intentional safe defaults.
- [x] Public upstream dependency URLs retained where technically necessary.
- [x] Third-party browser notices retained.
- [x] Repository documentation avoids publishing real environment identifiers.

## Public-project files

- [x] `LICENSE`
- [x] `NOTICE`
- [x] `CONTRIBUTING.md`
- [x] `SECURITY.md`
- [x] `docs/SETUP.md`
- [x] `docs/REQUIREMENTS.md`
- [x] `docs/ARCHITECTURE.md`
- [x] `docs/API.md`
- [x] `docs/TROUBLESHOOTING.md`
- [x] Architecture infographic
- [x] README warning/disclaimer, tagline, restrained badges, Mermaid diagram, and documentation links

`docs/PROMPTING.md` was not added because the project is not an AI/agent application and no prompting interface exists in the codebase.

## Documentation accuracy

- [x] Python requirement taken from `pyproject.toml`.
- [x] Runtime/test dependency versions taken from `pyproject.toml`.
- [x] Managed `blinkliveview` commit and SHA-256 taken from bootstrap/adapter code.
- [x] Default ports/timing/path values taken from committed configuration.
- [x] HTTP routes taken from the aiohttp application.
- [x] Windows DPAPI requirement taken from the secret-store implementation.
- [x] FFmpeg requirement taken from bootstrap/media code.
- [x] Hardware minimum/recommended values not invented where evidence is absent.
- [x] Missing exact tested host/device configuration is explicitly disclosed.

## Pre-release quality

- [x] README explicitly says the latest revision is partially tested and still contains bugs.
- [x] Known manually-paused recovery status issue documented.
- [ ] Complete real-device latest-revision acceptance run.
- [ ] Full four-stage long-duration experiment validation.
- [ ] Exact tested hardware/OS/FFmpeg baseline captured in sanitized form.
- [ ] Known pre-release bugs resolved and regression-tested.

## Before changing repository visibility

- [x] Confirm GitHub contains no tags or releases.
- [x] Review the merged public-ready branch/PR ref as part of the all-ref history scan.
- [ ] Run the repository test suite in the intended Python environment.
- [ ] Run `node --check static/app.js` if Node.js is available.
- [ ] Perform at least a short real-device smoke test.
- [x] Confirm no newly generated `runtime/`, `config.local.toml`, logs, databases, media, tokens, or auth files are staged.
- [x] Re-run secret and identifier scans after the final documentation update.
- [ ] Decide whether the remaining known bugs are acceptable for a clearly labeled pre-release.
- [ ] Change repository visibility only as a separate, deliberate maintainer action.
