# Contributing

Thanks for taking the time. Issues and pull requests are both welcome.

## Reporting a bug

Open an issue using the bug report template and include:

- the add-on version and your Home Assistant / Supervisor version,
- the add-on log with `log_level: debug`, trimmed to the relevant lines,
- which MCP client you use, and the tool call that misbehaved.

For anything security-related, see [SECURITY.md](SECURITY.md) instead — please do not
open a public issue.

## Working on the code

The whole server is [`ha_mcp_server/server.py`](ha_mcp_server/server.py). Its only
runtime dependency is `websockets`; everything else is the standard library. That is
deliberate — the add-on holds administrative credentials, and a small dependency
surface is part of how it stays auditable. Please do not add a web framework.

### Running the transport locally

You do not need Home Assistant to exercise the MCP layer. Import the module, set a
token and start the handler:

```python
import importlib.util, threading, time
from http.server import ThreadingHTTPServer

spec = importlib.util.spec_from_file_location("srv", "ha_mcp_server/server.py")
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
m.TOKEN = "test-token"
srv = ThreadingHTTPServer(("127.0.0.1", 18099), m.Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
```

`/health`, `/mcp` with `initialize` and `tools/list`, authentication and error handling
all work without a Supervisor. Any tool that actually calls Home Assistant will return
`{"status": 0, "error": …}`, which is itself the correct behaviour to verify.

### Testing against a real Supervisor

1. Copy the `ha_mcp_server` folder into your Home Assistant `/addons` directory (the
   `addons` Samba share).
2. **Settings → Add-ons → Add-on Store → ⋮ → Check for updates**; the add-on appears
   under *Local add-ons*.
3. Install, set a token, start, and watch the Log tab.

Rebuild after a change with **⋮ → Rebuild** on the add-on page.

## Adding a tool

A tool is one function plus one entry in `TOOLS`:

```python
def t_example(a):
    return core("GET", f"/example/{a['thing']}")

TOOLS["ha_example"] = (
    T("One sentence that tells a model exactly when to reach for this.",
      {"thing": S("What it identifies.", "kitchen")}, ["thing"]),
    t_example,
)
```

Two things matter more than they look:

- **The description is a prompt.** It is read by a language model deciding whether to
  call your tool. Say what it does and when to use it, in plain English, in one or two
  sentences.
- **Return data, not prose.** Return a dict or a list; the server serialises it. Signal
  failure with an `error` key rather than raising, so the model can read what went
  wrong and correct itself.

## Style

Match what is there: plain Python, no framework, and comments that explain *why* a
decision was made rather than restating the code. Keep lines under 120 characters.

## Pull requests

- One change per pull request.
- Update [`ha_mcp_server/CHANGELOG.md`](ha_mcp_server/CHANGELOG.md) under an
  *Unreleased* heading.
- Bump `version` in `config.yaml` only if you are asked to; releases are cut in one go.
- CI runs yamllint, hadolint, the Home Assistant add-on linter and a real Docker build.
  Green CI is expected before review.

## Code of conduct

Be decent to people. See [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).
