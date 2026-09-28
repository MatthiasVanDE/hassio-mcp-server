#!/usr/bin/env python3
"""MCP server exposing the Home Assistant APIs, running as a Supervisor add-on.

Two transports, because MCP clients differ in what they support:
  POST /mcp     streamable HTTP, stateless -- one request, one response
  GET  /sse     the older SSE transport, paired with POST /messages?session_id=...
  GET  /health  unauthenticated liveness probe, used by the Docker HEALTHCHECK

A second, separate server runs on INGRESS_PORT: the add-on's own page, reached
through Supervisor ingress ("OPEN WEB UI"). It serves no MCP, only the setup
information a new user would otherwise have to assemble from the documentation.

Access to Home Assistant does NOT go through a long-lived access token but through
the SUPERVISOR_TOKEN that the Supervisor injects into the container. That token is
rotated by the Supervisor and never appears in a configuration file.
"""
import asyncio, base64, difflib, fnmatch, html, json, os, queue, secrets, sys, threading
import re, signal, urllib.error, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

VERSION = "2.2.0"
SUPERVISOR = os.environ.get("SUPERVISOR_TOKEN", "")
CORE_REST = "http://supervisor/core/api"
CORE_WS = "ws://supervisor/core/websocket"
SUP_REST = "http://supervisor"
PORT = 8099
# Ingress is deliberately on its own port: PORT demands a bearer token that no browser
# has, and the ingress port must not demand one, because the Supervisor has already
# authenticated the user before it proxies anything here.
INGRESS_PORT = 8098
MAX_CHARS = 100_000
MAX_BODY = 16 * 1024 * 1024
# Binary data does not fit in an MCP response (which is text). Such payloads are
# written here, into the `share` folder, and the tool returns the path instead.
SHARE = "/share/ha-mcp"
# Home Assistant's configuration directory, mounted by `map: homeassistant_config`.
# Not /config: under the current Supervisor mount scheme that path is the add-on's
# own addon_config folder.
CONFIG_DIR = "/homeassistant"
# The previous version of everything a tool overwrites or deletes.
BACKUP_DIR = os.path.join(SHARE, "backups")
BACKUP_KEEP = 300


def opts():
    try:
        return json.load(open("/data/options.json"))
    except Exception:
        return {}


OPTIONS = opts()
# Outside options.json on purpose: a generated token must survive an update, and must
# not end up in a configuration snapshot that gets pasted into an issue.
TOKEN_FILE = "/data/token"
# Both are filled in by main(); see resolve_token().
TOKEN = ""
TOKEN_SOURCE = ""
# Optional second token that may only read; see readonly_refusal().
READONLY_TOKEN = (OPTIONS.get("readonly_token") or "").strip()
LEVELS = {"debug": 10, "info": 20, "warning": 30, "error": 40}
LEVEL = LEVELS.get(OPTIONS.get("log_level", "info"), 20)
# Replaced in main() by the time zone reported by Home Assistant. Until then UTC,
# so that a log line written during start-up still carries a valid timestamp.
TZ = timezone.utc
TZ_LABEL = "UTC"
# The address the add-on page tells clients to connect to; resolved once at start.
HOST = "homeassistant.local"


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
def _http(base, method, path, body=None, params=None, timeout=45, headers=None, raw=False):
    """One call against the core or the Supervisor. `raw` hands back the bytes as they
    came, for the few callers (a camera image) that want them rather than a file."""
    if not path.startswith("/"):
        path = "/" + path
    url = base + path
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method.upper(), headers={
        "Authorization": f"Bearer {SUPERVISOR}", "Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            ctype = (r.headers.get_content_type() or "").lower()
            data = r.read()
            if raw:
                return {"status": r.status, "content_type": ctype, "data": data}
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


# Calls after which Home Assistant only comes back if its YAML configuration is valid.
RESTARTS = {("homeassistant", "restart"), ("hassio", "host_reboot"), ("homeassistant", "stop")}


def restart_blocked():
    """Why a restart must not happen now, or None.

    A restart with a broken configuration.yaml leaves Home Assistant in safe mode
    or not running at all, and then this add-on can no longer reach it to repair
    the file. The UI checks the configuration before it restarts; so does this.
    """
    res = core("POST", "/config/core/check_config")
    body = res.get("body") if isinstance(res.get("body"), dict) else {}
    if res.get("status") == 200 and body.get("result") == "valid":
        return None
    return {"error": "restart refused: the configuration check did not pass. Fix the errors, or "
                     "pass skip_config_check=true if you are certain.",
            "check": body or res}


def t_service(a):
    data = json.loads(a["data_json"]) if a.get("data_json") else {}
    if a.get("entity_id"):
        data["entity_id"] = [x.strip() for x in a["entity_id"].split(",")]
    if (a["domain"], a["service"]) in RESTARTS and not a.get("skip_config_check"):
        blocked = restart_blocked()
        if blocked:
            return blocked
    path = f"/services/{a['domain']}/{a['service']}"
    if a.get("return_response"):
        path += "?return_response"
    return core("POST", path, data)


def t_template(a):
    return core("POST", "/template", {"template": a["template"]})


def _when(value, default_hours):
    """An absolute ISO timestamp, a relative '6h' / '3d' / '2w', or a number of hours ago."""
    if value in (None, ""):
        return datetime.now(TZ) - timedelta(hours=default_hours)
    text = str(value).strip()
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([hdw]?)", text)
    if m:
        factor = {"": 1, "h": 1, "d": 24, "w": 168}[m.group(2)]
        return datetime.now(TZ) - timedelta(hours=float(m.group(1)) * factor)
    dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=TZ)


def _local(ts):
    """Epoch seconds (or milliseconds) as local time, short enough to repeat a thousand times."""
    if ts is None:
        return None
    ts = float(ts)
    if ts > 1e11:
        ts /= 1000
    return datetime.fromtimestamp(ts, TZ).strftime("%Y-%m-%d %H:%M:%S")


def t_history(a):
    """State history as one compact [time, state] pair per change.

    The REST shape repeats the entity id and two ISO timestamps on every row, which
    is what used to push a day of a chatty sensor past the response limit. This is
    the same data at roughly a quarter of the size, in Home Assistant's time zone.
    """
    ids = [x.strip() for x in (a.get("entity_id") or "").split(",") if x.strip()]
    start = _when(a.get("start") or a.get("hours"), 24)
    end = _when(a["end"], 0) if a.get("end") else datetime.now(TZ)
    if not ids:
        # Everything, as the REST API gives it: without entity ids the websocket
        # command refuses, and a whole-house history is rarely what is meant.
        p = {"minimal_response": "", "no_attributes": "", "end_time": end.isoformat()}
        return core("GET", f"/history/period/{start.isoformat()}", params=p)
    res = ws_cmd("history/history_during_period", {
        "start_time": start.isoformat(), "end_time": end.isoformat(), "entity_ids": ids,
        "minimal_response": True, "no_attributes": not a.get("attributes"),
        "significant_changes_only": not a.get("all_changes")})
    if not isinstance(res, dict) or res.get("error"):
        return res
    out = {"start": start.strftime("%Y-%m-%d %H:%M:%S"), "end": end.strftime("%Y-%m-%d %H:%M:%S"),
           "time_zone": str(TZ), "entities": {}}
    for eid in ids:
        rows = res.get(eid) or []
        changes = []
        for r in rows:
            row = [_local(r.get("lc") or r.get("lu")), r.get("s")]
            if a.get("attributes") and r.get("a"):
                row.append(r["a"])
            changes.append(row)
        out["entities"][eid] = {"changes": len(changes), "history": changes}
    return out


def t_statistics(a):
    """Long-term statistics: hourly or daily mean/min/max, or sum/change for meters.

    Kept by the recorder for years, where state history is purged after ten days by
    default -- this is how "what did the heat pump use last month" gets answered.
    """
    ids = [x.strip() for x in a["statistic_ids"].split(",") if x.strip()]
    start = _when(a.get("start"), 24 * 30)
    end = _when(a["end"], 0) if a.get("end") else datetime.now(TZ)
    period = a.get("period") or "day"
    params = {"start_time": start.isoformat(), "end_time": end.isoformat(),
              "statistic_ids": ids, "period": period}
    if a.get("types"):
        params["types"] = [x.strip() for x in a["types"].split(",")]
    res = ws_cmd("recorder/statistics_during_period", params)
    if not isinstance(res, dict) or res.get("error"):
        return res
    out = {"period": period, "start": start.strftime("%Y-%m-%d %H:%M"), "end": end.strftime("%Y-%m-%d %H:%M"),
           "statistics": {}}
    for sid in ids:
        rows = res.get(sid) or []
        out["statistics"][sid] = [dict({k: (round(v, 4) if isinstance(v, float) else v)
                                        for k, v in r.items() if k not in ("start", "end", "last_reset")
                                        and v is not None}, start=_local(r.get("start")))
                                  for r in rows]
        if not rows:
            out.setdefault("missing", []).append(sid)
    if out.get("missing"):
        out["note"] = ("No statistics for some ids. Only entities with a state_class are kept long-term; "
                       "external statistics use an id with a colon, such as sensor:energy.")
    return out


def t_logbook(a):
    p = {"entity": a["entity_id"]} if a.get("entity_id") else {}
    start = _when(a.get("start") or a.get("hours"), 24)
    if a.get("end"):
        p["end_time"] = _when(a["end"], 0).isoformat()
    return core("GET", f"/logbook/{start.isoformat()}", params=p)


LOG_LEVEL = re.compile(r"(?:^|\s)(DEBUG|INFO|WARNING|ERROR|CRITICAL)(?:\s|:|\])")
LOG_RANK = {"DEBUG": 0, "INFO": 1, "WARNING": 2, "ERROR": 3, "CRITICAL": 4}


def supervisor_log(endpoint, lines, timeout=60):
    """The last `lines` entries of a journald log behind the Supervisor.

    Without a Range header the Supervisor answers with exactly 100 lines, whatever
    was asked for -- which is why this tool used to stop at 100. With it, the
    journal is cut on the Supervisor's side, so a thousand lines cost no more than a
    hundred.
    """
    res = _http(SUP_REST, "GET", endpoint, timeout=timeout,
                headers={"Accept": "text/plain", "Range": f"entries=:-{lines - 1}:{lines}"})
    if isinstance(res.get("body"), str):
        res["body"] = ANSI.sub("", res["body"])
    return res


