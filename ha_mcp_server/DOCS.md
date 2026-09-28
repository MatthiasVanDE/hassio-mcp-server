# MCP Server (Home Assistant API)

Exposes the full Home Assistant REST, WebSocket and Supervisor APIs as
[Model Context Protocol][mcp] tools over HTTP, so an AI assistant can read, diagnose
and change your Home Assistant installation.

> **This add-on grants administrative access.** Anyone holding the token can do
> anything you can do in the Home Assistant UI. Read [Security](#security) before you
> start it.

## Installation

1. Add the repository `https://github.com/MatthiasVanDE/hassio-mcp-server` under
   **Settings → Add-ons → Add-on Store → ⋮ → Repositories**.
2. Install **MCP Server (Home Assistant API)**. A prebuilt image is pulled; nothing is
   compiled on your machine.
3. Start it. Nothing has to be configured first — a token is generated on the first
   start if you have not set one.
4. Click **OPEN WEB UI** on the add-on. The page shows the endpoint, the token and a
   ready-made client configuration, each with a copy button. **Show in sidebar**, on
   that same add-on page, puts it in the sidebar for good.

## The add-on page

Served over Supervisor ingress, on a port of its own that is not published to your
network. It speaks no MCP: it exists so that connecting a client is copy and paste
rather than reading this document.

It shows whether Home Assistant is answering, the address a client should use, the
time zone in force, the token, a `claude mcp add` command, a JSON configuration for
any other client, and every tool with a one-line description.

Secrets on the page start blurred; *Show secrets* reveals them, and the copy buttons
work either way, so you can hand someone a screenshot without handing them your house.

**Who may see the token.** Ingress makes Home Assistant authenticate whoever opens the
page, but that is not the same as restricting it to administrators. The page therefore
asks Home Assistant itself whether your account is in the `system-admin` group, and
prints the token only then. If the answer is no — or if the question could not be
answered — the page says so and leaves the token out. It is always in the add-on log.

## Configuration

```yaml
token: "e3b0c44298fc1c149afbf4c8996fb924..."
log_level: info
file_access: read_only
readonly_token: ""
timezone: ""
```

### Option: `token` (optional since 2.1.0)

The bearer token an MCP client must send in its `Authorization` header.

**Left empty, the add-on generates one** of 32 random bytes on its first start, stores
it in `/data/token` — outside the options, so it survives restarts and updates and
never turns up in a configuration you paste into an issue — and shows it on its page
and in its log. The port is therefore never unauthenticated, which matters, because
reaching it means full administrative access to your home.

Set it to a value of your own if you would rather choose, or need the same token on
several installations:

```bash
openssl rand -hex 32
```

A configured token always wins over the generated one. Changing it takes effect on
restart, and every already-configured client must be updated to match.

### Option: `log_level`

One of `debug`, `info` (default), `warning`, `error`. At `debug` every incoming tool
call is logged with a 160-character preview of its arguments, which is the fastest way
to see what a client is actually asking for.

### Option: `file_access`

What `ha_file` may do in Home Assistant's configuration directory:

| Value | Allows |
|---|---|
| `off` | nothing; the tool answers that it is switched off |
| `read_only` (default) | `list`, `read` and `search` |
| `read_write` | also `write`, `edit` and `delete` |

The directory is mounted into the add-on either way; this option is what decides.
It defaults to read-only because a broken `configuration.yaml` is the one mistake
that can keep Home Assistant from starting — and then nothing, this add-on included,
can reach it to fix the file.

With `read_write`, every change is still bounded:

- the previous version of the file is copied to `share/ha-mcp/backups` **first**, and
  the change is refused if that copy cannot be made;
- a `.yaml` file is parsed before it is written (Home Assistant's own tags such as
  `!include` and `!secret` are understood), and invalid YAML is refused;
- the file is written beside the original and renamed over it, so Home Assistant
  never reads half a file;
- `secrets.yaml` and everything in `.storage` are never written, and login data
  (`.storage/auth*`, `.storage/onboarding`, `.cloud`) is never read at all.

### Option: `readonly_token` (optional)

A second bearer token, for clients that should be able to look but not change
anything. Leave it empty to disable. A client using it:

- sees only the tools it can use, and gets instructions that say it is read-only;
- may call every tool marked read-only (`ha_overview`, `ha_search`, `ha_states`,
  `ha_history`, `ha_statistics`, `ha_logbook`, `ha_error_log`, `ha_traces`,
  `ha_template`, `ha_config_get`, `ha_check_config`, `ha_camera`, `ha_registry`,
  `ha_addons`);
- may use the escape hatches only for what cannot change anything: `ha_rest` and
  `ha_supervisor` with `GET`, `ha_ws` with list/get-style commands (never anything
  under `auth/`), `ha_file` list/read/search, `ha_dashboard` list/get and
  `ha_addon_action` info/logs.

Anything else is refused before it reaches Home Assistant. An SSE session opened with
the read-only token stays read-only for its lifetime.

Read-only is not the same as harmless to share. It can read every entity, every
automation, the logs, and through `ha_supervisor` `GET` the configuration of your
add-ons — including passwords stored in their options. Downloading backups is not
allowed, because a backup contains `secrets.yaml`. Give this token to a client you
would trust to look around the whole house, not to someone who should see only part
of it.

### Option: `timezone` (optional)

An IANA time zone name such as `Europe/Brussels`. Leave it empty — the add-on then
asks Home Assistant which time zone it runs in, which is correct in nearly every case.

It matters because relative questions are turned into absolute timestamps here. Asking
for "the last 24 hours" of history on an installation that is not on UTC, with the
wrong time zone configured, silently shifts every answer by the offset.

## Network

| Port | Published | Purpose |
|---|---|---|
| `8099/tcp` | yes | `POST /mcp` streamable HTTP · `GET /sse` SSE transport · `GET /health` |
| `8098/tcp` | no | the add-on page, reachable only through Supervisor ingress |

`/health` is deliberately **not** authenticated: the container's Docker `HEALTHCHECK`
polls it and cannot send a bearer token. A container that stops answering is restarted.
The endpoint returns only `{"status": "ok", "version": …, "tools": 26}`.

Both `/mcp` and `/sse` require `Authorization: Bearer <token>` — the full token, or
the read-only one if you configured it.

## Reaching it from a machine that is not on your network

The endpoint is plain HTTP and the token travels in a header, so anything that exposes
port 8099 to the internet — a port forward, a DMZ rule, a naked reverse proxy — puts an
administrative credential for your house on the wire in the clear. Two ways to do this
properly:

- **A VPN into your own network**, which is the simplest and the one to prefer.
  WireGuard or Tailscale, both available as add-ons; the client then uses exactly the
  same `http://<host>:8099/mcp` address as it would at home.
- **A reverse proxy that terminates TLS**, if the client cannot hold a VPN. Give it its
  own hostname and certificate, proxy to `<host>:8099`, and pass the `Authorization`
  header through untouched. The bearer token stays the only thing standing between the
  internet and your installation, so treat a leaked token as a break-in and rotate it.

Home Assistant Cloud (Nabu Casa) does **not** cover this: it publishes Home Assistant
itself, not an add-on's own port.

## Connecting a client

### Claude Code

```bash
claude mcp add --transport http home-assistant \
  http://homeassistant.local:8099/mcp \
  --header "Authorization: Bearer YOUR_TOKEN"
```

### Claude Desktop, or any stdio-only client

Bridge with [`mcp-remote`][mcp-remote]:

```json
{
  "mcpServers": {
    "home-assistant": {
      "command": "npx",
      "args": [
        "-y", "mcp-remote",
        "http://homeassistant.local:8099/mcp",
        "--header", "Authorization: Bearer YOUR_TOKEN"
      ]
    }
  }
}
```

### Protocol notes

- `/mcp` is **stateless**: one HTTP request carries one JSON-RPC message and the
  response comes straight back. Notifications get `202 No Content`. JSON-RPC batch
  arrays are accepted.
- `/sse` opens an event stream and announces its message endpoint in the first
  `endpoint` event, as `POST /messages?session_id=…`. A `: ping` comment is sent every
  15 seconds so intermediate routers do not drop the connection.
- Supported methods: `initialize`, `ping`, `tools/list`, `tools/call`, and the
  `notifications/initialized` and `notifications/cancelled` notifications. There are no
  resources or prompts — everything is a tool.
- `initialize` returns short `instructions`: where to start, that changes are backed up,
  and whether the connection is read-only.
- Every tool carries MCP annotations (`readOnlyHint`, `destructiveHint`), which clients
  can use to decide what to ask your permission for.

## Tool reference

Every tool returns a JSON document as text. Failures come back with `isError: true` and
a `status` / `error` field rather than throwing, so an assistant can read what went
wrong and adjust.

### Finding your way

#### `ha_overview`

No parameters. Everything that usually needs attention, in one answer: Home
Assistant's version and state (including safe or recovery mode), entity counts per
domain, unavailable entities (disabled ones left out), pending updates, open repairs
that are not ignored, integrations that failed to load, add-ons in an error state,
persistent notifications, and how many errors and warnings are in the log.

#### `ha_search`

| Parameter | Type | Notes |
|---|---|---|
| `query` | string | Words, or an exact `entity_id` |
| `domain` | string | Only this domain |
| `area` | string | Only this area, by name or id |
| `in_config` | boolean | Also search the text of every automation and script configuration |
| `limit` | number | Default 25 |

Matches words against the entity id, friendly name, area, device and aliases, and
tolerates a typo. Results carry a `score` (100 is an exact id).

With an **exact entity id** as `query`, the answer also has `used_by` — every
automation, script, scene, group and person that refers to it, YAML-defined ones
included — and `belongs_to`: its device, area and integration. That comes from Home
Assistant's own `search/related`, the same index behind the *Related* tab in the UI.
Dashboards and template sensors are not in that index; `ha_file` with
`action: search` finds those.

```json
{"query": "zwembad warmtepomp"}
{"query": "sensor.p1_meter_power"}
```

### Reading state

#### `ha_states`

Read entities.

| Parameter | Type | Notes |
|---|---|---|
| `entity_id` | string | A single id, returned with all attributes |
| `domain` | string | Filter a list by domain, e.g. `sensor` |
| `search` | string | Filter on text in the id or the friendly name |

With no parameters it lists every entity as `{entity_id, name, state}`, capped at 400
with `truncated: true` when there are more. With `entity_id` it returns the raw Home
Assistant state object, attributes included.

```json
{"entity_id": "climate.living_room"}
{"domain": "binary_sensor", "search": "door"}
```

#### `ha_history`

| Parameter | Type | Notes |
|---|---|---|
| `entity_id` | string | Comma separated |
| `start` | string | ISO time, or relative: `6h`, `3d`, `2w`. Default 24 hours ago |
| `end` | string | Same formats. Default now |
| `hours` | number | Alternative to `start` |
| `attributes` | boolean | Include attributes — much larger |
| `all_changes` | boolean | Every change, not only significant ones |

Returns each entity as `{"changes": n, "history": [["2026-09-28 10:06:10", "heat"], …]}`,
in Home Assistant's time zone — about a quarter of the size of the REST format, which
repeats the entity id and two timestamps on every row. Without `entity_id` it falls
back to the REST answer for everything.

State history is purged after ten days by default. For anything older, use
`ha_statistics`.

#### `ha_statistics`

| Parameter | Type | Notes |
|---|---|---|
| `statistic_ids` | string | **Required.** Entity ids, comma separated |
| `start` / `end` | string | As for history. Default: the last 30 days |
| `period` | string | `5minute`, `hour`, `day` (default), `week`, `month` |
| `types` | string | Subset of `mean,min,max,sum,state,change` |

The recorder's long-term statistics, kept for years: mean, min and max per period for
measurements, `sum` and `change` for meters. `change` per `day` is the daily
consumption of an energy sensor. Only entities with a `state_class` have statistics;
the answer says which ids had none.

#### `ha_logbook`

Who or what changed something, and when. Parameters: `entity_id` (optional), and
`start`/`end`/`hours` as for history. This is the tool that answers "why did the
light come on".

#### `ha_traces`

| Parameter | Type | Notes |
|---|---|---|
| `entity_id` | string | **Required.** `automation.*` or `script.*` |
| `run_id` | string | One run, from the list |
| `limit` | number | Runs to list, default 10 |
| `sections` | string | Only `trigger`, `condition`, `action`, `config`, `error` |

Without `run_id`: the stored runs, newest first, with what triggered each, how it
ended and its last step. With `run_id`: that run reduced to what explains it — the
trigger (entity, from and to state), each condition with its result, each action
step with its result and the variables it changed (shown once, not at every step).

When there are no traces the answer says why: the automation does not exist, is
switched off, has never run, or ran but its traces are gone (they are kept in memory
and lost on a restart).

#### `ha_template`

Render a Jinja2 template inside Home Assistant. Parameter: `template`. The most
efficient way to compute or summarise across many entities at once, because the work
happens in Home Assistant instead of in the model's context.

```json
{"template": "{{ states.sensor | selectattr('state','gt','30') | map(attribute='entity_id') | list }}"}
```

### Acting

#### `ha_service`

Call any service in any domain. `homeassistant.restart`, `homeassistant.stop` and
`hassio.host_reboot` first run the configuration check and are refused while it
fails; pass `skip_config_check: true` if you are certain.

| Parameter | Type | Notes |
|---|---|---|
| `domain` | string | **Required.** e.g. `light` |
| `service` | string | **Required.** e.g. `turn_on` |
| `entity_id` | string | Target entities, comma separated |
| `data_json` | string | Extra service data, as JSON text |
| `return_response` | boolean | For services that return data |
| `skip_config_check` | boolean | Restart even if the configuration check fails |

```json
{"domain": "light", "service": "turn_on", "entity_id": "light.kitchen",
 "data_json": "{\"brightness_pct\": 40}"}
```

### Configuration

#### `ha_config_get` · `ha_config_save` · `ha_config_delete`

Read, write and remove automations, scripts, scenes and helpers. `kind` is one of
`automation`, `script`, `scene`, `input_boolean`, `input_number`, `input_select`,
`input_text`, `template`.

- `ha_config_get` with `kind` only lists everything of that kind; add `object_id` for
  one.
- `ha_config_save` takes `kind`, `object_id` and `config_json`. **It replaces the
  entire configuration** — read the object first unless you are creating a new one. A
  fresh `object_id` creates a new object.
- `ha_config_delete` takes `kind` and `object_id`.

Both first save the current version to `share/ha-mcp/backups` and put its path in the
answer as `backup`; if the current version cannot be read, nothing is changed.

For automations and scripts, `ha_config_save` then adds a `review`:

- `missing` — every `entity_id` and service/action named in the configuration that
  does not exist. Home Assistant accepts those at save time and fails only when it
  runs. Templates are not evaluated, and blueprints are skipped.
- `advice` — constructs that a native trigger or condition expresses better: a
  numeric comparison, the time of day or the weekday computed in a template,
  `now() - last_changed`, `states.x.y.state`, a device trigger, `wait_template`,
  `service_template`, a motion automation with a delay in `mode: single`. Advice
  only; the automation was saved.

#### `ha_check_config`

No parameters. Runs the same check as *Settings → System → Restart → Check
configuration* and returns `{"result": "valid" | "invalid", "errors": …}`.

#### `ha_file`

| Parameter | Type | Notes |
|---|---|---|
| `action` | string | `list`, `read` (default), `search`, `write`, `edit`, `delete` |
| `path` | string | Relative to the configuration directory; `/config/…` is understood too |
| `content` | string | `write`: the complete new content |
| `old_text` / `new_text` | string | `edit`: replace one exact piece of text, which must occur exactly once |
| `query` | string | `search`: a case-insensitive regular expression |
| `pattern` | string | `list`/`search`: file name pattern; `search` defaults to `*.yaml` |
| `recursive` | boolean | `list`: include subdirectories |
| `start_line` / `max_lines` | number | `read`: a range of lines |

Writing needs `file_access: read_write`; see the option for what is backed up,
validated and never touched. `edit` is the one to prefer for a change in a large
file: it sends only the part that changes. After a write the answer says what makes
the change take effect (`automation.reload`, `template.reload`, or a check and a
restart).

```json
{"action": "search", "query": "sensor\\.zwembad_temperatuur"}
{"action": "edit", "path": "template.yaml", "old_text": "unit_of_measurement: W", "new_text": "unit_of_measurement: kW"}
```

#### `ha_dashboard`

| Parameter | Type | Notes |
|---|---|---|
| `action` | string | `list`, `get` (default), `patch` |
| `url_path` | string | The dashboard; omit for the default one |
| `path` | string | `get`: a JSON pointer, e.g. `/views/2/sections/0` |
| `summary` | boolean | `get` without `path`: `false` returns everything |
| `patch_json` | string | `patch`: a JSON Patch list — `add`, `replace`, `remove`, `move` |

`get` without `path` returns an outline: each view's index, path, title and number of
sections or cards. `patch` reads the current configuration, backs it up, applies every
operation, and saves only if all of them succeeded. A real dashboard is hundreds of
kilobytes; a patch sends only what changes.

```json
{"action": "patch", "url_path": "dashboard-klimaat", "patch_json":
 "[{\"op\": \"replace\", \"path\": \"/views/0/title\", \"value\": \"Overzicht\"}]"}
```

#### `ha_camera`

`entity_id` (a `camera.*`) and `width` (default 1024). Returns the snapshot as an MCP
image, which a model that reads images can look at directly. Home Assistant scales it.

These write to `automations.yaml` and friends exactly as the UI editors do, and the
change is live immediately.

#### `ha_registry`

List a registry. `what` is one of `entities`, `devices`, `areas`, `floors`, `labels`,
`integrations`. This is what maps an entity to the device, area and floor it belongs
to — information the state API does not carry.

#### `ha_expose`

Expose entities to Assist or hide them again. Parameters: `entity_ids` (comma
separated), `expose` (boolean, default `true`). This is what decides what the voice
assistant can see.

### Diagnostics

#### `ha_error_log`

| Parameter | Type | Notes |
|---|---|---|
| `source` | string | `errors`, `core` (default), `host`, `supervisor` |
| `lines` | number | Lines or entries to return, default 100, at most 5000 |
| `search` | string | Only lines containing this text, case-insensitive |
| `level` | string | Only this level and worse: `DEBUG` … `CRITICAL` |

`errors` is Home Assistant's own list of distinct warnings and errors since the last
start (`system_log/list`): each with how often it happened, when first and last, the
logger, the source line and the traceback. Start there.

`core`, `host` and `supervisor` are the raw logs, served through the Supervisor
because the core `/api/error_log` endpoint was removed in Home Assistant 2026.9. The
Supervisor is asked for exactly the last `lines` entries; with `search` or `level` it
reads the last 3000 and filters those. ANSI colour codes are stripped. `lines_read`
says how much was read, `matches` how much passed the filter.

### Add-ons, backups and the host

#### `ha_addons`

List every installed add-on as `{slug, name, state, version}`. No parameters.

#### `ha_addon_action`

Parameters: `slug`, and `action` — one of `info`, `logs`, `start`, `stop`, `restart`,
`update`. For `logs`, `lines` sets how many (default 200).

#### `ha_supervisor`

Any Supervisor API call: host, OS, network, backups, the add-on store. Parameters:
`endpoint`, `method` (default `GET`), `body_json`. A `POST` to `/core/restart`,
`/core/rebuild` or `/host/reboot` checks the configuration first, like `ha_service`;
`skip_config_check: true` overrides that.

```json
{"endpoint": "/backups"}
{"endpoint": "/backups/new/full", "method": "POST", "body_json": "{\"name\": \"before refactor\"}"}
```

#### `ha_download`

Fetch an endpoint and write the result as a **file** into the `share` folder,
subdirectory `ha-mcp`. Parameters: `endpoint`, `target` (`supervisor` by default, or
`core`). Use this for anything binary — a backup, a camera snapshot.

#### `ha_upload`

Send a file from `share/ha-mcp` to an endpoint as a multipart upload. Parameters:
`file` (a name inside `ha-mcp`, or an absolute path), `endpoint`, `field` (default
`file`), `target`. This is what makes restoring a backup reachable: drop the `.tar`
into the share over Samba, then point at it.

### Escape hatches

#### `ha_rest`

Any REST call. Parameters: `path` (with or without the `/api` prefix), `method`
(default `GET`), `body_json`.

#### `ha_ws`

Any WebSocket command. Parameters: `command`, `params_json`. The web interface uses
the WebSocket API for nearly everything, so this reaches what REST does not offer:
dashboards, users, backup details, integration config entries.

```json
{"command": "lovelace/config", "params_json": "{\"url_path\": null}"}
```

## Large and binary responses

An MCP response is text, and a model's context is finite. Two safeguards follow from
that:

- **Binary payloads** (a `.tar`, an image) are written to `/share/ha-mcp/…` and the
  tool returns `{"binary": true, "file": "…", "bytes": …}` instead of mangled text.
- **Text over 100 000 characters** is truncated, but the *complete* response is written
  to `/share/ha-mcp/<tool>-<timestamp>.json` first and the final line says where. A
  truncated JSON document is unparseable and the remainder would otherwise be gone.

Both land in Home Assistant's `share` folder, reachable over Samba or the File editor
add-on.

## Backups of every change

`ha_config_save`, `ha_config_delete`, `ha_file` (write, edit, delete) and
`ha_dashboard` (patch) copy the current version to `share/ha-mcp/backups` before they
change anything, and refuse to change it when that copy fails. Each answer names the
file in `backup`. The names start with a timestamp, so the folder sorts in the order
things happened; the newest 300 are kept.

To undo a change, give the backup back: `ha_config_save` with the saved JSON,
`ha_file` `write` with the saved file, or `ha_dashboard` — the saved dashboard is the
complete configuration, which `ha_ws` `lovelace/config/save` accepts as is.

These are not Home Assistant backups. For a full backup, use `ha_supervisor` with
`/backups/new/full`.

## Security

The token is the entire security boundary.

- The generated token is 32 random bytes from `secrets.token_hex`. If you set one by
  hand, match that: `openssl rand -hex 32`.
- The add-on page is on an unpublished port that only the Supervisor can reach, it is
  never cached (`Cache-Control: no-store`), and it prints the token only for accounts
  in the `system-admin` group.
- **Do not expose port 8099 to the internet.** The transport is plain HTTP and the
  token would cross the network in the clear. Use a VPN, or a reverse proxy that
  terminates TLS.
- Treat the token as an administrative credential wherever you store it, and rotate it
  if a machine holding it is lost.
- Give a client that only needs to look the `readonly_token` instead.
- `file_access` stays `read_only` unless you need the model to edit YAML. Whatever it
  is set to, login data is never read and `secrets.yaml` is only ever shown masked.

The add-on itself stores no Home Assistant credential. It authenticates to the core
using the `SUPERVISOR_TOKEN` the Supervisor places in its environment, which is rotated
by the Supervisor and disappears with the container.

## Troubleshooting

**The log warns that the generated token could not be stored.**
`/data` is not writable, which means a new token on every start and clients that stop
working after a restart. Set `token` on the Configuration tab to a fixed value, and
check the add-on's storage in the Supervisor.

**The add-on page says it cannot establish that you are an administrator.**
It asks Home Assistant for the user list over the WebSocket API; that call failed. The
page still works and the token is in the add-on log. If it persists, the core was
probably not up yet — restart the add-on.

**`No SUPERVISOR_TOKEN in the environment`.**
The add-on is not running under the Supervisor. It requires Home Assistant OS or
Supervised; it cannot work on Home Assistant Container or Core.

**The client connects but lists no tools.**
Check that you are pointing at `/mcp` (or `/sse`), not at the bare host and port. A
bare `/` returns `404 unknown path`.

**Every call returns 401.**
The header must be exactly `Authorization: Bearer <token>`, and the token must match
the configured one character for character. Leading or trailing whitespace in the
configuration field is stripped; whitespace in the client's header is not.

**`connection to Home Assistant: HTTP 0` at start-up.**
The core was not up yet. The add-on starts after Home Assistant and the health check
restarts it, but if this persists, check the core log with `ha_error_log` or the
Supervisor UI.

**History or logbook answers are shifted by a few hours.**
The time zone is wrong. Check the `time zone:` line in the add-on log, and set the
`timezone` option explicitly if Home Assistant's own setting is not what you expect.

**A response mentions a file in `/share/ha-mcp` that you cannot find.**
That is Home Assistant's `share` folder — open the Samba share named `share`, or use
the File editor add-on, and look in the `ha-mcp` subdirectory.

**`ha_file` says the configuration directory is not mounted.**
The mount is new in 2.2.0 and is set up when the add-on container is created. Restart
the add-on once after updating.

**`ha_file` refuses to write.**
`file_access` is `read_only` (the default). Set it to `read_write` on the Configuration
tab and restart the add-on.

**A restart is refused with "the configuration check did not pass".**
That is the check doing its job: Home Assistant would not come back up. Read the
errors in the answer, fix them, and run `ha_check_config` until it says `valid`.

## Support

Open an issue at <https://github.com/MatthiasVanDE/hassio-mcp-server/issues>. Include
the add-on version, your Home Assistant version, the relevant add-on log lines at
`log_level: debug`, and which MCP client you are using.

[mcp]: https://modelcontextprotocol.io
[mcp-remote]: https://www.npmjs.com/package/mcp-remote
