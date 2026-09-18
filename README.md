# Home Assistant Add-on: MCP Server

[![License: MIT][license-badge]][license]
[![Supports aarch64][aarch64-badge]](#requirements)
[![Supports amd64][amd64-badge]](#requirements)
[![Supports armv7][armv7-badge]](#requirements)
[![Supports i386][i386-badge]](#requirements)

**Give an AI assistant the same reach over Home Assistant that you have.**

This add-on turns the complete Home Assistant **REST API**, **WebSocket API** and
**Supervisor API** into [Model Context Protocol][mcp] tools, served over HTTP from
the machine Home Assistant already runs on. An MCP client — Claude, or any other —
can then read state and history, call any service, render templates, create and edit
automations, browse the device and area registries, read the core and host logs, and
manage add-ons and backups.

[![Open your Home Assistant instance and show the add add-on repository dialog with a specific repository URL pre-filled.][repo-badge]][repo-link]

---

## Why not the built-in `mcp_server` integration?

Home Assistant ships an MCP server integration. It is a good fit for voice-style
requests and a deliberately narrow one. This add-on solves a different problem.

| | Built-in `mcp_server` | This add-on |
|---|---|---|
| Entities visible | only those exposed to Assist | every entity, always |
| Services | the Assist intent set | every service in every domain |
| Create or edit automations | no | yes |
| Registries (devices, areas, floors, labels) | no | yes |
| Read core / host / Supervisor logs | no | yes |
| Manage add-ons, backups, the host | no | yes |
| Arbitrary REST / WebSocket calls | no | yes |
| Runs when your laptop is closed | n/a | yes — it lives on the HA machine |

The short version: the built-in integration is designed to let an assistant *operate*
your home. This add-on is designed to let an assistant *work on* it — diagnose it,
refactor it, and build it out. That is a much larger grant of authority, which is why
[security](#security) is not an afterthought below.

## Highlights

- **18 tools** covering state, history, logbook, services, templates, config,
  registries, logs, add-ons, backups and raw API access.
- **No long-lived access token.** The add-on authenticates to Home Assistant with the
  `SUPERVISOR_TOKEN` the Supervisor injects into the container. That token is managed
  and rotated by the Supervisor and never appears in a configuration file.
- **Two MCP transports**: stateless streamable HTTP on `/mcp`, and the older SSE
  transport on `/sse`, because clients differ in what they support.
- **Binary data is handled honestly.** An MCP response is text, so a backup or a camera
  snapshot cannot travel inside one. Those land in the `share` folder and the tool
  returns the path. Over-long text responses are written there too rather than being
  silently cut into unparseable JSON.
- **Watchdog-backed.** If `/health` stops answering, the Supervisor restarts it.
- **One file, one dependency.** `server.py` plus `websockets`. Nothing to audit but the
  thing itself.

## Requirements

- A **Home Assistant OS** or **Home Assistant Supervised** installation. The add-on
  needs the Supervisor; it will not run on Home Assistant Container or Core.
- Architecture `aarch64`, `amd64`, `armv7` or `i386`.
- An MCP client that can reach the Home Assistant machine over HTTP.

## Installation

1. Add this repository to your Home Assistant instance — click the badge above, or go
   to **Settings → Add-ons → Add-on Store → ⋮ → Repositories** and add:

   ```text
   https://github.com/MatthiasVanDE/hassio-mcp-server
   ```

2. Find **MCP Server (Home Assistant API)** in the store and click **Install**. The
   Supervisor builds the image on your own machine; on a Raspberry Pi expect a few
   minutes.
3. Open the **Configuration** tab and set a `token`. Generate a real one:

   ```bash
   openssl rand -hex 32
   ```

   The add-on **refuses to start without a token** — see [security](#security).
4. **Start** the add-on and check the **Log** tab. A healthy start looks like this:

   ```text
   [09:12:04] INFO    connection to Home Assistant: HTTP 200 {'message': 'API running.'}
   [09:12:04] INFO    time zone: Europe/Brussels (from Home Assistant)
   [09:12:04] INFO    18 tools available on port 8099 (/mcp, /sse, /health)
   ```

Full option reference, tool-by-tool documentation and troubleshooting live in
**[`ha_mcp_server/DOCS.md`](ha_mcp_server/DOCS.md)**, which is also shown on the
add-on's Documentation tab once installed.

## Connecting a client

The endpoint is `http://<home-assistant-host>:8099/mcp`, with your token in an
`Authorization: Bearer` header.

### Claude Code

```bash
claude mcp add --transport http home-assistant \
  http://homeassistant.local:8099/mcp \
  --header "Authorization: Bearer YOUR_TOKEN"
```

### Claude Desktop, and any client that only speaks stdio

Bridge with [`mcp-remote`][mcp-remote]. In `claude_desktop_config.json`:

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

### Anything else

Point the client at `/mcp` for streamable HTTP, or `/sse` for the SSE transport
(its message endpoint is announced in the first `endpoint` event). Both require the
same bearer token.

### Verifying by hand

```bash
curl -s http://homeassistant.local:8099/health
# {"status": "ok", "version": "2.0.0", "tools": 18}

curl -s http://homeassistant.local:8099/mcp \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | jq '.result.tools | length'
# 18
```

## The tools

| Tool | What it does |
|---|---|
| `ha_states` | Read one entity in full, or a filtered list by domain or search text |
| `ha_service` | Call any service in any domain |
| `ha_template` | Render a Jinja2 template inside Home Assistant |
| `ha_history` | State history over a period |
| `ha_logbook` | Who or what changed something, and when |
| `ha_error_log` | The core, host or Supervisor log, ANSI codes stripped |
| `ha_config_get` | Read an automation, script, scene or helper |
| `ha_config_save` | Create or overwrite an automation, script, scene or helper |
| `ha_config_delete` | Delete one |
| `ha_registry` | List entities, devices, areas, floors, labels or integrations |
| `ha_expose` | Expose entities to Assist, or hide them |
| `ha_addons` | List installed add-ons and their state |
| `ha_addon_action` | Info, logs, start, stop, restart or update an add-on |
| `ha_download` | Fetch any endpoint and write the result to the `share` folder |
| `ha_upload` | Send a file from `share` to an endpoint as a multipart upload |
| `ha_supervisor` | Any Supervisor API call: host, OS, backups, network, store |
| `ha_rest` | Any Home Assistant REST call — the escape hatch |
| `ha_ws` | Any WebSocket command — reaches what REST does not expose |

Parameters and worked examples for each are in [`DOCS.md`](ha_mcp_server/DOCS.md#tool-reference).

## Security

**Read this part.** This add-on holds Supervisor-level credentials. Anyone who can
reach its port with the right token can do anything you can do in the Home Assistant
UI, including running services, editing automations, reading your logs and downloading
your backups.

- **The token is the entire boundary.** Use a long random value. `openssl rand -hex 32`,
  not the name of your dog. The add-on refuses to start with an empty token rather than
  quietly opening the port.
- **Do not forward port 8099 to the internet.** The transport is plain HTTP; the token
  would cross the network in the clear. Reach it over a VPN (WireGuard, Tailscale) or
  put it behind a reverse proxy that terminates TLS.
- **`/health` is intentionally unauthenticated**, because the Supervisor watchdog cannot
  send a token. It reveals only `ok`, the version and the number of tools.
- **Treat it as an admin credential** in whatever client you configure it in, and rotate
  the token if a machine holding it is lost.

Found a vulnerability? See [SECURITY.md](SECURITY.md) — please do not open a public issue.

## How it works

```text
   MCP client                  Add-on container                Home Assistant
  ┌───────────┐   HTTP+Bearer  ┌──────────────────┐  Supervisor ┌──────────────┐
  │ Claude,   │ ─────────────► │ server.py        │  token      │ Core REST    │
  │ or any    │   /mcp  /sse   │ JSON-RPC ◄─► HA  │ ──────────► │ Core WS      │
  │ MCP host  │ ◄───────────── │ 18 tools         │             │ Supervisor   │
  └───────────┘                └────────┬─────────┘             └──────────────┘
                                        │ files too large or binary
                                        ▼
                                  /share/ha-mcp
```

`server.py` is a single-file MCP server with no framework: `ThreadingHTTPServer` for
transport, `urllib` for REST, `websockets` for the WebSocket API. It never stores a
Home Assistant credential — the `SUPERVISOR_TOKEN` arrives in the environment and dies
with the container.

## Contributing

Issues and pull requests are welcome. Please read [CONTRIBUTING.md](CONTRIBUTING.md)
first; it is short and mostly about how to test a change against a real Supervisor.

## Changelog

See [CHANGELOG.md](ha_mcp_server/CHANGELOG.md).

## License

[MIT](LICENSE) © Matthias Van der Elst.

This project is not affiliated with or endorsed by the Home Assistant project or
Nabu Casa, Inc.

[mcp]: https://modelcontextprotocol.io
[mcp-remote]: https://www.npmjs.com/package/mcp-remote
[license]: LICENSE
[license-badge]: https://img.shields.io/badge/license-MIT-blue.svg
[aarch64-badge]: https://img.shields.io/badge/aarch64-yes-green.svg
[amd64-badge]: https://img.shields.io/badge/amd64-yes-green.svg
[armv7-badge]: https://img.shields.io/badge/armv7-yes-green.svg
[i386-badge]: https://img.shields.io/badge/i386-yes-green.svg
[repo-badge]: https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg
[repo-link]: https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2FMatthiasVanDE%2Fhassio-mcp-server