def t_errorlog(a):
    """Logs, as a raw tail or as Home Assistant's own de-duplicated error list.

    The raw logs come through the Supervisor (/core/logs and friends) because the
    core /api/error_log endpoint was removed in Home Assistant 2026.9. 'errors' is
    system_log/list: every distinct warning and error since the last start, counted,
    with its traceback -- usually the better place to begin.
    """
    source = a.get("source") or "core"
    lines = min(int(a.get("lines") or 100), 5000)
    search = (a.get("search") or "").strip()
    level = (a.get("level") or "").upper()
    if source == "errors":
        items = ws_cmd("system_log/list")
        if not isinstance(items, list):
            return items
        rx = re.compile(re.escape(search), re.I) if search else None
        out = []
        for e in sorted(items, key=lambda e: e.get("timestamp") or 0, reverse=True):
            if level and LOG_RANK.get(e.get("level"), 0) < LOG_RANK.get(level, 0):
                continue
            text = " ".join(e.get("message") or []) + " " + (e.get("name") or "")
            if rx and not rx.search(text + " " + (e.get("exception") or "")):
                continue
            item = {"level": e.get("level"), "logger": e.get("name"), "count": e.get("count"),
                    "first": _local(e.get("first_occurred")), "last": _local(e.get("timestamp")),
                    "source": ":".join(str(x) for x in (e.get("source") or [])),
                    "message": e.get("message")}
            if e.get("exception"):
                item["exception"] = e["exception"][-3000:]
            out.append(item)
        return {"distinct": len(out), "entries": out[:lines]}
    endpoint = {"core": "/core/logs", "host": "/host/logs", "supervisor": "/supervisor/logs"}.get(source)
    if not endpoint:
        return {"error": f"unknown source: {source}"}
    # A filter looks further back, or it would only ever search the last page.
    window = max(lines, 3000) if (search or level) else lines
    res = supervisor_log(endpoint, window)
    body = res.get("body")
    if isinstance(body, str):
        all_lines = body.splitlines()
        picked = all_lines
        if level:
            floor = LOG_RANK.get(level, 0)
            picked = [ln for ln in picked if (m := LOG_LEVEL.search(ln)) and LOG_RANK[m.group(1)] >= floor]
        if search:
            rx = re.compile(re.escape(search), re.I)
            picked = [ln for ln in picked if rx.search(ln)]
        res["body"] = "\n".join(picked[-lines:])
        res["lines_read"] = len(all_lines)
        if search or level:
            res["matches"] = len(picked)
    return res


KINDS = {"automation": "automation", "script": "script", "scene": "scene",
         "input_boolean": "input_boolean", "input_number": "input_number",
         "input_select": "input_select", "input_text": "input_text", "template": "template"}


def t_cfg_get(a):
    k = KINDS.get(a["kind"], a["kind"])
    return core("GET", f"/config/{k}/config/{a['object_id']}" if a.get("object_id") else f"/config/{k}/config")


def _cfg_backup(k, oid):
    """Back up the current version of an automation, script, scene or helper.

    Returns (path or None, error or None). A 404 simply means there is nothing yet
    to back up; any other failure to read it blocks the write, because then the
    old version could not be restored.
    """
    cur = core("GET", f"/config/{k}/config/{oid}")
    if cur.get("status") == 200:
        return backup(f"{k}_{oid}.json", json.dumps(cur.get("body"), ensure_ascii=False, indent=2)), None
    if cur.get("status") in (400, 404):
        return None, None
    return None, {"error": "not saved: the current version could not be read to back it up",
                  "detail": cur}


def t_cfg_save(a):
    k = KINDS.get(a["kind"], a["kind"])
    cfg = json.loads(a["config_json"])
    saved, err = _cfg_backup(k, a["object_id"])
    if err:
        return err
    res = core("POST", f"/config/{k}/config/{a['object_id']}", cfg)
    if saved:
        res["backup"] = saved
    if k in ("automation", "script") and isinstance(cfg, dict) and res.get("status") == 200:
        notes = review(cfg, k)
        if notes:
            res["review"] = notes
    return res


def t_cfg_del(a):
    k = KINDS.get(a["kind"], a["kind"])
    saved, err = _cfg_backup(k, a["object_id"])
    if err:
        return err
    res = core("DELETE", f"/config/{k}/config/{a['object_id']}")
    if saved:
        res["backup"] = saved
    return res


# ------------------------------------------------------------ reviewing a config
def _walk_refs(node, path, found):
    """Every entity_id and service/action named anywhere in an automation or script."""
    if isinstance(node, dict):
        for key, value in node.items():
            here = f"{path}.{key}" if path else str(key)
            if key in ("service", "action") and isinstance(value, str):
                found.append(("service", here, value))
            elif key == "entity_id" and isinstance(value, (str, list)):
                for i, v in enumerate(value if isinstance(value, list) else [value]):
                    if isinstance(v, str):
                        for part in v.split(","):
                            found.append(("entity", f"{here}[{i}]" if isinstance(value, list) else here,
                                          part.strip()))
            else:
                _walk_refs(value, here, found)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            _walk_refs(v, f"{path}[{i}]", found)


# Advice, never a refusal: each of these is legal Home Assistant, but a native
# construct is clearer, cheaper to evaluate, and shows up properly in a trace.
TEMPLATE_ADVICE = [
    (re.compile(r"now\(\)\s*-\s*[^}]*last_(changed|updated)|last_(changed|updated)[^}]*-\s*now\(\)"),
     "time since a state change is computed from last_changed in a template; a `for:` on the trigger or "
     "condition says the same natively (neither survives a restart; a helper that stores the time does)"),
    (re.compile(r"\|\s*(float|int)\b[^}]*[<>]|(float|int)\([^)]*\)\s*[<>]"),
     "a numeric comparison in a template; a numeric_state condition or trigger does this natively"),
    (re.compile(r"is_state\(\s*['\"]sun\.sun"), "sun.sun is tested in a template; use a sun condition"),
    (re.compile(r"now\(\)\.(hour|minute)"), "the time of day is tested in a template; use a time condition"),
    (re.compile(r"now\(\)\.(weekday|isoweekday)\(|strftime\(\s*['\"]%[aAw]"),
     "the weekday is tested in a template; a time condition takes weekday:"),
    (re.compile(r"\bstates\.[a-z_]+\.[a-z0-9_]+\.state\b"),
     "states.x.y.state raises when the entity is missing; states('x.y') does not"),
]


def _conditions(node):
    """Template strings used as conditions or template triggers, wherever they are nested."""
    out = []
    if isinstance(node, str):
        out.append(node)
    elif isinstance(node, list):
        for x in node:
            out += _conditions(x)
    elif isinstance(node, dict):
        if node.get("condition") == "template" or "value_template" in node:
            out.append(str(node.get("value_template", "")))
        for key in ("conditions", "condition"):
            if isinstance(node.get(key), (list, dict)):
                out += _conditions(node[key])
    return out


def _actions(node, out):
    if isinstance(node, list):
        for x in node:
            _actions(x, out)
    elif isinstance(node, dict):
        out.append(node)
        for key in ("sequence", "then", "else", "default", "parallel", "actions"):
            _actions(node.get(key), out)
        for key in ("choose", "if"):
            branch = node.get(key)
            if isinstance(branch, list):
                for b in branch:
                    if isinstance(b, dict):
                        out.append({"__conditions__": b.get("conditions")})
                        _actions(b.get("sequence"), out)
        if isinstance(node.get("repeat"), dict):
            _actions(node["repeat"].get("sequence"), out)


def review(cfg, kind):
    """What is probably wrong with an automation or script that was just saved.

    Home Assistant accepts a configuration that names an entity that does not exist,
    or a service that is not there, and only fails when it runs. This catches that
    at save time, and adds advice where a template does what a native condition
    would do better. Blueprints are skipped: their references are inputs.
    """
    if "use_blueprint" in cfg:
        return {}
    notes = {"missing": [], "advice": []}
    states = core("GET", "/states").get("body")
    services = core("GET", "/services").get("body")
    known = {e["entity_id"] for e in states} if isinstance(states, list) else None
    svc = {f"{d['domain']}.{name}" for d in services for name in d.get("services", {})} \
        if isinstance(services, list) else None
    refs = []
    _walk_refs(cfg, "", refs)
    for what, path, value in refs:
        if "{{" in value or "{%" in value or value in ("all", "none", ""):
            continue
        if what == "entity" and known is not None and value not in known:
            notes["missing"].append(f"{path}: entity {value} does not exist")
        if what == "service" and svc is not None and "." in value and value not in svc:
            notes["missing"].append(f"{path}: service {value} does not exist")
    conds = _conditions(cfg.get("conditions") or cfg.get("condition"))
    acts = []
    _actions(cfg.get("actions") or cfg.get("action") or cfg.get("sequence"), acts)
    for act in acts:
        if act.get("condition") or act.get("__conditions__"):
            conds += _conditions(act.get("__conditions__") or act)
    for t in cfg.get("triggers") or cfg.get("trigger") or []:
        if isinstance(t, dict):
            if (t.get("trigger") or t.get("platform")) == "template":
                conds.append(str(t.get("value_template", "")))
            if (t.get("trigger") or t.get("platform")) == "device":
                notes["advice"].append("a device trigger breaks when the device is replaced; "
                                       "an entity state trigger does not")
    for text in conds:
        for rx, advice in TEMPLATE_ADVICE:
            if rx.search(text) and advice not in notes["advice"]:
                notes["advice"].append(advice)
    for act in acts:
        if "wait_template" in act and "wait_template" not in " ".join(notes["advice"]):
            notes["advice"].append("wait_template polls a template; wait_for_trigger reacts to the event itself")
        if "service_template" in act:
            notes["advice"].append("service_template is deprecated; use action: with a choose:")
    if kind == "automation" and cfg.get("mode", "single") == "single" and \
            any("delay" in x or "wait_for_trigger" in x or "wait_template" in x for x in acts) and \
            re.search(r"binary_sensor\.\w*(motion|beweging|pir)", json.dumps(cfg.get("triggers") or
                                                                          cfg.get("trigger") or ""), re.I):
        notes["advice"].append("a motion automation with a delay in mode single ignores new motion while it "
                               "waits; mode: restart extends the timer instead")
    return {k: v for k, v in notes.items() if v}


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
        return supervisor_log(f"/addons/{a['slug']}/logs", min(int(a.get("lines") or 200), 5000))
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
    method = (a.get("method") or "GET").upper()
    endpoint = "/" + a["endpoint"].strip().lstrip("/")
    if method == "POST" and endpoint.rstrip("/") in ("/core/restart", "/core/rebuild", "/host/reboot") \
            and not a.get("skip_config_check"):
        blocked = restart_blocked()
        if blocked:
            return blocked
    return sup(method, endpoint, json.loads(a["body_json"]) if a.get("body_json") else None)


