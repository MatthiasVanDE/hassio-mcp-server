#!/usr/bin/env python3
"""MCP server exposing the Home Assistant APIs, running as a Supervisor add-on.

Two transports, because MCP clients differ in what they support:
  POST /mcp     streamable HTTP, stateless -- one request, one response
  GET  /sse     the older SSE transport, paired with POST /messages?session_id=...
  GET  /health  unauthenticated liveness probe, used by the Supervisor watchdog

Access to Home Assistant does NOT go through a long-lived access token but through
the SUPERVISOR_TOKEN that the Supervisor injects into the container. That token is
rotated by the Supervisor and never appears in a configuration file.
"""
import asyncio, json, os, queue, secrets, sys, threading
import re, signal, urllib.error, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

VERSION = "2.0.0"
SUPERVISOR = os.environ.get("SUPERVISOR_TOKEN", "")
CORE_REST = "http://supervisor/core/api"
CORE_WS = "ws://supervisor/core/websocket"
SUP_REST = "http://supervisor"
PORT = 8099
MAX_CHARS = 100_000
MAX_BODY = 16 * 1024 * 1024
# Binary data does not fit in an MCP response (which is text). Such payloads are
# written here, into the `share` folder, and the tool returns the path instead.
SHARE = "/share/ha-mcp"


def opts():
    try:
        return json.load(open("/data/options.json"))
    except Exception:
        return {}


OPTIONS = opts()
TOKEN = (OPTIONS.get("token") or "").strip()
LEVELS = {"debug": 10, "info": 20, "warning": 30, "error": 40}
LEVEL = LEVELS.get(OPTIONS.get("log_level", "info"), 20)
# Replaced in main() by the time zone reported by Home Assistant. Until then UTC,
# so that a log line written during start-up still carries a valid timestamp.
TZ = timezone.utc


def log(msg, level="info"):
    if LEVELS.get(level, 20) >= LEVEL:
        print(f"[{datetime.now(TZ).strftime('%H:%M:%S')}] {level.upper():<7} {msg}", flush=True)


def clip(text, name="result"):
    """Truncate an over-long response, but keep the complete result as a file.

    Without that safety net a truncated JSON response is unparseable text and the
    remainder is gone for good. Now the full response sits in the share folder and
    the last line says where.
    """
    if len(text) <= MAX_CHARS:
        return text
    try:
        os.makedirs(SHARE, exist_ok=True)
        path = os.path.join(SHARE, f"{name}-{datetime.now(TZ).strftime('%Y%m%d-%H%M%S')}.json")
        with open(path, "w") as f:
            f.write(text)
        tail = (f"\n\n[... truncated after {MAX_CHARS} of {len(text)} characters. The COMPLETE "
                f"response was written to {path}, reachable through the `share` folder.]")
    except Exception as e:
        tail = f"\n\n[... truncated, {len(text)-MAX_CHARS} characters dropped; writing the file failed: {e}]"
    return text[:MAX_CHARS] + tail


ANSI = re.compile(r"\x1b\[[0-9;]*m")

