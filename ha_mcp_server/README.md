# MCP Server (Home Assistant API)

The add-on itself. See:

- **[DOCS.md](DOCS.md)** — installation, every option, the full tool reference,
  security notes and troubleshooting. This is what the add-on's Documentation tab shows.
- **[CHANGELOG.md](CHANGELOG.md)** — what changed, and what breaks.
- **[../README.md](../README.md)** — what this is and why it exists.

| File | Role |
|---|---|
| `server.py` | The entire MCP server: transports, JSON-RPC, all 18 tools, the add-on page |
| `config.yaml` | Add-on manifest: permissions, ports, options |
| `Dockerfile` | Built by CI and published to GHCR; the Supervisor only pulls |
| `translations/en.yaml` | Option labels shown in the Home Assistant UI |