# ------------------------------------------------------------------------ search
def _tokens(text):
    return [t for t in re.split(r"[\s._\-/:,()]+", (text or "").lower()) if t]


def _score(query, fields):
    """How well a query matches an entity, 0-100. Deliberately simple and explainable.

    Exact id beats exact name beats every word found beats words found as prefixes;
    a typo still scores when the word is close enough. That is what "find the pool
    heat pump" needs, without an index to build or keep up to date.
    """
    q = query.lower().strip()
    eid, name = fields[0].lower(), (fields[1] or "").lower()
    if q in (eid, eid.split(".", 1)[-1]):
        return 100
    if q == name:
        return 95
    hay = " ".join(f.lower() for f in fields if f)
    squashed = hay.replace("_", "").replace(" ", "")
    words = _tokens(q)
    if not words:
        return 0
    if q in hay or q.replace(" ", "") in squashed:
        return 90
    hay_tokens = set(_tokens(hay))
    got = 0.0
    for w in words:
        if w in hay_tokens:
            got += 1
        elif any(t.startswith(w) for t in hay_tokens) or w in squashed:
            got += 0.8
        else:
            best = max((difflib.SequenceMatcher(None, w, t).ratio() for t in hay_tokens), default=0)
            if best >= 0.8:
                got += 0.7
    return int(80 * got / len(words))


def t_search(a):
    """Find entities by name, id, area or device, and everything that uses one.

    Two questions that otherwise take five calls: "which entity is the pool heat
    pump" and "what breaks if I rename this sensor". The second is answered by Home
    Assistant's own search/related, which also finds automations and scripts written
    in YAML -- those have no config API, so a text search would miss them.
    """
    query = (a.get("query") or "").strip()
    domain = (a.get("domain") or "").strip()
    area_q = (a.get("area") or "").strip().lower()
    limit = int(a.get("limit") or 25)
    states = core("GET", "/states").get("body")
    if not isinstance(states, list):
        return {"error": "could not read states"}
    ents = ws_cmd("config/entity_registry/list")
    devs = ws_cmd("config/device_registry/list")
    areas = ws_cmd("config/area_registry/list")
    ents = {e["entity_id"]: e for e in ents} if isinstance(ents, list) else {}
    devs = {d["id"]: d for d in devs} if isinstance(devs, list) else {}
    area_name = {x["area_id"]: x["name"] for x in areas} if isinstance(areas, list) else {}
    hits = []
    for st in states:
        eid = st["entity_id"]
        if domain and not eid.startswith(domain + "."):
            continue
        reg = ents.get(eid, {})
        dev = devs.get(reg.get("device_id") or "", {})
        area_id = reg.get("area_id") or dev.get("area_id")
        area = area_name.get(area_id, "")
        if area_q and area_q not in area.lower() and area_q != (area_id or ""):
            continue
        name = st.get("attributes", {}).get("friendly_name") or ""
        fields = [eid, name, area, dev.get("name_by_user") or dev.get("name") or "",
                  " ".join(reg.get("aliases") or []), reg.get("original_name") or ""]
        score = _score(query, fields) if query else 100
        if score >= 50:
            hits.append((score, eid, {"entity_id": eid, "name": name, "state": st.get("state"),
                                      "area": area or None, "device": fields[3] or None, "score": score}))
    hits.sort(key=lambda h: (-h[0], h[1]))
    out = {"matches": len(hits), "entities": [{k: v for k, v in h[2].items() if v is not None}
                                              for h in hits[:limit]]}
    target = query if query in ents or any(st["entity_id"] == query for st in states) else None
    if target:
        rel = ws_cmd("search/related", {"item_type": "entity", "item_id": target})
        if isinstance(rel, dict) and not rel.get("error"):
            out["used_by"] = {k: v for k, v in rel.items()
                              if k in ("automation", "script", "scene", "group", "person", "automation_blueprint",
                                       "script_blueprint")}
            out["belongs_to"] = {k: v for k, v in rel.items()
                                 if k in ("area", "device", "integration", "config_entry", "floor", "label")}
            if not out["used_by"]:
                out["used_by"] = "nothing: no automation, script, scene or group refers to it"
            out["note"] = ("used_by covers automations, scripts and scenes, including YAML ones. Dashboards and "
                           "templates are not indexed; search the files with ha_file action=search for those.")
    elif query and a.get("in_config"):
        out["in_config"] = _search_configs(query)
    return out


def _search_configs(query):
    """Automations and scripts whose configuration mentions the query, via the config API."""
    rx = re.compile(re.escape(query), re.I)
    found = []
    states = core("GET", "/states").get("body") or []
    for st in states:
        eid = st["entity_id"]
        if eid.startswith("automation."):
            cid, kind = st.get("attributes", {}).get("id"), "automation"
        elif eid.startswith("script."):
            cid, kind = eid.split(".", 1)[1], "script"
        else:
            continue
        if not cid:
            continue
        cfg = core("GET", f"/config/{kind}/config/{cid}")
        if cfg.get("status") == 200 and rx.search(json.dumps(cfg.get("body"), ensure_ascii=False)):
            found.append({"entity_id": eid, "name": st.get("attributes", {}).get("friendly_name"), "id": cid})
    return found


# ---------------------------------------------------------------------- overview
def t_overview(a):
    """What needs attention, in one call: the questions you ask first when something is off."""
    cfg = core("GET", "/config").get("body") or {}
    states = core("GET", "/states").get("body") or []
    by_domain = {}
    for st in states:
        by_domain[st["entity_id"].split(".", 1)[0]] = by_domain.get(st["entity_id"].split(".", 1)[0], 0) + 1
    reg = ws_cmd("config/entity_registry/list")
    disabled = {e["entity_id"] for e in reg if e.get("disabled_by")} if isinstance(reg, list) else set()
    unavailable = sorted(st["entity_id"] for st in states
                         if st["state"] == "unavailable" and st["entity_id"] not in disabled
                         and not st.get("attributes", {}).get("restored"))
    updates = [{"entity_id": st["entity_id"], "title": st["attributes"].get("title"),
                "installed": st["attributes"].get("installed_version"),
                "latest": st["attributes"].get("latest_version")}
               for st in states if st["entity_id"].startswith("update.") and st["state"] == "on"]
    repairs = ws_cmd("repairs/list_issues")
    repairs = [{"domain": i.get("domain"), "issue": i.get("translation_key") or i.get("issue_id"),
                "severity": i.get("severity"), "placeholders": i.get("translation_placeholders")}
               for i in (repairs.get("issues", []) if isinstance(repairs, dict) else [])
               if not i.get("ignored") and i.get("active", True)]
    entries = ws_cmd("config_entries/get")
    broken = [{"domain": e["domain"], "title": e["title"], "state": e["state"], "reason": e.get("reason")}
              for e in (entries if isinstance(entries, list) else [])
              if e.get("state") not in ("loaded", "not_loaded") and not e.get("disabled_by")]
    notes = ws_cmd("persistent_notification/get")
    errors = ws_cmd("system_log/list")
    addons = sup("GET", "/addons").get("body") or {}
    stopped = [x["slug"] for x in (addons.get("data") or {}).get("addons", [])
               if x.get("state") in ("error",)]
    out = {
        "home_assistant": {k: cfg.get(k) for k in ("version", "location_name", "time_zone", "state",
                                                   "safe_mode", "recovery_mode") if cfg.get(k) is not None},
        "entities": {"total": len(states), "by_domain": dict(sorted(by_domain.items(), key=lambda x: -x[1]))},
        "unavailable": {"count": len(unavailable), "entities": unavailable[:60]},
        "updates": updates,
        "repairs": repairs,
        "integrations_failing": broken,
        "addons_in_error": stopped,
        "notifications": [{"title": n.get("title"), "message": (n.get("message") or "")[:300]}
                          for n in (notes if isinstance(notes, list) else [])],
        "log": {"errors": sum(1 for e in errors if e.get("level") in ("ERROR", "CRITICAL")),
                "warnings": sum(1 for e in errors if e.get("level") == "WARNING")}
        if isinstance(errors, list) else errors,
    }
    return out


# ------------------------------------------------------------------------ camera
def t_camera(a):
    """A camera snapshot as an image the model can actually look at."""
    eid = a["entity_id"].strip()
    if not eid.startswith("camera."):
        return {"error": "entity_id must be a camera.* entity"}
    params = {"width": int(a.get("width") or 1024)}
    res = _http(CORE_REST, "GET", f"/camera_proxy/{eid}", params=params, timeout=30, raw=True)
    if res.get("status") != 200 or not res.get("data"):
        return {"error": f"no image from {eid}", "status": res.get("status"), "detail": res.get("error")}
    ctype = res.get("content_type") or "image/jpeg"
    if not ctype.startswith("image/"):
        return {"error": f"{eid} returned {ctype}, not an image"}
    return {"__content__": [
        {"type": "image", "mimeType": ctype, "data": base64.b64encode(res["data"]).decode()},
        {"type": "text", "text": f"{eid}, {len(res['data'])} bytes, {_local(datetime.now().timestamp())}"}]}


# -------------------------------------------------------------------- dashboards
def _pointer(path):
    if path in ("", "/"):
        return []
    if not path.startswith("/"):
        raise ValueError(f"a path starts with /, as in /views/0/cards/2 (got {path!r})")
    return [p.replace("~1", "/").replace("~0", "~") for p in path[1:].split("/")]


def _step(node, key):
    if isinstance(node, list):
        return node[int(key)]
    return node[key]


def _apply(doc, op):
    """One JSON Patch operation (add, replace, remove, move) applied in place."""
    kind, parts = op.get("op"), _pointer(op.get("path", ""))
    if not parts:
        raise ValueError("the whole dashboard cannot be patched at once; use a path below it")
    parent = doc
    for key in parts[:-1]:
        parent = _step(parent, key)
    last = parts[-1]
    if kind == "move":
        value = _apply(doc, {"op": "remove", "path": op["from"]})
        return _apply(doc, {"op": "add", "path": op["path"], "value": value})
    if isinstance(parent, list):
        idx = len(parent) if last == "-" else int(last)
        if kind == "add":
            parent.insert(idx, op["value"])
        elif kind == "replace":
            parent[idx] = op["value"]
        elif kind == "remove":
            return parent.pop(idx)
        else:
            raise ValueError(f"unsupported op: {kind}")
    else:
        if kind in ("add", "replace"):
            if kind == "replace" and last not in parent:
                raise KeyError(last)
            parent[last] = op["value"]
        elif kind == "remove":
            return parent.pop(last)
        else:
            raise ValueError(f"unsupported op: {kind}")
    return None


