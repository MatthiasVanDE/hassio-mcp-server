# Changelog

All notable changes to this add-on are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## 2.2.0 — 2026-09-28

Eight new tools and a safety net under every change. The ideas come from comparing
this add-on with the much larger [ha-mcp](https://github.com/homeassistant-ai/ha-mcp)
server; what is useful was rebuilt here on the REST, WebSocket and Supervisor APIs,
with no code running inside Home Assistant itself.

### Added

- **`ha_overview`** — what needs attention, in one call: unavailable entities, pending
  updates, open repairs, integrations that failed to load, add-ons in error,
  notifications, and the number of errors in the log.
- **`ha_search`** — find entities by words in their id, name, area, device or aliases,
  tolerant of a typo. Given an exact entity id it also lists every automation, script,
  scene and group that uses it (YAML ones included), through Home Assistant's own
  `search/related`.
- **`ha_traces`** — why an automation or script did what it did: its recent runs, and
  one run step by step with the trigger, each condition's result, each action's result
  and the variables it changed. When there are no traces it says why.
- **`ha_statistics`** — long-term statistics (hourly, daily, monthly mean/min/max and
  meter totals), which the recorder keeps for years where history is purged after ten
  days.
- **`ha_file`** — list, read and search Home Assistant's configuration directory, and
  with the new `file_access: read_write` option also write, edit and delete. YAML is
  validated before it is written, `secrets.yaml` is shown masked and never written,
  `.storage` is never written, and login data is never read. `edit` replaces one exact
  piece of text, so a change to a large file does not mean sending all of it.
- **`ha_dashboard`** — an outline of a dashboard, one part of it by JSON pointer, and
  changes as JSON Patch operations instead of a complete rewrite.
- **`ha_check_config`** — the configuration check the UI runs before a restart.
- **`ha_camera`** — a camera snapshot as an MCP image the model can look at.
- **Backups of every change.** `ha_config_save`, `ha_config_delete`, `ha_file` and
  `ha_dashboard` keep the previous version in `share/ha-mcp/backups` before changing
  anything, name it in their answer, and refuse to go ahead when the copy fails.
- **A review when an automation or script is saved.** Entities and services that do
  not exist are listed (Home Assistant accepts them and only fails at run time), with
  advice where a template does what a native trigger or condition would do better.
- **A read-only token** (`readonly_token`): a client using it sees and may call only
  what cannot change anything; everything else is refused before it reaches Home
  Assistant.
- **Tool annotations** (`readOnlyHint`, `destructiveHint`) on every tool, and server
  `instructions` in the `initialize` answer.
- Tests for all of the above against canned Home Assistant answers
  (`tests/test_tools.py`), run in CI.

### Changed

- **A restart checks the configuration first.** `homeassistant.restart`,
  `homeassistant.stop` and `hassio.host_reboot` through `ha_service`, and a restart or
  reboot through `ha_supervisor`, are refused while the check fails. A broken
  configuration would otherwise leave Home Assistant down, out of reach of the very
  tool that could fix it. `skip_config_check` overrides.
- `ha_history` returns compact `[time, state]` pairs in Home Assistant's time zone, and
  takes an absolute or relative `start` and `end`. Only significant changes by default.
- `ha_logbook` takes `start` and `end` as well.
- `ha_error_log` gained `source: errors` — Home Assistant's de-duplicated error list
  with counts and tracebacks — and `search` and `level` filters for the raw logs.
- The manifest maps `homeassistant_config` (at `/homeassistant`). Restart the add-on
  once after updating so the Supervisor mounts it.
- `pyyaml` is a new, pinned dependency, used only to check YAML and to mask secrets.

### Fixed

- **`ha_error_log` and add-on logs never returned more than 100 lines**, whatever
  `lines` said: the Supervisor answers with 100 unless it is asked for a range. It is
  now asked for exactly the lines requested, and `ha_addon_action` `logs` takes `lines`
  too.

## 2.1.1 — 2026-09-19

### Fixed

- The `token` option is no longer declared as required. It stopped being required in
  2.1.0, when an empty value started meaning "generate one", but the schema still said
  otherwise and the configuration panel marked a field with a red asterisk that is
  perfectly fine to leave alone.

### Added

- Documentation on reaching the add-on from outside your own network, and why a port
  forward is the wrong answer.

## 2.1.0 — 2026-09-19

Everything in this release is about the distance between finding the add-on and
having it working. Nothing about what the tools do changed.

### Added

- **The add-on has its own page.** "OPEN WEB UI" on the add-on, or the sidebar entry,
  opens a page that shows whether Home Assistant is answering, the endpoint address,
  the token behind a *Show secrets* toggle with a copy button, a ready-made
  `claude mcp add` command, a ready-made JSON configuration for any other client, and
  the full tool list. It is served over Supervisor ingress on port 8098, which speaks
  no MCP and asks for no bearer token — the Supervisor has already authenticated the
  visitor before it proxies anything there.
- **The token is shown only to administrators.** Ingress authenticates the visitor but
  does not by itself keep non-administrators out, and this token is unrestricted
  control over the house. The page therefore checks membership of the `system-admin`
  group against `config/auth/list` before printing it, and says plainly why it is
  hidden when it will not.
- **A generated token.** Leaving `token` empty no longer refuses to start. The add-on
  generates one, keeps it in `/data` so that it survives restarts and updates, and
  prints it in the log and on its page. A first start is now a working add-on instead
  of a red error, and the port is still never unauthenticated.
- **Prebuilt images**, published to `ghcr.io/matthiasvande/{arch}-addon-ha-mcp-server`
  for `aarch64` and `amd64` by a release workflow that runs on a version tag.
- `stage: stable` and `backup: hot` in the manifest, and a sidebar panel.
- Tests for the generated token, for the page, and for who is allowed to see the
  token on it.

### Changed

- **Installing no longer builds anything.** `config.yaml` now carries an `image:`, so
  the Supervisor pulls the image CI published for that exact version instead of
  compiling the Dockerfile on the user's own machine — seconds rather than minutes on
  a Raspberry Pi, and provably the same code for everyone. Building it by hand still
  works and is how you check what is inside.
- The release workflow refuses to publish unless the git tag, the version in
  `config.yaml`, the version in `server.py` and the changelog all agree, and CI checks
  the same thing on every pull request. With a prebuilt image a wrong version is not
  an inconvenience for the maintainer, it is a broken install for everyone.
- The `BUILD_VERSION` argument in the Dockerfile defaults to `dev`; CI passes the real
  version. A hand-built image now says what it is.
- The workflow files are linted along with the rest of the YAML.

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