# ------------------------------------------------------------ Home Assistant access
def _http(base, method, path, body=None, params=None, timeout=45):
    if not path.startswith("/"):
        path = "/" + path
    url = base + path
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method.upper(), headers={
        "Authorization": f"Bearer {SUPERVISOR}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            ctype = (r.headers.get_content_type() or "").lower()
            data = r.read()
            # Anything that is neither JSON nor text (a tar, a JPEG) would come back
            # as mangled text. Write it to disk and return the path instead.
            if ctype and not (ctype.startswith("text/") or "json" in ctype):
                return _to_share(data, r.headers, path, ctype, r.status)
            return {"status": r.status, "body": _maybe_json(data.decode("utf-8", "replace"))}
    except urllib.error.HTTPError as e:
        return {"status": e.code, "error": e.reason, "body": _maybe_json(e.read().decode("utf-8", "replace"))}
    except Exception as e:
        return {"status": 0, "error": f"{type(e).__name__}: {e}"}


def _to_share(data, headers, path, ctype, status):
    os.makedirs(SHARE, exist_ok=True)
    name = ""
    disposition = headers.get("Content-Disposition", "")
    m = re.search(r'filename="?([^";]+)"?', disposition)
    if m:
        name = os.path.basename(m.group(1))
    if not name:
        name = os.path.basename(path.rstrip("/")) or "download"
    target = os.path.join(SHARE, name)
    with open(target, "wb") as f:
        f.write(data)
    log(f"binary response ({ctype}, {len(data)} bytes) written to {target}")
    return {"status": status, "binary": True, "content_type": ctype,
            "bytes": len(data), "file": target,
            "note": "Binary data does not fit in an MCP response. The file was written to the "
                    "`share` folder, subdirectory ha-mcp."}


def _maybe_json(raw):
    try:
        return json.loads(raw)
    except Exception:
        return raw


def core(method, path, body=None, params=None):
    return _http(CORE_REST, method, path.replace("/api/", "/", 1) if path.startswith("/api/") else path,
                 body, params)


def sup(method, path, body=None):
    return _http(SUP_REST, method, path, body)


async def _ws_run(cmd):
    import websockets
    async with websockets.connect(CORE_WS, max_size=60_000_000, open_timeout=20) as ws:
        await ws.recv()
        await ws.send(json.dumps({"type": "auth", "access_token": SUPERVISOR}))
        auth = json.loads(await ws.recv())
        if auth.get("type") != "auth_ok":
            return {"error": "authentication on the core websocket was refused", "detail": auth}
        msg = dict(cmd)
        msg["id"] = 1
        await ws.send(json.dumps(msg))
        while True:
            r = json.loads(await asyncio.wait_for(ws.recv(), timeout=45))
            if r.get("id") == 1 and r.get("type") == "result":
                return r.get("result") if r.get("success") else {"error": r.get("error")}


def ws_cmd(cmd_type, params=None):
    cmd = {"type": cmd_type}
    if params:
        cmd.update(params)
    try:
        return asyncio.run(_ws_run(cmd))
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def ago(hours):
    """Timestamp `hours` in the past, in the time zone Home Assistant runs in.

    History and logbook questions are asked in relative terms ("the last 24 hours").
    Computing that in UTC on a machine that is not on UTC shifts every answer.
    """
    return (datetime.now(TZ) - timedelta(hours=float(hours))).strftime("%Y-%m-%dT%H:%M:%S")


# ------------------------------------------------------------------------ tools
def t_rest(a):
    return core(a.get("method", "GET"), a["path"], json.loads(a["body_json"]) if a.get("body_json") else None)


def t_ws(a):
    return ws_cmd(a["command"], json.loads(a["params_json"]) if a.get("params_json") else None)


def t_states(a):
    if a.get("entity_id"):
        return core("GET", f"/states/{a['entity_id']}")
    res = core("GET", "/states")
    items = res.get("body")
    if not isinstance(items, list):
        return res
    domain, q = a.get("domain", ""), (a.get("search") or "").lower()
    out = [e for e in items if (not domain or e["entity_id"].startswith(domain + "."))
           and (not q or q in e["entity_id"].lower()
                or q in str(e.get("attributes", {}).get("friendly_name", "")).lower())]
    slim = [{"entity_id": e["entity_id"], "name": e.get("attributes", {}).get("friendly_name"),
             "state": e["state"]} for e in out]
    return {"count": len(slim), "entities": slim[:400], "truncated": len(slim) > 400}


def t_service(a):
    data = json.loads(a["data_json"]) if a.get("data_json") else {}
    if a.get("entity_id"):
        data["entity_id"] = [x.strip() for x in a["entity_id"].split(",")]
    path = f"/services/{a['domain']}/{a['service']}"
    if a.get("return_response"):
        path += "?return_response"
    return core("POST", path, data)


def t_template(a):
    return core("POST", "/template", {"template": a["template"]})


def t_history(a):
    p = {"minimal_response": "", "no_attributes": ""}
    if a.get("entity_id"):
        p["filter_entity_id"] = a["entity_id"]
    return core("GET", f"/history/period/{ago(a.get('hours', 24))}", params=p)


def t_logbook(a):
    p = {"entity": a["entity_id"]} if a.get("entity_id") else {}
    return core("GET", f"/logbook/{ago(a.get('hours', 24))}", params=p)


def t_errorlog(a):
    """Logs through the Supervisor, not through /api/error_log.

    That core endpoint was removed in Home Assistant 2026.9 and now returns 404. On a
    Supervisor installation the logs live behind /core/logs, /host/logs and
    /supervisor/logs instead.
    """
    endpoint = {"core": "/core/logs", "host": "/host/logs",
                "supervisor": "/supervisor/logs"}.get(a.get("source", "core"), "/core/logs")
    res = _http(SUP_REST, "GET", endpoint, timeout=90)
    body = res.get("body")
    if isinstance(body, str):
        # journald output arrives with ANSI colour codes; those make a log line
        # unreadable inside an MCP response and cost nothing but characters.
        lines = ANSI.sub("", body).splitlines()
        n = int(a.get("lines", 100))
        res["body"] = "\n".join(lines[-n:])
        res["total_lines"] = len(lines)
    return res


KINDS = {"automation": "automation", "script": "script", "scene": "scene",
         "input_boolean": "input_boolean", "input_number": "input_number",
         "input_select": "input_select", "input_text": "input_text", "template": "template"}


def t_cfg_get(a):
    k = KINDS.get(a["kind"], a["kind"])
    return core("GET", f"/config/{k}/config/{a['object_id']}" if a.get("object_id") else f"/config/{k}/config")


def t_cfg_save(a):
    k = KINDS.get(a["kind"], a["kind"])
    return core("POST", f"/config/{k}/config/{a['object_id']}", json.loads(a["config_json"]))


def t_cfg_del(a):
    k = KINDS.get(a["kind"], a["kind"])
    return core("DELETE", f"/config/{k}/config/{a['object_id']}")


def t_registry(a):
    return ws_cmd({"entities": "config/entity_registry/list", "devices": "config/device_registry/list",
                   "areas": "config/area_registry/list", "floors": "config/floor_registry/list",
                   "labels": "config/label_registry/list", "integrations": "config_entries/get"}[a["what"]])


def t_expose(a):
    return ws_cmd("homeassistant/expose_entity", {
        "entity_ids": [x.strip() for x in a["entity_ids"].split(",")],
        "assistants": ["conversation"], "should_expose": bool(a.get("expose", True))})


def t_addons(a):
    r = sup("GET", "/addons")
    lst = ((r.get("body") or {}).get("data") or {}).get("addons")
    if lst is None:
        return r
    return [{"slug": x["slug"], "name": x["name"], "state": x["state"], "version": x.get("version")} for x in lst]


def t_addon_action(a):
    action = a["action"]
    if action == "info":
        return sup("GET", f"/addons/{a['slug']}/info")
    if action == "logs":
        res = _http(SUP_REST, "GET", f"/addons/{a['slug']}/logs", timeout=90)
        if isinstance(res.get("body"), str):
            res["body"] = ANSI.sub("", res["body"])
        return res
    return sup("POST", f"/addons/{a['slug']}/{action}")


def t_download(a):
    """Fetch an endpoint and always land the result as a file in the share folder."""
    base = SUP_REST if a.get("target", "supervisor") == "supervisor" else CORE_REST
    r = _http(base, "GET", a["endpoint"], timeout=300)
    if isinstance(r, dict) and r.get("binary"):
        return r
    # Write a JSON or text response to a file as well, since that is what was asked.
    os.makedirs(SHARE, exist_ok=True)
    name = os.path.basename(a["endpoint"].rstrip("/")) or "download"
    target = os.path.join(SHARE, name + ".json")
    body = r.get("body")
    with open(target, "w") as f:
        f.write(body if isinstance(body, str) else json.dumps(body, ensure_ascii=False, indent=1))
    return {"status": r.get("status"), "file": target}


def t_upload(a):
    """Send a file from the share folder to an endpoint as multipart/form-data.

    This is what puts restoring a backup within reach: drop the .tar file into the
    share over Samba and point at it. A JSON body cannot carry that.
    """
    path = a["file"]
    if not os.path.isabs(path):
        path = os.path.join(SHARE, path)
    if not os.path.exists(path):
        return {"error": f"file not found: {path}"}
    field = a.get("field", "file")
    boundary = "----mcp" + secrets.token_hex(12)
    with open(path, "rb") as f:
        payload = f.read()
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{field}\"; "
            f"filename=\"{os.path.basename(path)}\"\r\n"
            f"Content-Type: application/octet-stream\r\n\r\n").encode() + payload + \
           f"\r\n--{boundary}--\r\n".encode()
    base = SUP_REST if a.get("target", "supervisor") == "supervisor" else CORE_REST
    endpoint = a["endpoint"]
    if not endpoint.startswith("/"):
        endpoint = "/" + endpoint
    req = urllib.request.Request(base + endpoint, data=body, method="POST", headers={
        "Authorization": f"Bearer {SUPERVISOR}",
        "Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            return {"status": r.status, "bytes_sent": len(payload),
                    "body": _maybe_json(r.read().decode("utf-8", "replace"))}
    except urllib.error.HTTPError as e:
        return {"status": e.code, "error": e.reason,
                "body": _maybe_json(e.read().decode("utf-8", "replace"))}
    except Exception as e:
        return {"status": 0, "error": f"{type(e).__name__}: {e}"}


def t_supervisor(a):
    return sup(a.get("method", "GET"), a["endpoint"],
               json.loads(a["body_json"]) if a.get("body_json") else None)


S = lambda d, ex=None: {"type": "string", "description": d + (f" Example: {ex}" if ex else "")}
B = lambda d: {"type": "boolean", "description": d}
N = lambda d: {"type": "number", "description": d}


def T(desc, props, req):
    return {"description": desc,
            "inputSchema": {"type": "object", "properties": props, "required": req}}


TOOLS = {
 "ha_rest": (T("Arbitrary call against the Home Assistant REST API. Every endpoint is reachable this way.",
   {"path": S("Path, with or without the /api prefix.", "/states"),
    "method": S("GET, POST or DELETE. Defaults to GET."),
    "body_json": S("Request body as JSON text.")}, ["path"]), t_rest),
 "ha_ws": (T("Arbitrary command against the WebSocket API. The web interface uses it for nearly everything, "
   "so this reaches what REST does not offer: registries, integrations, dashboards, users, backups.",
   {"command": S("Command type.", "config/area_registry/list"),
    "params_json": S("Parameters as JSON text.")}, ["command"]), t_ws),
 "ha_states": (T("Read entities: a single one with all of its attributes, or a filtered list.",
   {"entity_id": S("A single entity_id for full detail."), "domain": S("Filter by domain.", "sensor"),
    "search": S("Filter on text in the id or the friendly name.")}, []), t_states),
 "ha_service": (T("Call a Home Assistant service. Every service in every domain is available.",
   {"domain": S("Domain.", "light"), "service": S("Service.", "turn_on"),
    "entity_id": S("Target entity or entities, comma separated."),
    "data_json": S("Service data as JSON text."),
    "return_response": B("true for services that return data.")}, ["domain", "service"]), t_service),
 "ha_template": (T("Render a Jinja2 template inside Home Assistant. Powerful for computing or summarising "
   "across many entities at once.", {"template": S("The template.")}, ["template"]), t_template),
 "ha_history": (T("State history of entities over a period.",
   {"entity_id": S("Entity or entities, comma separated."),
    "hours": N("Hours to look back. Defaults to 24.")}, []), t_history),
 "ha_logbook": (T("Logbook: who or what changed something, and when.",
   {"entity_id": S("Restrict to a single entity."),
    "hours": N("Hours to look back. Defaults to 24.")}, []), t_logbook),
 "ha_error_log": (T("Diagnostic log, served through the Supervisor. Pick the source: 'core' for Home Assistant "
   "itself (the default), 'host' for the operating system, 'supervisor' for the Supervisor.",
   {"source": {"type": "string", "enum": ["core", "host", "supervisor"], "description": "Which log to read."},
    "lines": N("Number of trailing lines. Defaults to 100.")}, []), t_errorlog),
 "ha_config_get": (T("Read the configuration of an automation, script, scene or helper.",
   {"kind": S("automation, script, scene, input_boolean, input_number, input_select, input_text or template."),
    "object_id": S("Object id. Omit to list them all.")}, ["kind"]), t_cfg_get),
 "ha_config_save": (T("Create an automation, script, scene or helper, or overwrite an existing one. "
   "CAUTION: this replaces the ENTIRE configuration; read it with ha_config_get first.",
   {"kind": S("Kind of object."), "object_id": S("Object id. A new unique id creates a new object."),
    "config_json": S("The complete configuration as JSON text.")},
   ["kind", "object_id", "config_json"]), t_cfg_save),
 "ha_config_delete": (T("Delete an automation, script, scene or helper.",
   {"kind": S("Kind of object."), "object_id": S("Object id.")}, ["kind", "object_id"]), t_cfg_del),
 "ha_registry": (T("List a registry: entities, devices, areas, floors, labels or integrations.",
   {"what": {"type": "string", "enum": ["entities", "devices", "areas", "floors", "labels", "integrations"],
             "description": "Which registry to list."}}, ["what"]), t_registry),
 "ha_expose": (T("Expose entities to Assist, or hide them again. This is what decides what the voice "
   "assistant can see.",
   {"entity_ids": S("Comma separated."), "expose": B("true = expose, false = hide.")},
   ["entity_ids"]), t_expose),
 "ha_addons": (T("List every installed add-on with its state.", {}, []), t_addons),
 "ha_addon_action": (T("Manage an add-on: read its info, read its logs, start, stop, restart or update it.",
   {"slug": S("Add-on slug.", "a0d7b954_nodered"),
    "action": {"type": "string", "enum": ["info", "logs", "start", "stop", "restart", "update"],
               "description": "What to do."}}, ["slug", "action"]), t_addon_action),
 "ha_download": (T("Fetch an endpoint and write the result as a FILE into the share folder, subdirectory "
   "ha-mcp. Use this for binary data: downloading a backup, a camera snapshot, a log file.",
   {"endpoint": S("Path.", "/backups/abc12345/download"),
    "target": S("'supervisor' (default) or 'core' for the Home Assistant API.")}, ["endpoint"]), t_download),
 "ha_upload": (T("Send a file from the share folder to an endpoint as a multipart upload. Required for "
   "anything that takes a file rather than JSON, such as restoring a backup.",
   {"file": S("File name inside the share subdirectory ha-mcp, or an absolute path.", "backup.tar"),
    "endpoint": S("Path.", "/backups/new/upload"),
    "field": S("Name of the form field. Defaults to 'file'."),
    "target": S("'supervisor' (default) or 'core'.")}, ["file", "endpoint"]), t_upload),
 "ha_supervisor": (T("Arbitrary call against the Supervisor API: host, OS, backups, network, add-on store.",
   {"endpoint": S("Path.", "/backups"), "method": S("GET or POST. Defaults to GET."),
    "body_json": S("Request body as JSON text.")}, ["endpoint"]), t_supervisor),
}

# ---------------------------------------------------------------------- JSON-RPC
def handle_rpc(req):
    """Return a response dict, or None for a notification."""
    method, rid = req.get("method"), req.get("id")
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": req.get("params", {}).get("protocolVersion", "2024-11-05"),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "home-assistant-api", "version": VERSION}}}
    if method in ("notifications/initialized", "notifications/cancelled"):
        return None
    if method == "ping":
        return {"jsonrpc": "2.0", "id": rid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {
            "tools": [{"name": n, **spec} for n, (spec, _) in TOOLS.items()]}}
    if method == "tools/call":
        p = req.get("params", {})
        name = p.get("name")
        args = p.get("arguments") or {}
        if name not in TOOLS:
            return {"jsonrpc": "2.0", "id": rid,
                    "error": {"code": -32601, "message": f"unknown tool: {name}"}}
        log(f"tool {name} {json.dumps(args, ensure_ascii=False)[:160]}", "debug")
        try:
            out = TOOLS[name][1](args)
            txt = out if isinstance(out, str) else json.dumps(out, ensure_ascii=False, default=str)
            err = isinstance(out, dict) and bool(out.get("error"))
        except Exception as e:
            log(f"tool {name} failed: {type(e).__name__}: {e}", "error")
            txt, err = f"{type(e).__name__}: {e}", True
        return {"jsonrpc": "2.0", "id": rid,
                "result": {"content": [{"type": "text", "text": clip(txt, name)}], "isError": err}}
    if rid is None:
        return None
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"unsupported: {method}"}}