def t_dashboard(a):
    """Read a dashboard, or part of one, and change it a piece at a time.

    Saving a dashboard means sending the whole configuration back, and a real one
    runs to hundreds of kilobytes -- far too much to pass through a tool argument
    reliably. A patch names only what changes; the add-on reads the current
    configuration, backs it up, applies the patch and saves the result.
    """
    action = a.get("action") or "get"
    if action == "list":
        return ws_cmd("lovelace/dashboards/list")
    url_path = a.get("url_path") or None
    if url_path in ("lovelace", "default"):
        url_path = None
    cfg = ws_cmd("lovelace/config", {"url_path": url_path})
    if not isinstance(cfg, dict) or cfg.get("error"):
        return cfg
    if action == "get":
        node = cfg
        try:
            for key in _pointer(a.get("path") or ""):
                node = _step(node, key)
        except (KeyError, IndexError, ValueError) as e:
            return {"error": f"path not found: {e}"}
        if not a.get("path"):
            views = [{"index": i, "path": v.get("path"), "title": v.get("title"),
                      "type": v.get("type"), "sections": len(v.get("sections") or []),
                      "cards": len(v.get("cards") or [])} for i, v in enumerate(cfg.get("views") or [])]
            if a.get("summary", True):
                return {"url_path": url_path or "lovelace", "views": views,
                        "hint": "Pass path=/views/<index> for one view, or summary=false for everything."}
        return node
    if action != "patch":
        return {"error": f"unknown action: {action}"}
    ops = json.loads(a["patch_json"]) if isinstance(a.get("patch_json"), str) else a.get("patch_json")
    if isinstance(ops, dict):
        ops = [ops]
    if not isinstance(ops, list) or not ops:
        return {"error": "patch_json must be a JSON Patch list, e.g. "
                         '[{"op":"replace","path":"/views/0/title","value":"Home"}]'}
    saved = backup(f"dashboard_{url_path or 'lovelace'}.json", json.dumps(cfg, ensure_ascii=False, indent=1))
    new = json.loads(json.dumps(cfg))
    for i, op in enumerate(ops):
        try:
            _apply(new, op)
        except (KeyError, IndexError, ValueError, TypeError) as e:
            return {"error": f"operation {i} ({op.get('op')} {op.get('path')}) failed: {e!r}; nothing was saved"}
    res = ws_cmd("lovelace/config/save", {"url_path": url_path, "config": new})
    if isinstance(res, dict) and res.get("error"):
        return res
    return {"saved": url_path or "lovelace", "operations": len(ops), "backup": saved}


# ------------------------------------------------------------------------ backups
def backup(label, content):
    """Keep the previous version of whatever is about to be overwritten or deleted.

    Every write tool calls this BEFORE it changes anything, and refuses to go ahead
    when it fails: an edit that cannot be undone is worse than an edit that did not
    happen. The copies land in the share folder, next to everything else the add-on
    writes, and only the newest BACKUP_KEEP are kept.
    """
    os.makedirs(BACKUP_DIR, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", label).strip("_")[:120] or "item"
    path = os.path.join(BACKUP_DIR, f"{datetime.now(TZ).strftime('%Y%m%d-%H%M%S-%f')}_{safe}")
    with open(path, "wb" if isinstance(content, bytes) else "w") as f:
        f.write(content)
    try:
        old = sorted(os.listdir(BACKUP_DIR))[:-BACKUP_KEEP]
        for name in old:
            os.remove(os.path.join(BACKUP_DIR, name))
    except Exception as e:
        log(f"pruning old backups failed: {e}", "warning")
    return path


# ------------------------------------------------------------ files under /config
def file_mode():
    mode = OPTIONS.get("file_access") or "read_only"
    return mode if mode in ("off", "read_only", "read_write") else "read_only"


def _resolve(path):
    """Map a user-supplied path onto CONFIG_DIR, refusing anything that escapes it.

    People write the same file as `template.yaml`, `/config/template.yaml` or
    `/homeassistant/template.yaml`; all three mean the file in Home Assistant's
    configuration directory. The resolved path is checked AFTER symlinks are
    followed, so a link cannot lead out of the directory either.
    """
    rel = (path or "").strip().replace("\\", "/").lstrip("/")
    for prefix in ("config/", "homeassistant/"):
        if rel.startswith(prefix):
            rel = rel[len(prefix):]
    if rel in ("config", "homeassistant"):
        rel = ""
    root = os.path.realpath(CONFIG_DIR)
    full = os.path.realpath(os.path.join(root, rel))
    if full != root and not full.startswith(root + os.sep):
        raise ValueError(f"{path!r} lies outside the Home Assistant configuration directory")
    return full, "" if full == root else os.path.relpath(full, root)


def _refusal(rel, writing):
    """Why this file may not be touched, or None. Applies whatever file_access says."""
    parts = rel.split(os.sep) if rel else []
    top, name = (parts[0] if parts else ""), (parts[-1] if parts else "")
    # Login data: refresh tokens, password hashes, the Nabu Casa account. The model
    # has no use for them, and a tool response is not a place a credential belongs.
    if (top == ".storage" and (name.startswith("auth") or name == "onboarding")) or top == ".cloud":
        return "this file holds login credentials and is never read or written by this add-on"
    if writing and top == ".storage":
        return ("Home Assistant owns .storage and overwrites it from memory; a change made here "
                "is lost or corrupts the file. Use the registry, dashboard or config tools instead")
    if writing and rel == "secrets.yaml":
        return ("secrets.yaml is shown with its values masked, so a rewrite from that would destroy "
                "every secret. Edit it by hand")
    return None


def _mask_secrets(text):
    """secrets.yaml with the keys intact and every value replaced.

    Fails closed: if the file cannot be parsed, nothing is returned at all, because
    a line-by-line mask would leak a multi-line value.
    """
    import yaml
    try:
        data = yaml.load(text, Loader=_yaml_loader()) or {}
    except Exception:
        data = None
    if not isinstance(data, dict):
        return "# secrets.yaml could not be parsed, so its content is withheld rather than half-masked"
    return "".join(f"{k}: \"********\"\n" for k in data) + \
        "# values masked by the MCP add-on; the keys are what !secret refers to\n"


def _yaml_loader():
    """A SafeLoader that accepts Home Assistant's own tags (!include, !secret, !input ...).

    Only used to CHECK a file, never to interpret it, so every custom tag simply
    becomes None. Without this, a perfectly valid configuration.yaml fails to load.
    """
    import yaml

    class Loader(yaml.SafeLoader):
        pass
    Loader.add_multi_constructor("!", lambda loader, suffix, node: None)
    return Loader


def _yaml_error(text):
    try:
        import yaml
        yaml.load(text, Loader=_yaml_loader())
        return None
    except Exception as e:
        mark = getattr(e, "problem_mark", None)
        where = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
        return f"{getattr(e, 'problem', None) or e}{where}"


# What to do after a file changed, so the change actually takes effect.
RELOAD_HINTS = {
    "automations.yaml": "automation.reload", "scripts.yaml": "script.reload",
    "scenes.yaml": "scene.reload", "groups.yaml": "group.reload",
    "customize.yaml": "homeassistant.reload_core_config",
}
SKIP_DIRS = {".git", "__pycache__", "deps", ".cache", "tts", ".cloud", "node_modules"}
MAX_FILE = 5 * 1024 * 1024


def _after_write(rel):
    base = os.path.basename(rel)
    if base in RELOAD_HINTS:
        return f"Takes effect after {RELOAD_HINTS[base]} (call it with ha_service)."
    if rel.endswith((".yaml", ".yml")):
        return ("Run ha_check_config, then reload what the file configures (for instance "
                "template.reload) or restart Home Assistant.")
    return None


def _walk(full, recursive):
    if not recursive:
        for name in sorted(os.listdir(full)):
            yield os.path.join(full, name)
        return
    for dirpath, dirs, files in os.walk(full):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        for name in sorted(dirs) + sorted(files):
            yield os.path.join(dirpath, name)


def _read_text(full):
    if os.path.getsize(full) > MAX_FILE:
        return None, f"file is larger than {MAX_FILE // 1024 // 1024} MB"
    with open(full, "rb") as f:
        data = f.read()
    if b"\x00" in data[:8192]:
        return None, f"binary file ({len(data)} bytes)"
    return data.decode("utf-8", "replace"), None


def t_file(a):
    mode = file_mode()
    if mode == "off":
        return {"error": "file access is switched off in the add-on configuration (option file_access)"}
    if not os.path.isdir(CONFIG_DIR):
        return {"error": f"{CONFIG_DIR} is not mounted. Restart the add-on after updating it, so the "
                         "Supervisor mounts the Home Assistant configuration directory."}
    action = a.get("action") or "read"
    writing = action in ("write", "edit", "delete")
    if writing and mode != "read_write":
        return {"error": f"file access is '{mode}'. Set the add-on option file_access to read_write "
                         "to allow writing."}
    try:
        full, rel = _resolve(a.get("path", ""))
    except ValueError as e:
        return {"error": str(e)}
    reason = _refusal(rel, writing)
    if reason:
        return {"error": reason, "path": rel}

    if action in ("list", "search"):
        if not os.path.isdir(full):
            return {"error": f"not a directory: {rel or '/'}"}
        root = os.path.realpath(CONFIG_DIR)
        pattern = a.get("pattern") or ("*.yaml" if action == "search" else "*")
        recursive = bool(a.get("recursive", action == "search"))
        if action == "list":
            out = []
            for p in _walk(full, recursive):
                r = os.path.relpath(p, root)
                if not (fnmatch.fnmatch(os.path.basename(p), pattern) or fnmatch.fnmatch(r, pattern)):
                    continue
                st = os.stat(p)
                out.append({"path": r, "type": "dir" if os.path.isdir(p) else "file",
                            "bytes": None if os.path.isdir(p) else st.st_size,
                            "modified": datetime.fromtimestamp(st.st_mtime, TZ).strftime("%Y-%m-%d %H:%M")})
            return {"count": len(out), "entries": out[:1000], "truncated": len(out) > 1000}
        try:
            rx = re.compile(a["query"], re.IGNORECASE)
        except KeyError:
            return {"error": "search needs a query"}
        except re.error as e:
            return {"error": f"invalid regular expression: {e}"}
        hits, files = [], 0
        for p in _walk(full, recursive):
            r = os.path.relpath(p, root)
            if os.path.isdir(p) or _refusal(r, False):
                continue
            if not (fnmatch.fnmatch(os.path.basename(p), pattern) or fnmatch.fnmatch(r, pattern)):
                continue
            text, _ = _read_text(p)
            if text is None:
                continue
            if r == "secrets.yaml":
                text = _mask_secrets(text)
            files += 1
            for n, line in enumerate(text.splitlines(), 1):
                if rx.search(line):
                    hits.append({"file": r, "line": n, "text": line.strip()[:300]})
            if len(hits) >= 300:
                break
        return {"files_searched": files, "hits": hits[:300], "truncated": len(hits) >= 300}

    if action == "read":
        if os.path.isdir(full):
            return t_file({"action": "list", "path": rel})
        if not os.path.exists(full):
            return {"error": f"no such file: {rel}"}
        text, why = _read_text(full)
        if text is None:
            return {"error": why, "path": rel}
        if rel == "secrets.yaml":
            text = _mask_secrets(text)
        lines = text.splitlines(keepends=True)
        first = max(int(a.get("start_line") or 1), 1)
        count = int(a.get("max_lines") or 0) or len(lines)
        part = lines[first - 1:first - 1 + count]
        out = {"path": rel, "total_lines": len(lines), "content": "".join(part)}
        if first > 1 or len(part) < len(lines):
            out["lines"] = f"{first}-{first + len(part) - 1}"
        return out

    if action == "delete":
        if not os.path.isfile(full):
            return {"error": f"no such file: {rel} (directories are not deleted)"}
        with open(full, "rb") as f:
            saved = backup("file_" + rel, f.read())
        os.remove(full)
        return {"deleted": rel, "backup": saved}

    if action not in ("write", "edit"):
        return {"error": f"unknown action: {action}"}
    if os.path.isdir(full):
        return {"error": f"{rel} is a directory"}
    old = None
    if os.path.exists(full):
        with open(full, "rb") as f:
            old = f.read()
    if action == "edit":
        if old is None:
            return {"error": f"no such file: {rel}"}
        find, repl = a.get("old_text"), a.get("new_text")
        if not find or repl is None:
            return {"error": "edit needs old_text and new_text"}
        text = old.decode("utf-8")
        hits = text.count(find)
        if hits != 1:
            return {"error": f"old_text occurs {hits} times in {rel}; it must match exactly once. "
                             "Include more surrounding lines to make it unique."}
        content = text.replace(find, repl)
    else:
        content = a.get("content")
        if content is None:
            return {"error": "write needs content"}
    if rel.endswith((".yaml", ".yml")):
        problem = _yaml_error(content)
        if problem:
            return {"error": f"not written: the result is not valid YAML ({problem})", "path": rel}
    data = content.encode("utf-8")
    if old == data:
        return {"path": rel, "unchanged": True}
    saved = backup("file_" + rel, old) if old is not None else None
    os.makedirs(os.path.dirname(full), exist_ok=True)
    # Write beside the target and rename over it, so Home Assistant never reads a
    # half-written file.
    tmp = f"{full}.mcp-{secrets.token_hex(4)}"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, full)
    out = {"path": rel, "bytes": len(data), "created": old is None, "backup": saved}
    hint = _after_write(rel)
    if hint:
        out["next"] = hint
    return out


