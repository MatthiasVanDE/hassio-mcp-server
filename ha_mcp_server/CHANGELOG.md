# Changelog

All notable changes to this add-on are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## 2.0.0 — 2026-09-18

First public release. The add-on had been running privately since early 2026; this
version is the one prepared for other people to install, which is what makes it a
major bump rather than a first `1.0.0`.

### Added

- `amd64` support alongside `aarch64`. Those are the two architectures Home Assistant
  still supports; `armv7` and `i386` were dropped in Home Assistant 2025.12.
- `timezone` option. When left empty the add-on now asks Home Assistant which time
  zone it runs in, instead of assuming `Europe/Brussels`. This affects how relative
  history and logbook questions are turned into timestamps.
- JSON-RPC batch requests are accepted on `/mcp` and `/messages`.
- English documentation: `README.md`, `DOCS.md` and option translations.
- A Docker `HEALTHCHECK` polling `/health`. It replaces the add-on `watchdog` option,
  which the Home Assistant add-on linter now reports as obsolete.

### Changed

- `websockets` and `tzdata` are pinned to exact versions. The image is rebuilt on each
  user's machine at whatever date they install it, so an unpinned dependency means no
  two installations necessarily run the same code.
- **Breaking:** every tool description, response field and parameter name is now in
  English. Tool *names* are unchanged, so existing client configurations keep working,
  but code or prompts that depended on the Dutch response keys must be updated:
  `naam` → `name`, `toestand` → `state`, `aantal` → `count`, `entiteiten` → `entities`,
  `afgekapt` → `truncated`, `binair` → `binary`, `bestand` → `file`,
  `toelichting` → `note`, `staat` → `state`, `versie` → `version`,
  `totaal_regels` → `total_lines`, `bytes_verstuurd` → `bytes_sent`.
- **Breaking:** two tool parameters were renamed: `ha_error_log`'s `bron` is now
  `source`, and `ha_upload`'s `bestand` and `veld` are now `file` and `field`.
- `ha_addon_action` with `action: logs` now strips ANSI colour codes, as
  `ha_error_log` already did.

### Fixed

- A request body that parsed as JSON but was not an object (a bare string, or a
  JSON-RPC batch array) raised an `AttributeError` inside the handler thread and
  dropped the connection without a response. Such requests now get a proper `-32600`
  error, and batches are executed.
- An unauthenticated `POST` returned `401` without draining the request body, which
  desynchronised the next request on a keep-alive connection.
- A token containing a non-ASCII character made `secrets.compare_digest` raise instead
  of returning `401`.
- Requests with an oversized body are now rejected with `413` rather than being read
  into memory in full.
- A client hanging up mid-request printed a full traceback into the add-on log. A
  disconnect is ordinary and is now a single debug line.

## 1.3.0 — internal

- Supervisor-served logs (`/core/logs`, `/host/logs`, `/supervisor/logs`) after the
  core `/api/error_log` endpoint was removed in Home Assistant 2026.9.
- `ha_download` and `ha_upload` for binary payloads through the `share` folder.
- Graceful `SIGTERM` handling, so the Supervisor no longer has to kill the container.