def dispatch(req):
    """Route one request, or a JSON-RPC batch, to handle_rpc.

    A client is free to send an array of calls, and anything that is neither an
    array nor an object is simply malformed. Both used to reach handle_rpc as-is
    and take the connection down with an AttributeError instead of producing the
    error response the caller is entitled to.
    """
    if isinstance(req, list):
        out = [r for r in (dispatch(x) for x in req) if r is not None]
        return out or None
    if not isinstance(req, dict):
        return {"jsonrpc": "2.0", "id": None,
                "error": {"code": -32600, "message": "invalid request: expected a JSON object"}}
    return handle_rpc(req)


# -------------------------------------------------------------------------- HTTP
SESSIONS = {}
SESSIONS_LOCK = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        log(f"{self.address_string()} {fmt % args}", "debug")

    def _auth_ok(self):
        got = self.headers.get("Authorization", "")
        if not got.startswith("Bearer "):
            return False
        # compare_digest on str requires both sides to be ASCII; comparing bytes
        # keeps a token with an accent in it from raising instead of returning 401.
        return secrets.compare_digest(got[7:].strip().encode(), TOKEN.encode())

    def _send(self, code, body=b"", ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _deny(self):
        self._send(401, json.dumps({"error": "missing or invalid bearer token"}).encode())

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/health":
            return self._send(200, json.dumps({"status": "ok", "version": VERSION, "tools": len(TOOLS)}).encode())
        if not self._auth_ok():
            return self._deny()
        if path == "/sse":
            return self._sse()
        self._send(404, b'{"error":"unknown path"}')

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
        except ValueError:
            return self._send(400, b'{"error":"invalid Content-Length"}')
        if length > MAX_BODY:
            return self._send(413, b'{"error":"request body too large"}')
        # Read the body before answering, even on a rejected request: leaving it in
        # the socket desynchronises the next request on a keep-alive connection.
        raw = self.rfile.read(length) if length else b""
        if not self._auth_ok():
            return self._deny()
        try:
            req = json.loads(raw or b"{}")
        except Exception:
            return self._send(400, b'{"error":"invalid JSON"}')

        if parsed.path == "/mcp":
            # Stateless: the answer goes straight back in the same request.
            resp = dispatch(req)
            if resp is None:
                return self._send(202, b"")
            return self._send(200, json.dumps(resp).encode())

        if parsed.path.startswith("/messages"):
            sid = urllib.parse.parse_qs(parsed.query).get("session_id", [""])[0]
            with SESSIONS_LOCK:
                q = SESSIONS.get(sid)
            if q is None:
                return self._send(404, b'{"error":"unknown session"}')
            resp = dispatch(req)
            if resp is not None:
                q.put(resp)
            return self._send(202, b"")

        self._send(404, b'{"error":"unknown path"}')

    def _sse(self):
        sid = secrets.token_hex(16)
        q = queue.Queue()
        with SESSIONS_LOCK:
            SESSIONS[sid] = q
        log(f"SSE session opened: {sid}", "debug")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            self.wfile.write(f"event: endpoint\ndata: /messages?session_id={sid}\n\n".encode())
            self.wfile.flush()
            while True:
                try:
                    msg = q.get(timeout=15)
                    self.wfile.write(f"event: message\ndata: {json.dumps(msg)}\n\n".encode())
                except queue.Empty:
                    # Keepalive as a comment line, or a router closes the connection.
                    self.wfile.write(b": ping\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            log(f"SSE session closed: {sid}", "debug")
        finally:
            with SESSIONS_LOCK:
                SESSIONS.pop(sid, None)


class Server(ThreadingHTTPServer):
    """ThreadingHTTPServer that does not shout about a client hanging up.

    A disconnecting client is ordinary: an SSE stream closed, a bridge restarted, a
    watchdog probe timing out. The default handler prints a full traceback for each
    one, which in an add-on log reads like a crash and invites bug reports about
    something that is working exactly as intended.
    """

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError, TimeoutError)):
            return log(f"client {client_address[0]} disconnected: {type(exc).__name__}", "debug")
        super().handle_error(request, client_address)


