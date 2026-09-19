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
4. Click **OPEN WEB UI** (or the **MCP Server** entry in the sidebar). The page shows
   the endpoint, the token and a ready-made client configuration, each with a copy
   button.

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
The endpoint returns only `{"status": "ok", "version": …, "tools": 18}`.

Both `/mcp` and `/sse` require `Authorization: Bearer <token>`.

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

## Tool reference

Every tool returns a JSON document as text. Failures come back with `isError: true` and
a `status` / `error` field rather than throwing, so an assistant can read what went
wrong and adjust.

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

State history. Parameters: `entity_id` (comma separated, optional) and `hours`
(default 24). Requests are made with `minimal_response` and `no_attributes`, so the
answer stays small enough to reason over.

#### `ha_logbook`

Who or what changed something, and when. Parameters: `entity_id` (optional),
`hours` (default 24). This is the tool that answers "why did the light come on".

#### `ha_template`

Render a Jinja2 template inside Home Assistant. Parameter: `template`. The most
efficient way to compute or summarise across many entities at once, because the work
happens in Home Assistant instead of in the model's context.

```json
{"template": "{{ states.sensor | selectattr('state','gt','30') | map(attribute='entity_id') | list }}"}
```

### Acting

#### `ha_service`

Call any service in any domain.

| Parameter | Type | Notes |
|---|---|---|
| `domain` | string | **Required.** e.g. `light` |
| `service` | string | **Required.** e.g. `turn_on` |
| `entity_id` | string | Target entities, comma separated |
| `data_json` | string | Extra service data, as JSON text |
| `return_response` | boolean | For services that return data |

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
| `source` | string | `core` (default), `host`, `supervisor` |
| `lines` | number | Trailing lines, default 100 |

Served through the Supervisor's `/core/logs`, `/host/logs` and `/supervisor/logs`,
because the core `/api/error_log` endpoint was removed in Home Assistant 2026.9. ANSI
colour codes are stripped, and `total_lines` tells you how much was there in full.

### Add-ons, backups and the host

#### `ha_addons`

List every installed add-on as `{slug, name, state, version}`. No parameters.

#### `ha_addon_action`

Parameters: `slug`, and `action` — one of `info`, `logs`, `start`, `stop`, `restart`,
`update`.

#### `ha_supervisor`

Any Supervisor API call: host, OS, network, backups, the add-on store. Parameters:
`endpoint`, `method` (default `GET`), `body_json`.

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

## Support

Open an issue at <https://github.com/MatthiasVanDE/hassio-mcp-server/issues>. Include
the add-on version, your Home Assistant version, the relevant add-on log lines at
`log_level: debug`, and which MCP client you are using.

[mcp]: https://modelcontextprotocol.io
[mcp-remote]: https://www.npmjs.com/package/mcp-remote