def t_check_config(a):
    """Validate the YAML configuration the way the UI's "Check configuration" does."""
    res = core("POST", "/config/core/check_config")
    return res.get("body") if res.get("status") == 200 else res


# ------------------------------------------------------------------------ traces
def _trace_item_id(entity_id):
    """Traces are stored under the automation's unique id, not under its entity id."""
    reg = ws_cmd("config/entity_registry/get", {"entity_id": entity_id})
    if isinstance(reg, dict) and reg.get("unique_id"):
        return str(reg["unique_id"])
    return entity_id.split(".", 1)[1]


def _trace_bucket(path, domain):
    head = path.split("/", 1)[0]
    if head == "trigger":
        return "trigger"
    if head == "condition":
        return "condition"
    if head in ("action", "sequence") or (domain == "script" and head.isdigit()):
        return "action"
    return None


def _trace_detail(entity_id, domain, trace, sections):
    """A trace boiled down to what explains a run: why it started, which conditions
    held, what each step did and returned. The raw trace repeats every variable at
    every step and is many times larger."""
    steps = {"trigger": [], "condition": [], "action": []}
    for path, entries in (trace.get("trace") or {}).items():
        bucket = _trace_bucket(path, domain)
        if bucket and isinstance(entries, list):
            steps[bucket] += [dict(e, path=path) for e in entries]
    for lst in steps.values():
        lst.sort(key=lambda e: (e.get("timestamp", ""), e.get("path", "")))
    out = {"entity_id": entity_id, "run_id": trace.get("run_id"),
           "timestamp": trace.get("timestamp"), "state": trace.get("state"),
           "script_execution": trace.get("script_execution")}
    if steps["trigger"]:
        first = steps["trigger"][0]
        vars_ = (first.get("changed_variables") or {}).get("trigger") or \
                (first.get("variables") or {}).get("trigger") or {}
        trig = {k: vars_.get(k) for k in ("platform", "description", "entity_id") if vars_.get(k)}
        for k in ("from_state", "to_state"):
            if isinstance(vars_.get(k), dict):
                trig[k] = vars_[k].get("state")
        if first.get("error"):
            trig["error"] = first["error"]
        out["trigger"] = trig
    elif trace.get("trigger"):
        out["trigger"] = {"description": trace["trigger"]}
    out["conditions"] = [{k: v for k, v in {"path": c["path"], "result": (c.get("result") or {}).get("result"),
                                            "error": c.get("error")}.items() if v is not None}
                         for c in steps["condition"]]
    actions, last = [], None
    for s in steps["action"]:
        item = {"path": s["path"], "timestamp": s.get("timestamp")}
        for k in ("result", "error", "child_id"):
            if s.get(k):
                item[k] = s[k]
        variables = s.get("changed_variables") or {}
        # `context` changes on every step and says nothing about the run.
        useful = {k: v for k, v in variables.items() if v is not None and k not in ("trigger", "context")}
        fingerprint = json.dumps(useful, sort_keys=True, default=str)
        if useful and fingerprint != last:
            item["variables"] = useful
            last = fingerprint
        actions.append(item)
    out["actions"] = actions
    cfg = trace.get("config") or {}
    out["config"] = {"alias": cfg.get("alias"), "mode": cfg.get("mode", "single")}
    if trace.get("error"):
        out["error"] = trace["error"]
    if sections:
        keep = {s.strip() for s in sections.split(",")} | {"entity_id", "run_id", "timestamp", "state",
                                                           "script_execution"}
        keep |= {"conditions"} if "condition" in keep else set()
        keep |= {"actions"} if "action" in keep else set()
        out = {k: v for k, v in out.items() if k in keep}
    return {k: v for k, v in out.items() if v not in (None, [], {})}


def t_traces(a):
    entity_id = a["entity_id"].strip()
    domain = entity_id.split(".", 1)[0]
    if domain not in ("automation", "script"):
        return {"error": "entity_id must be an automation.* or script.* entity"}
    item_id = _trace_item_id(entity_id)
    if a.get("run_id"):
        trace = ws_cmd("trace/get", {"domain": domain, "item_id": item_id, "run_id": a["run_id"]})
        if not isinstance(trace, dict) or trace.get("error"):
            return trace
        return _trace_detail(entity_id, domain, trace, a.get("sections"))
    runs = ws_cmd("trace/list", {"domain": domain, "item_id": item_id})
    if isinstance(runs, dict):
        return runs
    runs = list(reversed(runs or []))
    limit = int(a.get("limit") or 10)
    out = {"entity_id": entity_id, "stored": len(runs), "runs": [
        {k: v for k, v in {"run_id": r.get("run_id"), "start": (r.get("timestamp") or {}).get("start"),
                           "state": r.get("state"), "trigger": r.get("trigger"),
                           "execution": r.get("script_execution"), "error": r.get("error"),
                           "last_step": r.get("last_step")}.items() if v}
        for r in runs[:limit]]}
    if not runs:
        # "No traces" has four different causes; say which one applies.
        st = core("GET", f"/states/{entity_id}")
        body = st.get("body") if st.get("status") == 200 else None
        if not isinstance(body, dict):
            out["why"] = f"{entity_id} does not exist"
        elif body.get("state") == "off":
            out["why"] = "it is switched off, so it never runs"
        elif not body.get("attributes", {}).get("last_triggered"):
            out["why"] = "it has never run since it was created"
        else:
            out["why"] = ("it has run, but no traces are stored: they are kept in memory and lost on a "
                          "restart, and stored_traces may be 0 in its configuration")
    else:
        out["hint"] = "Pass a run_id to see why that run did what it did."
    return out


S = lambda d, ex=None: {"type": "string", "description": d + (f" Example: {ex}" if ex else "")}
B = lambda d: {"type": "boolean", "description": d}
N = lambda d: {"type": "number", "description": d}
E = lambda d, values: {"type": "string", "enum": values, "description": d}


def T(desc, props, req, ro=False, destructive=False):
    """A tool definition. `ro` and `destructive` become MCP annotations, which clients
    use to decide what to ask permission for -- and which the read-only token uses
    to decide what it may call at all."""
    return {"description": desc,
            "inputSchema": {"type": "object", "properties": props, "required": req},
            "annotations": {"readOnlyHint": ro, "destructiveHint": destructive and not ro,
                            "openWorldHint": False}}


TIME = S("Start: an ISO time, or relative such as 6h, 3d, 2w. Defaults to 24h ago.", "2026-09-27T06:00")

