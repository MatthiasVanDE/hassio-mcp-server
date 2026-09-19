# Home Assistant Add-on: MCP Server

[![Latest release][release-badge]][releases]
[![License: MIT][license-badge]][license]
[![Supports aarch64][aarch64-badge]](#requirements)
[![Supports amd64][amd64-badge]](#requirements)
[![CI][ci-badge]][ci]

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

- **Installs in seconds.** Prebuilt `aarch64` and `amd64` images, published to GHCR by
  CI for every released version. Nothing compiles on your Raspberry Pi, and every
  installation of a given version is provably the same code.
- **It sets itself up.** Start it and open **OPEN WEB UI**: the endpoint, the token
  behind a *Show secrets* toggle with a copy button, a ready-made command for Claude
  Code, a ready-made JSON configuration for everything else, and the live tool list.
  No token to invent, no documentation to read first.
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
- **Self-healing.** A Docker `HEALTHCHECK` polls `/health`; a container that stops
  answering is restarted.
- **One file, one dependency.** `server.py` plus `websockets`. Nothing to audit but the
  thing itself.

## Requirements

- A **Home Assistant OS** or **Home Assistant Supervised** installation. The add-on
  needs the Supervisor; it will not run on Home Assistant Container or Core.
- Architecture `aarch64` (Raspberry Pi 4/5 and most ARM hardware) or `amd64`. Those
  are the two Home Assistant still supports; `armv7` and `i386` were dropped in Home
  Assistant 2025.12.
- An MCP client that can reach the Home Assistant machine over HTTP.

## Installation

1. Add this repository to your Home Assistant instance — click the badge above, or go
   to **Settings → Add-ons → Add-on Store → ⋮ → Repositories** and add:

   ```text
   https://github.com/MatthiasVanDE/hassio-mcp-server
   ```

2. Find **MCP Server (Home Assistant API)** in the store and click **Install**. A
   prebuilt image is pulled; it takes seconds, and nothing is compiled on your machine.
3. **Start** it. There is nothing to configure first: with no `token` set, the add-on
   generates one, keeps it across restarts and updates, and prints it in the **Log**
   tab. A healthy start looks like this:

   ```text
   [09:12:04] INFO    No token was configured, so one was generated for you:
   [09:12:04] INFO        3f7c…
   [09:12:04] INFO    connection to Home Assistant: HTTP 200 {'message': 'API running.'}
   [09:12:04] INFO    time zone: Europe/Brussels (from Home Assistant)
   [09:12:04] INFO    clients should connect to http://192.168.0.16:8099/mcp
   [09:12:04] INFO    18 tools available on port 8099 (/mcp, /sse, /health)
   [09:12:04] INFO    add-on page on ingress port 8098
   ```

4. Click **OPEN WEB UI**. The page shows the endpoint and the token with copy buttons,
   and a configuration you can paste straight into your client. Prefer to choose the
   token yourself? Set `token` in the **Configuration** tab and restart; it wins over
   the generated one.

Full option reference, tool-by-tool documentation and troubleshooting live in
**[`ha_mcp_server/DOCS.md`](ha_mcp_server/DOCS.md)**, which is also shown on the
add-on's Documentation tab once installed.

## Connecting a client

**The add-on's own page has all of this filled in for you**, with your address and
your token: click **OPEN WEB UI** on the add-on. (Flip **Show in sidebar** on the same
page and it gets a permanent sidebar entry.) What follows is the same thing, spelled
out.

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
# {"status": "ok", "version": "2.1.1", "tools": 18}

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

- **The token is the entire boundary.** Left to itself the add-on generates 32 random
  bytes and keeps them in `/data`; if you set one by hand, use a value of that calibre
  (`openssl rand -hex 32`, not the name of your dog). The port is never open without
  one.
- **Do not forward port 8099 to the internet.** The transport is plain HTTP; the token
  would cross the network in the clear. Reach it over a VPN (WireGuard, Tailscale) or
  put it behind a reverse proxy that terminates TLS.
- **The add-on's page shows the token only to administrators.** Ingress authenticates
  whoever opens it, but does not by itself keep non-administrators out, so the page
  checks `system-admin` group membership itself before printing the token — and says
  so when it will not. Its port is not published to your network; only the Supervisor
  can reach it.
- **`/health` is intentionally unauthenticated**, because the container's health check
  cannot send a token. It reveals only `ok`, the version and the number of tools.
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
[releases]: https://github.com/MatthiasVanDE/hassio-mcp-server/releases
[release-badge]: https://img.shields.io/github/v/release/MatthiasVanDE/hassio-mcp-server?label=add-on&color=41bdf5
[mcp-remote]: https://www.npmjs.com/package/mcp-remote
[license]: LICENSE
[license-badge]: https://img.shields.io/badge/license-MIT-blue.svg
[aarch64-badge]: https://img.shields.io/badge/aarch64-yes-green.svg
[amd64-badge]: https://img.shields.io/badge/amd64-yes-green.svg
[ci]: https://github.com/MatthiasVanDE/hassio-mcp-server/actions/workflows/ci.yaml
[ci-badge]: https://github.com/MatthiasVanDE/hassio-mcp-server/actions/workflows/ci.yaml/badge.svg
[repo-badge]: https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg
[repo-link]: https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2FMatthiasVanDE%2Fhassio-mcp-server
