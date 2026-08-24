# Public Release Checklist

This checklist records the release-preparation work performed against the repository state based on `main` commit `e5dab3924868c23e6e007be5d285c6f374fa0ca3`.

It is intentionally conservative: incomplete verification is left incomplete rather than inferred.

## Repository privacy and history

- [x] Repository visibility was checked and left unchanged.
- [x] `main` was identified as the only branch available through the connected repository interface at review time.
- [x] Reachable `main` history was traversed parent-by-parent.
- [x] Reachable `main` history contains two commits: the initial build and one subsequent fix commit.
- [x] The second commit changes existing files only; it does not add/delete files relative to the initial commit.
- [x] Current project-authored configuration uses only generic loopback addresses and relative runtime paths.
- [x] Current project-authored files inspected for credentials, real/non-placeholder IP addresses, private DNS/domain names, MAC addresses, hostnames, GPS/location data, usernames, email addresses, identifying home paths, machine names, and internal service identifiers.
- [x] Removed/replaced lines in the only historical diff inspected for the same categories.
- [x] No sensitive historical material was found that justified rewriting reachable `main` history.
- [x] No unrestricted force push was used.
- [ ] **Tag refs require a final manual GitHub check before visibility is changed.** The connected GitHub interface used for this review can inspect branches/commits but does not expose tag enumeration for this private repository. Do not make the repository public until any tags are manually confirmed clean or removed/sanitized.

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

- [ ] Manually inspect GitHub tags because tag enumeration was unavailable to the connected review interface.
- [ ] Review the proposed public-ready branch/PR diff.
- [ ] Run the repository test suite in the intended Python environment.
- [ ] Run `node --check static/app.js` if Node.js is available.
- [ ] Perform at least a short real-device smoke test.
- [ ] Confirm no newly generated `runtime/`, `config.local.toml`, logs, databases, media, tokens, or auth files are staged.
- [ ] Re-run a secret/identifier scan after any additional changes.
- [ ] Decide whether the remaining known bugs are acceptable for a clearly labeled pre-release.
- [ ] Change repository visibility only as a separate, deliberate maintainer action.