TOOLS = {
 "ha_overview": (T("Start here when something seems wrong: Home Assistant version and state, entity counts, "
   "unavailable entities, pending updates, open repairs, failing integrations, add-ons in error, "
   "notifications, and how many errors are in the log.", {}, [], ro=True), t_overview),
 "ha_search": (T("Find entities by words in their id, name, area, device or aliases, typo-tolerant. With an exact "
   "entity_id as query it also says which automations, scripts, scenes and groups use it (YAML ones "
   "included), and which device, area and integration it belongs to.",
   {"query": S("Words or an exact entity_id.", "zwembad warmtepomp"),
    "domain": S("Only this domain.", "sensor"), "area": S("Only this area (name or id)."),
    "in_config": B("Also search the text of every automation and script configuration (slower)."),
    "limit": N("Maximum entities returned. Defaults to 25.")}, [], ro=True), t_search),
 "ha_states": (T("Read entities: a single one with all of its attributes, or a filtered list.",
   {"entity_id": S("A single entity_id for full detail."), "domain": S("Filter by domain.", "sensor"),
    "search": S("Filter on text in the id or the friendly name.")}, [], ro=True), t_states),
 "ha_history": (T("State changes of one or more entities over a period, as compact [time, state] pairs in "
   "Home Assistant's time zone. Only significant changes unless all_changes is set.",
   {"entity_id": S("Entity or entities, comma separated."), "start": TIME,
    "end": S("End, same formats. Defaults to now."), "hours": N("Alternative to start: hours back."),
    "attributes": B("Include attributes (much larger)."),
    "all_changes": B("Every change, not only significant ones.")}, [], ro=True), t_history),
 "ha_statistics": (T("Long-term statistics, kept for years: hourly/daily/monthly mean, min and max for "
   "measurements, sum and change for meters (energy, gas, water). Use this, not history, for anything older "
   "than about ten days or for totals per day.",
   {"statistic_ids": S("Entity ids (or external statistic ids), comma separated.", "sensor.p1_meter_energy"),
    "start": S("Start: ISO time, or relative such as 30d. Defaults to 30 days ago."),
    "end": S("End. Defaults to now."),
    "period": E("Bucket size. Defaults to day.", ["5minute", "hour", "day", "week", "month"]),
    "types": S("Comma separated subset of mean,min,max,sum,state,change.")},
   ["statistic_ids"], ro=True), t_statistics),
 "ha_logbook": (T("Logbook: who or what changed something, and when.",
   {"entity_id": S("Restrict to a single entity."), "start": TIME, "end": S("End. Defaults to now."),
    "hours": N("Alternative to start: hours back.")}, [], ro=True), t_logbook),
 "ha_error_log": (T("Logs. source=errors gives Home Assistant's own de-duplicated list of warnings and errors "
   "since the last start, each with a count and traceback: the best place to begin. core, host and supervisor "
   "give the raw log tail; search and level filter it over the last few thousand lines.",
   {"source": E("Which log. Defaults to core.", ["errors", "core", "host", "supervisor"]),
    "lines": N("How many lines or entries to return. Defaults to 100, at most 5000."),
    "search": S("Only lines containing this text (case-insensitive).", "dobiss"),
    "level": E("Only this level and worse.", ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"])},
   [], ro=True), t_errorlog),
 "ha_traces": (T("Why an automation or script did (or did not do) what it did. Without run_id: its recent runs "
   "with trigger, result and errors. With run_id: that run step by step -- trigger, each condition's result, "
   "each action with its result and the variables it changed.",
   {"entity_id": S("automation.* or script.* entity.", "automation.boost_zwembad"),
    "run_id": S("One run, from the list."), "limit": N("Runs to list. Defaults to 10."),
    "sections": S("Only these parts of a run: trigger, condition, action, config, error.")},
   ["entity_id"], ro=True), t_traces),
 "ha_template": (T("Render a Jinja2 template inside Home Assistant. Powerful for computing or summarising "
   "across many entities at once.", {"template": S("The template.")}, ["template"], ro=True), t_template),
 "ha_service": (T("Call a Home Assistant service. Every service in every domain is available. "
   "homeassistant.restart first checks the configuration and refuses if it is invalid.",
   {"domain": S("Domain.", "light"), "service": S("Service.", "turn_on"),
    "entity_id": S("Target entity or entities, comma separated."),
    "data_json": S("Service data as JSON text."),
    "return_response": B("true for services that return data."),
    "skip_config_check": B("Restart even if the configuration check fails.")},
   ["domain", "service"]), t_service),
 "ha_config_get": (T("Read the configuration of an automation, script, scene or helper.",
   {"kind": S("automation, script, scene, input_boolean, input_number, input_select, input_text or template."),
    "object_id": S("Object id. Omit to list them all.")}, ["kind"], ro=True), t_cfg_get),
 "ha_config_save": (T("Create an automation, script, scene or helper, or overwrite an existing one. "
   "CAUTION: this replaces the ENTIRE configuration; read it with ha_config_get first. The previous version "
   "is backed up first, and for automations and scripts the answer lists entities or services that do not "
   "exist and constructs a native trigger or condition would express better.",
   {"kind": S("Kind of object."), "object_id": S("Object id. A new unique id creates a new object."),
    "config_json": S("The complete configuration as JSON text.")},
   ["kind", "object_id", "config_json"]), t_cfg_save),
 "ha_config_delete": (T("Delete an automation, script, scene or helper. It is backed up first.",
   {"kind": S("Kind of object."), "object_id": S("Object id.")}, ["kind", "object_id"],
   destructive=True), t_cfg_del),
 "ha_check_config": (T("Check the YAML configuration the way Settings > System > Restart does. Run it after "
   "editing a YAML file and before restarting.", {}, [], ro=True), t_check_config),
 "ha_file": (T("Files in the Home Assistant configuration directory (configuration.yaml, template.yaml, "
   "packages, blueprints, www). list and read, search (grep across files), and -- if the add-on option "
   "file_access is read_write -- write, edit (replace one exact piece of text) and delete. Every write or "
   "delete keeps the previous version in /share/ha-mcp/backups, and YAML is checked before it is written. "
   "secrets.yaml is shown masked; .storage is read-only.",
   {"action": E("What to do. Defaults to read.", ["list", "read", "search", "write", "edit", "delete"]),
    "path": S("Relative to the configuration directory.", "template.yaml"),
    "content": S("write: the complete new file content."),
    "old_text": S("edit: the exact text to replace; must occur exactly once."),
    "new_text": S("edit: what replaces it."),
    "query": S("search: a regular expression, case-insensitive.", "sensor\\.zwembad"),
    "pattern": S("list/search: file name pattern. search defaults to *.yaml.", "*.yaml"),
    "recursive": B("list: include subdirectories. search always does."),
    "start_line": N("read: first line (1-based)."), "max_lines": N("read: how many lines.")},
   ["action"], destructive=True), t_file),
 "ha_dashboard": (T("Dashboards. list them; get one (by default an outline of its views, or one part with "
   "path=/views/2); patch changes only what you name, with JSON Patch operations, after backing the "
   "dashboard up. Use patch rather than sending a whole dashboard back.",
   {"action": E("What to do. Defaults to get.", ["list", "get", "patch"]),
    "url_path": S("The dashboard's url_path; omit for the default dashboard.", "dashboard-klimaat"),
    "path": S("get: a JSON pointer into the configuration.", "/views/0/sections/1"),
    "summary": B("get without path: false returns the complete configuration."),
    "patch_json": S("patch: a JSON Patch list (add, replace, remove, move).",
                    '[{"op":"replace","path":"/views/0/title","value":"Home"}]')},
   []), t_dashboard),
 "ha_camera": (T("A snapshot of a camera, returned as an image you can look at.",
   {"entity_id": S("camera.* entity."), "width": N("Scale to this width in pixels. Defaults to 1024.")},
   ["entity_id"], ro=True), t_camera),
 "ha_registry": (T("List a registry: entities, devices, areas, floors, labels or integrations.",
   {"what": E("Which registry to list.", ["entities", "devices", "areas", "floors", "labels", "integrations"])},
   ["what"], ro=True), t_registry),
 "ha_expose": (T("Expose entities to Assist, or hide them again. This is what decides what the voice "
   "assistant can see.",
   {"entity_ids": S("Comma separated."), "expose": B("true = expose, false = hide.")},
   ["entity_ids"]), t_expose),
 "ha_addons": (T("List every installed add-on with its state.", {}, [], ro=True), t_addons),
 "ha_addon_action": (T("Manage an add-on: read its info, read its logs, start, stop, restart or update it.",
   {"slug": S("Add-on slug.", "a0d7b954_nodered"),
    "action": E("What to do.", ["info", "logs", "start", "stop", "restart", "update"]),
    "lines": N("logs: how many lines. Defaults to 200.")}, ["slug", "action"]), t_addon_action),
 "ha_download": (T("Fetch an endpoint and write the result as a FILE into the share folder, subdirectory "
   "ha-mcp. Use this for binary data: downloading a backup, a camera snapshot, a log file.",
   {"endpoint": S("Path.", "/backups/abc12345/download"),
    "target": S("'supervisor' (default) or 'core' for the Home Assistant API.")}, ["endpoint"]),
   t_download),
 "ha_upload": (T("Send a file from the share folder to an endpoint as a multipart upload. Required for "
   "anything that takes a file rather than JSON, such as restoring a backup.",
   {"file": S("File name inside the share subdirectory ha-mcp, or an absolute path.", "backup.tar"),
    "endpoint": S("Path.", "/backups/new/upload"),
    "field": S("Name of the form field. Defaults to 'file'."),
    "target": S("'supervisor' (default) or 'core'.")}, ["file", "endpoint"]), t_upload),
 "ha_supervisor": (T("Arbitrary call against the Supervisor API: host, OS, backups, network, add-on store. "
   "Restarting the core or rebooting the host checks the configuration first.",
   {"endpoint": S("Path.", "/backups"), "method": S("GET or POST. Defaults to GET."),
    "body_json": S("Request body as JSON text."),
    "skip_config_check": B("Restart or reboot even if the configuration check fails.")}, ["endpoint"]),
   t_supervisor),
 "ha_rest": (T("Arbitrary call against the Home Assistant REST API. Every endpoint is reachable this way.",
   {"path": S("Path, with or without the /api prefix.", "/states"),
    "method": S("GET, POST or DELETE. Defaults to GET."),
    "body_json": S("Request body as JSON text.")}, ["path"]), t_rest),
 "ha_ws": (T("Arbitrary command against the WebSocket API. The web interface uses it for nearly everything, "
   "so this reaches what REST does not offer: registries, integrations, dashboards, users, backups.",
   {"command": S("Command type.", "config/area_registry/list"),
    "params_json": S("Parameters as JSON text.")}, ["command"]), t_ws),
}

# ------------------------------------------------------------------ read-only access
# A second token, for a client that should be able to look but not touch: a
# dashboard assistant, a family member's chat, an experiment. Tools marked read-only
# are simply allowed. The escape hatches are allowed only for calls that cannot
# change anything; everything else is refused before it reaches Home Assistant.
RO_WS = re.compile(r"(^|/)(list|get|info|config|related|list_issues|current_user|log_info|"
                   r"statistics_during_period|list_statistic_ids|history_during_period|"
                   r"get_events|get_prefs|get_states|get_config|get_services|subscriptions)$")
RO_WS_DENY = re.compile(r"(^|/)(auth/|config/auth/)")


def readonly_refusal(name, args):
    """Why a read-only client may not make this call, or None."""
    spec = TOOLS[name][0]
    if spec["annotations"]["readOnlyHint"]:
        return None
    method = (args.get("method") or "GET").upper()
    if name in ("ha_rest", "ha_supervisor") and method == "GET":
        return None
    if name == "ha_file" and (args.get("action") or "read") in ("list", "read", "search"):
        return None
    if name == "ha_dashboard" and (args.get("action") or "get") in ("list", "get"):
        return None
    if name == "ha_addon_action" and args.get("action") in ("info", "logs"):
        return None
    if name == "ha_ws" and RO_WS.search(args.get("command") or "") and not RO_WS_DENY.search(args.get("command")):
        return None
    return (f"{name} with these arguments can change Home Assistant, and this client connected with the "
            "read-only token. Nothing was changed.")


def readonly_tool_visible(name):
    """What a read-only client sees in tools/list: everything it can use at least partly."""
    return TOOLS[name][0]["annotations"]["readOnlyHint"] or name in (
        "ha_rest", "ha_ws", "ha_supervisor", "ha_file", "ha_dashboard", "ha_addon_action")


INSTRUCTIONS = """This server is a Home Assistant installation, with full administrative access.
- To orient yourself, or when something is wrong, start with ha_overview; for "which entity is ...", ha_search.
- Before changing an automation or script, read it with ha_config_get; to see why it misbehaved, ha_traces.
- ha_error_log source=errors is the de-duplicated error list; the raw log comes with search and level filters.
- Every overwrite or delete made through ha_config_save, ha_config_delete, ha_file and ha_dashboard keeps the
  previous version in /share/ha-mcp/backups (the path is in the answer), so a change can be undone.
- After editing YAML, run ha_check_config before reloading or restarting.
- Relative times are in the Home Assistant time zone."""


# ---------------------------------------------------------------------- JSON-RPC
def handle_rpc(req, readonly=False):
    """Return a response dict, or None for a notification."""
    method, rid = req.get("method"), req.get("id")
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": req.get("params", {}).get("protocolVersion", "2024-11-05"),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "home-assistant-api", "version": VERSION},
            "instructions": INSTRUCTIONS + ("\n- This connection is READ-ONLY: calls that would change "
                                            "anything are refused." if readonly else "")}}
    if method in ("notifications/initialized", "notifications/cancelled"):
        return None
    if method == "ping":
        return {"jsonrpc": "2.0", "id": rid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {
            "tools": [{"name": n, **spec} for n, (spec, _) in TOOLS.items()
                      if not readonly or readonly_tool_visible(n)]}}
    if method == "tools/call":
        p = req.get("params", {})
        name = p.get("name")
        args = p.get("arguments") or {}
        if name not in TOOLS:
            return {"jsonrpc": "2.0", "id": rid,
                    "error": {"code": -32601, "message": f"unknown tool: {name}"}}
        log(f"tool {name}{' (read-only)' if readonly else ''} "
            f"{json.dumps(args, ensure_ascii=False)[:160]}", "debug")
        refusal = readonly_refusal(name, args) if readonly else None
        if refusal:
            return {"jsonrpc": "2.0", "id": rid,
                    "result": {"content": [{"type": "text", "text": refusal}], "isError": True}}
        try:
            out = TOOLS[name][1](args)
            # A tool that returns something other than text (a camera image) says so.
            if isinstance(out, dict) and "__content__" in out:
                return {"jsonrpc": "2.0", "id": rid, "result": {"content": out["__content__"], "isError": False}}
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


