#!/usr/bin/env python3
"""Transport and protocol tests for the MCP server.

These run without Home Assistant. They exercise everything between an MCP client and
the point where a tool would call the core API: authentication, both transports,
JSON-RPC framing, error handling and the tool catalogue. Anything that does reach Home
Assistant is expected to fail with a connection error, which is itself correct
behaviour and is asserted as such.

Run with: python tests/test_transport.py
"""
import http.client
import importlib.util
import json
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, os.pardir, "ha_mcp_server", "server.py")
# A non-ASCII token on purpose: compare_digest on str raises on anything but ASCII.
TOKEN = "test-tökén-9f2a"
PORT = 18099
IPORT = 18098
BASE = f"http://127.0.0.1:{PORT}"
IBASE = f"http://127.0.0.1:{IPORT}"

failures = []


def check(name, got, want):
    if got == want:
        print(f"  ok    {name}")
    else:
        print(f"  FAIL  {name}: got {got!r}, want {want!r}")
        failures.append(name)


def load():
    spec = importlib.util.spec_from_file_location("srv", SERVER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def post(path, body=None, token=None):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(BASE + path, data=data, headers=headers,
                                 method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def main():
    m = load()
    m.TOKEN = TOKEN
    srv = m.Server(("127.0.0.1", PORT), m.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.3)

    print("health endpoint")
    status, body = post("/health")
    check("unauthenticated 200", status, 200)
    check("reports ok", json.loads(body)["status"], "ok")
    check("reports the tool count", json.loads(body)["tools"], len(m.TOOLS))

    print("authentication")
    ping = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    check("no token rejected", post("/mcp", ping)[0], 401)
    check("wrong token rejected", post("/mcp", ping, "wrong")[0], 401)
    check("non-ascii token accepted", post("/mcp", ping, TOKEN)[0], 200)
    check("sse needs a token", post("/sse")[0], 401)

    print("json-rpc framing")
    status, body = post("/mcp", {"jsonrpc": "2.0", "id": 2, "method": "initialize",
                                 "params": {"protocolVersion": "2024-11-05"}}, TOKEN)
    check("initialize succeeds", status, 200)
    check("advertises tools", "tools" in json.loads(body)["result"]["capabilities"], True)
    check("reports its version", json.loads(body)["result"]["serverInfo"]["version"], m.VERSION)

    status, body = post("/mcp", {"jsonrpc": "2.0", "id": 3, "method": "tools/list"}, TOKEN)
    tools = json.loads(body)["result"]["tools"]
    check("tools/list returns every tool", len(tools), len(m.TOOLS))
    check("every tool is described", all(t.get("description") for t in tools), True)
    check("every tool has a schema", all(t["inputSchema"]["type"] == "object" for t in tools), True)
    check("required params are declared", all(
        set(t["inputSchema"].get("required", [])) <= set(t["inputSchema"]["properties"])
        for t in tools), True)

    check("notifications get 202",
          post("/mcp", {"jsonrpc": "2.0", "method": "notifications/initialized"}, TOKEN)[0], 202)

    status, body = post("/mcp", [{"jsonrpc": "2.0", "id": 4, "method": "ping"},
                                 {"jsonrpc": "2.0", "id": 5, "method": "ping"}], TOKEN)
    check("batches are answered", [r["id"] for r in json.loads(body)], [4, 5])
    check("an all-notification batch gets 202",
          post("/mcp", [{"jsonrpc": "2.0", "method": "notifications/initialized"}], TOKEN)[0], 202)

    print("error handling")
    _, body = post("/mcp", "not-an-object", TOKEN)
    check("a non-object body is an invalid request", json.loads(body)["error"]["code"], -32600)
    _, body = post("/mcp", {"jsonrpc": "2.0", "id": 6, "method": "resources/list"}, TOKEN)
    check("an unsupported method is reported", json.loads(body)["error"]["code"], -32601)
    _, body = post("/mcp", {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                            "params": {"name": "nope", "arguments": {}}}, TOKEN)
    check("an unknown tool is reported", json.loads(body)["error"]["code"], -32601)
    check("an unknown path is 404", post("/nope", {"a": 1}, TOKEN)[0], 404)

    # No Supervisor here, so the call cannot reach Home Assistant. It must come back as
    # a readable error rather than an exception escaping the handler.
    _, body = post("/mcp", {"jsonrpc": "2.0", "id": 8, "method": "tools/call",
                            "params": {"name": "ha_states", "arguments": {}}}, TOKEN)
    result = json.loads(body)["result"]
    check("an unreachable core is an isError result", result["isError"], True)
    check("and still returns text content", result["content"][0]["type"], "text")

    print("keep-alive")
    conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=10)
    conn.request("POST", "/mcp", json.dumps(ping), {"Content-Type": "application/json"})
    first = conn.getresponse()
    first.read()
    conn.request("POST", "/mcp", json.dumps({"jsonrpc": "2.0", "id": 9, "method": "ping"}),
                 {"Content-Type": "application/json", "Authorization": "Bearer " + TOKEN})
    second = conn.getresponse()
    reused = json.loads(second.read().decode())
    check("a rejected request does not desync the connection", (first.status, second.status), (401, 200))
    check("and the next request is answered", reused["id"], 9)
    conn.close()

    print("oversized bodies")
    conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=10)
    conn.putrequest("POST", "/mcp")
    conn.putheader("Content-Type", "application/json")
    conn.putheader("Content-Length", str(m.MAX_BODY + 1))
    conn.endheaders()
    check("are refused with 413", conn.getresponse().status, 413)
    conn.close()

    print("sse transport")
    conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=10)
    conn.request("GET", "/sse", headers={"Authorization": "Bearer " + TOKEN})
    resp = conn.getresponse()
    check("stream opens", resp.status, 200)
    check("as an event stream", resp.headers.get("Content-Type"), "text/event-stream")
    announced = resp.readline().decode() + resp.readline().decode()
    check("and announces its message endpoint", "event: endpoint" in announced
          and "/messages?session_id=" in announced, True)
    session_id = announced.split("session_id=")[1].strip()

    status, _ = post(f"/messages?session_id={session_id}", {"jsonrpc": "2.0", "id": 10, "method": "ping"}, TOKEN)
    check("a message for a live session is accepted", status, 202)
    check("a message for an unknown session is not",
          post("/messages?session_id=deadbeef", ping, TOKEN)[0], 404)
    conn.close()

    print("token resolution")
    with tempfile.TemporaryDirectory() as tmp:
        m.TOKEN_FILE = os.path.join(tmp, "token")
        m.OPTIONS = {}
        first, source = m.resolve_token()
        check("a token is generated when none is configured", len(first), 64)
        check("and reported as generated", source, "generated")
        check("it is persisted", open(m.TOKEN_FILE).read().strip(), first)
        # A token that changed on every start would break every configured client.
        check("a restart reuses it", m.resolve_token()[0], first)
        m.OPTIONS = {"token": "  chosen-by-hand  "}
        check("a configured token wins", m.resolve_token(), ("chosen-by-hand", "configuration"))
    m.TOKEN = TOKEN

    print("the add-on page")
    # None of this may depend on Home Assistant being reachable.
    m.HOST = "192.168.0.16"
    m.TZ_LABEL = "Europe/Brussels (from Home Assistant)"
    m.core = lambda *a, **k: {"status": 200, "body": {"message": "API running."}}
    m.ws_cmd = lambda *a, **k: [{"id": "admin-1", "group_ids": ["system-admin"]},
                                {"id": "guest-1", "group_ids": ["system-users"]}]
    ing = m.Server(("127.0.0.1", IPORT), m.IngressHandler)
    threading.Thread(target=ing.serve_forever, daemon=True).start()
    time.sleep(0.3)

    def page(user_id):
        req = urllib.request.Request(IBASE + "/", headers={"X-Remote-User-Id": user_id,
                                                           "X-Remote-User-Name": "tester"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read().decode(), dict(r.headers)

    status, body, headers = page("admin-1")
    check("ingress needs no bearer token", status, 200)
    check("an administrator sees the token", TOKEN in body, True)
    check("the endpoint is spelled out", f"http://192.168.0.16:{m.PORT}/mcp" in body, True)
    check("every tool is listed", all(name in body for name in m.TOOLS), True)
    check("secrets start hidden", 'class="hide-secrets"' in body, True)
    check("the page is not cached", headers.get("Cache-Control"), "no-store")

    status, body, _ = page("guest-1")
    check("a non-administrator does not see the token", TOKEN in body, False)
    check("and is told why", "not an administrator" in body, True)

    m.ws_cmd = lambda *a, **k: {"error": "no websocket here"}
    status, body, _ = page("admin-1")
    check("an unverifiable user does not see the token", TOKEN in body, False)
    check("the page still renders", status, 200)
    check("no user id at all is not an administrator", m.is_admin(""), False)
    ing.shutdown()

    print("helpers")
    check("short text is left alone", m.clip("short"), "short")
    check("long text is truncated", len(m.clip("x" * (m.MAX_CHARS + 50))) > m.MAX_CHARS, True)
    check("ago() returns an iso timestamp", len(m.ago(24)), 19)

    srv.shutdown()
    print()
    if failures:
        print(f"{len(failures)} failed: {', '.join(failures)}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