def resolve_timezone():
    """Take the time zone from the add-on options, else from Home Assistant itself.

    Relative questions ("the last 24 hours") are turned into absolute timestamps
    here. Computing those in UTC on an installation that is not on UTC silently
    shifts every history and logbook answer, so it is worth one API call.
    """
    name = (OPTIONS.get("timezone") or "").strip()
    source = "the add-on configuration"
    if not name:
        cfg = core("GET", "/config").get("body")
        if isinstance(cfg, dict):
            name = cfg.get("time_zone") or ""
            source = "Home Assistant"
    try:
        return ZoneInfo(name), f"{name} (from {source})"
    except Exception:
        return timezone.utc, "UTC (no usable time zone found)"


def main():
    global TZ
    if not TOKEN:
        log("NO TOKEN CONFIGURED. Set 'token' in the add-on configuration; without one this port would "
            "hand full administrative access to Home Assistant to anyone on the network.", "error")
        sys.exit(1)
    if not SUPERVISOR:
        log("No SUPERVISOR_TOKEN in the environment -- is this really running as an add-on?", "error")
        sys.exit(1)
    ping = core("GET", "/")
    log(f"connection to Home Assistant: HTTP {ping.get('status')} {ping.get('body')}")
    TZ, tz_label = resolve_timezone()
    log(f"time zone: {tz_label}")
    log(f"{len(TOOLS)} tools available on port {PORT} (/mcp, /sse, /health)")
    srv = Server(("0.0.0.0", PORT), Handler)

    def stop(signum, _frame):
        # Without this the Supervisor waits ten seconds and then kills the container,
        # and open SSE sessions never get a clean shutdown.
        log(f"signal {signum} received, shutting down")
        threading.Thread(target=srv.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    srv.serve_forever()
    srv.server_close()
    log("stopped")


if __name__ == "__main__":
    main()