def dispatch(req, readonly=False):
    """Route one request, or a JSON-RPC batch, to handle_rpc.

    A client is free to send an array of calls, and anything that is neither an
    array nor an object is simply malformed. Both used to reach handle_rpc as-is
    and take the connection down with an AttributeError instead of producing the
    error response the caller is entitled to.
    """
    if isinstance(req, list):
        out = [r for r in (dispatch(x, readonly) for x in req) if r is not None]
        return out or None
    if not isinstance(req, dict):
        return {"jsonrpc": "2.0", "id": None,
                "error": {"code": -32600, "message": "invalid request: expected a JSON object"}}
    return handle_rpc(req, readonly)


# -------------------------------------------------------------------------- HTTP
SESSIONS = {}
SESSIONS_LOCK = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        log(f"{self.address_string()} {fmt % args}", "debug")

    def _auth_ok(self):
        """'full', 'readonly' or None, for the token this request carries."""
        got = self.headers.get("Authorization", "")
        if not got.startswith("Bearer "):
            return None
        # compare_digest on str requires both sides to be ASCII; comparing bytes
        # keeps a token with an accent in it from raising instead of returning 401.
        sent = got[7:].strip().encode()
        if secrets.compare_digest(sent, TOKEN.encode()):
            return "full"
        if READONLY_TOKEN and secrets.compare_digest(sent, READONLY_TOKEN.encode()):
            return "readonly"
        return None

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
        access = self._auth_ok()
        if not access:
            return self._deny()
        if path == "/sse":
            return self._sse(access == "readonly")
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
        access = self._auth_ok()
        if not access:
            return self._deny()
        readonly = access == "readonly"
        try:
            req = json.loads(raw or b"{}")
        except Exception:
            return self._send(400, b'{"error":"invalid JSON"}')

        if parsed.path == "/mcp":
            # Stateless: the answer goes straight back in the same request.
            resp = dispatch(req, readonly)
            if resp is None:
                return self._send(202, b"")
            return self._send(200, json.dumps(resp).encode())

        if parsed.path.startswith("/messages"):
            sid = urllib.parse.parse_qs(parsed.query).get("session_id", [""])[0]
            with SESSIONS_LOCK:
                session = SESSIONS.get(sid)
            if session is None:
                return self._send(404, b'{"error":"unknown session"}')
            q, session_readonly = session
            # A session opened with the read-only token stays read-only, whatever
            # token the individual message carries.
            resp = dispatch(req, readonly or session_readonly)
            if resp is not None:
                q.put(resp)
            return self._send(202, b"")

        self._send(404, b'{"error":"unknown path"}')

    def _sse(self, readonly=False):
        sid = secrets.token_hex(16)
        q = queue.Queue()
        with SESSIONS_LOCK:
            SESSIONS[sid] = (q, readonly)
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


# --------------------------------------------------------------- the add-on page
# Served over Supervisor ingress, on its own port, so Home Assistant shows an
# "OPEN WEB UI" button and takes care of logging the user in. This is what turns the
# add-on from something you configure by reading documentation into something you
# open, copy and paste. It never serves the MCP protocol itself: that stays on PORT,
# behind the bearer token, because an MCP client cannot hold a Home Assistant
# session.


def guess_host():
    """The address an MCP client outside this container should connect to.

    Home Assistant's own internal_url is the best answer when it is set; failing
    that, the address of the primary network interface as the Supervisor reports it.
    Neither is guaranteed to be reachable from wherever the client runs, so the page
    presents it as a starting point rather than as the truth.
    """
    cfg = core("GET", "/config").get("body")
    if isinstance(cfg, dict):
        for key in ("internal_url", "external_url"):
            host = urllib.parse.urlparse((cfg.get(key) or "").strip() or "//").hostname
            if host:
                return host
    data = (sup("GET", "/network/info").get("body") or {}).get("data", {})
    for iface in sorted(data.get("interfaces", []), key=lambda i: not i.get("primary")):
        for cidr in (iface.get("ipv4") or {}).get("address", []):
            if cidr:
                return cidr.split("/")[0]
    return "homeassistant.local"


def is_admin(user_id):
    """Whether the Home Assistant user behind an ingress request is an administrator.

    Ingress authenticates the user but does not by itself keep non-administrators
    out, and this page shows the token -- which is unrestricted control over the
    house. `config/auth/list` carries no is_admin field; membership of the
    `system-admin` group is what the frontend checks, so that is what is checked
    here. Returns None when the answer could not be established, which the page
    treats as "not proven" and explains, rather than silently denying.
    """
    if not user_id:
        return False
    users = ws_cmd("config/auth/list")
    if not isinstance(users, list):
        log(f"could not establish whether the ingress user is an administrator: {users}", "warning")
        return None
    for user in users:
        if user.get("id") == user_id:
            return "system-admin" in (user.get("group_ids") or [])
    return False


PAGE_CSS = """
:root {
  color-scheme: light dark;
  --bg: #f4f6f8; --card: #ffffff; --ink: #1c2126; --muted: #5d6b79;
  --line: #e2e7ec; --accent: #03a9f4; --ok: #1f9254; --warn: #b86e00;
  --code-bg: #f7f9fb;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #111418; --card: #1a1f25; --ink: #e8edf2; --muted: #9aa8b6;
    --line: #2a313a; --accent: #4fc3f7; --ok: #4ecb8a; --warn: #e0a34a;
    --code-bg: #12161b;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 24px 16px 64px; background: var(--bg); color: var(--ink);
  font: 15px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
}
.wrap { max-width: 880px; margin: 0 auto; }
header { display: flex; align-items: baseline; gap: 12px; flex-wrap: wrap; margin-bottom: 4px; }
h1 { font-size: 22px; margin: 0; letter-spacing: -0.01em; }
h2 { font-size: 15px; margin: 28px 0 10px; text-transform: uppercase;
     letter-spacing: 0.06em; color: var(--muted); }
p { margin: 0 0 12px; }
.sub { color: var(--muted); margin-bottom: 20px; }
.pill { font-size: 12px; font-weight: 600; padding: 3px 10px; border-radius: 999px;
        background: color-mix(in srgb, var(--ok) 18%, transparent); color: var(--ok); }
.card { background: var(--card); border: 1px solid var(--line); border-radius: 12px;
        padding: 16px 18px; }
.grid { display: grid; gap: 12px; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); }
.grid .card strong { display: block; font-size: 18px; font-weight: 600; }
.grid .card span { color: var(--muted); font-size: 12px; text-transform: uppercase;
                   letter-spacing: 0.05em; }
.row { display: flex; align-items: center; gap: 10px; margin-bottom: 10px; flex-wrap: wrap; }
.row label { min-width: 84px; color: var(--muted); font-size: 13px; }
code, pre { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 13px; }
pre { background: var(--code-bg); border: 1px solid var(--line); border-radius: 8px;
      padding: 12px 14px; overflow-x: auto; margin: 0 0 6px; }
.val { background: var(--code-bg); border: 1px solid var(--line); border-radius: 8px;
       padding: 6px 10px; flex: 1 1 260px; overflow-x: auto; white-space: nowrap; }
button { font: inherit; font-size: 13px; padding: 6px 12px; border-radius: 8px;
         border: 1px solid var(--line); background: var(--card); color: var(--ink);
         cursor: pointer; }
button:hover { border-color: var(--accent); color: var(--accent); }
.secret { transition: filter .15s; }
body.hide-secrets .secret { filter: blur(5px); }
table { width: 100%; border-collapse: collapse; }
td { padding: 7px 10px; border-top: 1px solid var(--line); vertical-align: top; }
tr:first-child td { border-top: 0; }
td.name { white-space: nowrap; font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
          font-size: 13px; color: var(--accent); }
td.what { color: var(--muted); font-size: 13px; }
.note { color: var(--muted); font-size: 13px; }
.warn { color: var(--warn); }
footer { margin-top: 32px; color: var(--muted); font-size: 13px; }
a { color: var(--accent); }
"""

PAGE_JS = """
function copy(id, btn) {
  var text = document.getElementById(id).dataset.value;
  var done = function () { var t = btn.textContent; btn.textContent = 'Copied'; 
    setTimeout(function () { btn.textContent = t; }, 1200); };
  // The clipboard API needs a secure context, which plain http on a local address is
  // not, so the old textarea trick has to stay as a fallback.
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(text).then(done, function () { fallback(text, done); });
  } else { fallback(text, done); }
}
function fallback(text, done) {
  var ta = document.createElement('textarea');
  ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
  document.body.appendChild(ta); ta.select();
  try { document.execCommand('copy'); done(); } catch (e) { window.prompt('Copy:', text); }
  document.body.removeChild(ta);
}
function toggleSecrets(btn) {
  var hidden = document.body.classList.toggle('hide-secrets');
  btn.textContent = hidden ? 'Show secrets' : 'Hide secrets';
}
"""


def _field(label, element_id, value, shown=None, secret=False):
    """One labelled value with a copy button; `shown` may differ from what is copied."""
    css = " secret" if secret else ""
    return (f'<div class="row"><label>{html.escape(label)}</label>'
            f'<div class="val{css}" id="{element_id}" data-value="{html.escape(value, quote=True)}">'
            f'{html.escape(shown if shown is not None else value)}</div>'
            f'<button onclick="copy(\'{element_id}\', this)">Copy</button></div>')


def _block(label, element_id, text, secret=True):
    css = " secret" if secret else ""
    return (f'<h2>{html.escape(label)}</h2>'
            f'<pre class="{css.strip()}" id="{element_id}" '
            f'data-value="{html.escape(text, quote=True)}">{html.escape(text)}</pre>'
            f'<button onclick="copy(\'{element_id}\', this)">Copy</button>')


def render_page(user_name, admin):
    """The whole page, server-rendered: no build step, no assets, no requests out."""
    ping = core("GET", "/")
    connected = ping.get("status") == 200
    url = f"http://{HOST}:{PORT}/mcp"
    token = TOKEN if admin is True else ""
    shown = token or "•" * 32

    cards = [
        ("Home Assistant", "connected" if connected else "unreachable",
         "API answers" if connected else f"HTTP {ping.get('status')} {ping.get('error', '')}"),
        ("Tools", str(len(TOOLS)), "exposed over MCP"),
        ("Time zone", TZ_LABEL.split(" (")[0], "used for relative questions"),
        ("File access", file_mode().replace("_", " "), "to the configuration directory"),
        ("Version", VERSION, "add-on"),
    ]
    card_html = "".join(f'<div class="card"><span>{html.escape(c)}</span>'
                        f'<strong>{html.escape(v)}</strong>'
                        f'<span class="note">{html.escape(n)}</span></div>'
                        for c, v, n in cards)

    if admin is True:
        secret_note = ('The token is full administrative access to your home. Treat it like a '
                       'password: it grants everything you can do in Home Assistant, and more.')
    elif admin is False:
        secret_note = ('<span class="warn">The token is hidden because your Home Assistant account '
                       'is not an administrator.</span> Ask an administrator for it, or read it from '
                       'the add-on log.')
    else:
        secret_note = ('<span class="warn">The token is hidden because it could not be established '
                       'that you are an administrator.</span> It is also printed in the add-on log '
                       'on every start.')

    cli = (f'claude mcp add --transport http home-assistant {url} \\\n'
           f'  --header "Authorization: Bearer {token or "YOUR-TOKEN"}"')
    cfg = json.dumps({"mcpServers": {"home-assistant": {
        "type": "http", "url": url,
        "headers": {"Authorization": f"Bearer {token or 'YOUR-TOKEN'}"}}}}, indent=2)

    rows = "".join(f'<tr><td class="name">{html.escape(name)}</td>'
                   f'<td class="what">{html.escape(spec[0]["description"].split(". ")[0])}</td></tr>'
                   for name, spec in TOOLS.items())

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MCP Server</title><style>{PAGE_CSS}</style></head>
<body class="hide-secrets"><div class="wrap">
<header><h1>MCP Server</h1>
<span class="pill">{'running' if connected else 'no connection to Home Assistant'}</span></header>
<p class="sub">{html.escape(f'Hello {user_name}. ' if user_name else '')}Point an MCP client at the
endpoint below and it can read, diagnose and edit this Home Assistant installation.</p>

<div class="grid">{card_html}</div>

<h2>Connect a client</h2>
<div class="card">
{_field('Endpoint', 'f-url', url)}
{_field('Token', 'f-token', token or 'unavailable', shown, secret=True)}
<p class="note">{secret_note}</p>
<button onclick="toggleSecrets(this)">Show secrets</button>
</div>

{_block('Claude Code', 'b-cli', cli)}
{_block('Any client that takes a JSON configuration', 'b-cfg', cfg)}
<p class="note">A client that speaks only stdio needs a bridge:
<code>npx -y mcp-remote http://{html.escape(HOST)}:{PORT}/sse --header "Authorization: Bearer ..."</code></p>

<h2>{len(TOOLS)} tools</h2>
<div class="card"><table>{rows}</table></div>

<footer>Add-on {VERSION} &middot;
<a href="https://github.com/MatthiasVanDE/hassio-mcp-server" target="_blank" rel="noreferrer">
documentation and source</a> &middot; the endpoint above is not reachable from the internet unless
you deliberately make it so.</footer>
</div><script>{PAGE_JS}</script></body></html>"""


class IngressHandler(BaseHTTPRequestHandler):
    """Everything here is reachable only through the Supervisor: the port is not
    published to the host, and Supervisor only proxies a logged-in session."""

    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        log(f"ingress {self.address_string()} {fmt % args}", "debug")

    def do_GET(self):
        # Ingress rewrites the path, so any path is the page -- except the odds and ends
        # a browser asks for on its own. Rendering the page for a favicon would mean an
        # API call and a user lookup for nothing.
        path = urllib.parse.urlparse(self.path).path
        if os.path.splitext(path)[1] in (".ico", ".png", ".svg", ".css", ".js", ".map"):
            return self.send_error(404)
        body = render_page(
            self.headers.get("X-Remote-User-Display-Name") or self.headers.get("X-Remote-User-Name") or "",
            is_admin(self.headers.get("X-Remote-User-Id", "")),
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        # The page carries a secret; no proxy or browser should keep a copy.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)



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


def resolve_token():
    """The bearer token clients must send: the configured one, or one generated once.

    An empty `token` option used to be fatal, which made the very first start of the
    add-on a crash, and the first impression a red error. Generating one instead
    means the add-on comes up working, and the token is on its own page with a copy
    button and in the log. The generated token is kept in /data so that it survives
    a restart and an update -- a token that changed on every start would silently
    break every client that had been configured with it.
    """
    configured = (OPTIONS.get("token") or "").strip()
    if configured:
        return configured, "configuration"
    try:
        with open(TOKEN_FILE) as f:
            saved = f.read().strip()
        if saved:
            return saved, "generated"
    except FileNotFoundError:
        pass
    except Exception as e:
        log(f"could not read {TOKEN_FILE}: {e}", "warning")
    token = secrets.token_hex(32)
    try:
        with open(TOKEN_FILE, "w") as f:
            f.write(token)
        os.chmod(TOKEN_FILE, 0o600)
    except Exception as e:
        log(f"could not store the generated token in {TOKEN_FILE}: {e} -- a different token "
            f"will be generated on the next start, which breaks configured clients. Set the "
            f"`token` option to a fixed value instead.", "warning")
    return token, "generated"


def main():
    global TZ, TZ_LABEL, HOST, TOKEN, TOKEN_SOURCE
    if not SUPERVISOR:
        log("No SUPERVISOR_TOKEN in the environment -- is this really running as an add-on?", "error")
        sys.exit(1)
    TOKEN, TOKEN_SOURCE = resolve_token()
    if TOKEN_SOURCE == "generated":
        log("-" * 78)
        log("No token was configured, so one was generated for you:")
        log(f"    {TOKEN}")
        log("Clients send it as the header:  Authorization: Bearer <token>")
        log("The add-on's own page (OPEN WEB UI) shows it with a copy button and a")
        log("ready-made client configuration. Set the `token` option to use your own.")
        log("-" * 78)
    ping = core("GET", "/")
    log(f"connection to Home Assistant: HTTP {ping.get('status')} {ping.get('body')}")
    TZ, TZ_LABEL = resolve_timezone()
    log(f"time zone: {TZ_LABEL}")
    HOST = guess_host()
    log(f"clients should connect to http://{HOST}:{PORT}/mcp")
    if READONLY_TOKEN:
        if READONLY_TOKEN == TOKEN:
            log("readonly_token is the same as token, so it grants full access -- choose a different one",
                "warning")
        else:
            log("a read-only token is configured: clients using it can look but not change anything")
    mounted = os.path.isdir(CONFIG_DIR)
    log(f"file access to the configuration directory: {file_mode()}"
        + ("" if mounted else f" (but {CONFIG_DIR} is not mounted)"))
    log(f"{len(TOOLS)} tools available on port {PORT} (/mcp, /sse, /health)")
    srv = Server(("0.0.0.0", PORT), Handler)
    # Ingress is not essential to what the add-on does, so it must never be the reason
    # the add-on fails to start: a Supervisor that does not offer ingress, or a port
    # already taken, costs the page and nothing else.
    ingress = None
    try:
        ingress = Server(("0.0.0.0", INGRESS_PORT), IngressHandler)
        threading.Thread(target=ingress.serve_forever, daemon=True).start()
        log(f"add-on page on ingress port {INGRESS_PORT}")
    except Exception as e:
        log(f"the add-on page could not be started: {type(e).__name__}: {e}", "warning")

    def stop(signum, _frame):
        # Without this the Supervisor waits ten seconds and then kills the container,
        # and open SSE sessions never get a clean shutdown.
        log(f"signal {signum} received, shutting down")
        if ingress:
            threading.Thread(target=ingress.shutdown, daemon=True).start()
        threading.Thread(target=srv.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    srv.serve_forever()
    srv.server_close()
    log("stopped")


if __name__ == "__main__":
    main()
