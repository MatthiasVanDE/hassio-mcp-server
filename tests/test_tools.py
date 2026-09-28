#!/usr/bin/env python3
"""Tests for the tools themselves, with Home Assistant replaced by canned answers.

test_transport.py covers everything up to the point where a tool is called. This
covers what the tools do with what Home Assistant returns: file access and its
limits, backups, the review of a saved automation, traces, dashboard patches,
search, history, logs, the restart guard and the read-only token.

Run with: python tests/test_tools.py
"""
import importlib.util
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, os.pardir, "ha_mcp_server", "server.py")

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


def call(m, name, args, readonly=False):
    resp = m.handle_rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                         "params": {"name": name, "arguments": args}}, readonly)
    result = resp["result"]
    text = result["content"][0].get("text", "")
    try:
        return json.loads(text), result
    except ValueError:
        return text, result


def files(m, tmp):
    print("files")
    cfg = os.path.join(tmp, "config")
    os.makedirs(os.path.join(cfg, ".storage"))
    os.makedirs(os.path.join(cfg, "packages"))
    open(os.path.join(cfg, "configuration.yaml"), "w").write(
        "homeassistant:\n  packages: !include_dir_named packages\ntemplate: !include template.yaml\n")
    open(os.path.join(cfg, "template.yaml"), "w").write("- sensor:\n    - name: Zwembad\n      state: 1\n")
    open(os.path.join(cfg, "secrets.yaml"), "w").write("wifi: hunter2\nblock: |\n  line one\n  line two\n")
    open(os.path.join(cfg, ".storage", "auth"), "w").write('{"refresh_tokens": []}')
    open(os.path.join(cfg, ".storage", "core.entity_registry"), "w").write('{"data": {}}')
    m.CONFIG_DIR = cfg
    m.SHARE = os.path.join(tmp, "share")
    m.BACKUP_DIR = os.path.join(m.SHARE, "backups")

    m.OPTIONS = {"file_access": "off"}
    out, res = call(m, "ha_file", {"action": "read", "path": "template.yaml"})
    check("off means off", "switched off" in out["error"], True)

    m.OPTIONS = {}
    out, _ = call(m, "ha_file", {"action": "read", "path": "/config/template.yaml"})
    check("read-only is the default, and /config/ is understood", "Zwembad" in out["content"], True)
    out, _ = call(m, "ha_file", {"action": "write", "path": "template.yaml", "content": "x: 1\n"})
    check("read-only refuses a write", "read_write" in out["error"], True)
    out, _ = call(m, "ha_file", {"action": "read", "path": "../../etc/passwd"})
    check("a path cannot escape the directory", "outside" in str(out), True)
    os.symlink("/etc", os.path.join(cfg, "escape"))
    out, _ = call(m, "ha_file", {"action": "read", "path": "escape/hosts"})
    check("nor through a symlink", "outside" in str(out), True)

    out, _ = call(m, "ha_file", {"action": "read", "path": "secrets.yaml"})
    check("secrets are masked", "hunter2" not in out["content"] and "line one" not in out["content"], True)
    check("but their keys are shown", "wifi:" in out["content"] and "block:" in out["content"], True)
    out, _ = call(m, "ha_file", {"action": "read", "path": ".storage/auth"})
    check("login data is never read", "credentials" in out["error"], True)
    out, _ = call(m, "ha_file", {"action": "read", "path": ".storage/core.entity_registry"})
    check("the rest of .storage can be read", out["content"], '{"data": {}}')

    out, _ = call(m, "ha_file", {"action": "search", "query": "zwembad"})
    check("search finds the line", [(h["file"], h["line"]) for h in out["hits"]], [("template.yaml", 2)])
    out, _ = call(m, "ha_file", {"action": "search", "query": "hunter2", "pattern": "*"})
    check("search never reveals a secret", out["hits"], [])
    out, _ = call(m, "ha_file", {"action": "list", "path": ""})
    check("list shows the files", {"template.yaml", "packages"} <= {e["path"] for e in out["entries"]}, True)
    out, _ = call(m, "ha_file", {"action": "read", "path": "template.yaml", "start_line": 2, "max_lines": 1})
    check("a line range", (out["content"].strip(), out["lines"]), ("- name: Zwembad", "2-2"))

    m.OPTIONS = {"file_access": "read_write"}
    out, _ = call(m, "ha_file", {"action": "write", "path": "template.yaml", "content": "- sensor: [\n"})
    check("invalid YAML is not written", "not valid YAML" in out["error"], True)
    check("and the file is untouched", "Zwembad" in open(os.path.join(cfg, "template.yaml")).read(), True)
    out, _ = call(m, "ha_file", {"action": "write", "path": "configuration.yaml",
                                 "content": "homeassistant:\n  packages: !include_dir_named packages\n"
                                            "sensor: !include sensors.yaml\n"})
    check("Home Assistant's own tags are valid YAML", out.get("error"), None)
    check("an overwrite leaves a backup", os.path.exists(out["backup"]), True)
    check("and says what to do next", "ha_check_config" in out["next"], True)

    out, _ = call(m, "ha_file", {"action": "edit", "path": "template.yaml",
                                 "old_text": "name: Zwembad", "new_text": "name: Zwembad water"})
    check("edit replaces one exact piece", "Zwembad water" in open(os.path.join(cfg, "template.yaml")).read(),
          True)
    check("and backs up the old version", "name: Zwembad\n" in open(out["backup"]).read(), True)
    out, _ = call(m, "ha_file", {"action": "edit", "path": "template.yaml", "old_text": "e", "new_text": "E"})
    check("an ambiguous edit is refused", "must match exactly once" in out["error"], True)
    out, _ = call(m, "ha_file", {"action": "write", "path": "secrets.yaml", "content": "a: b\n"})
    check("secrets.yaml is never written", "masked" in out["error"], True)
    out, _ = call(m, "ha_file", {"action": "write", "path": ".storage/core.entity_registry", "content": "{}"})
    check(".storage is never written", "Home Assistant owns" in out["error"], True)
    out, _ = call(m, "ha_file", {"action": "write", "path": "packages/pool.yaml", "content": "sensor: []\n"})
    check("a new file is created", out.get("created"), True)
    out, _ = call(m, "ha_file", {"action": "delete", "path": "packages/pool.yaml"})
    check("delete removes it", os.path.exists(os.path.join(cfg, "packages", "pool.yaml")), False)
    check("after backing it up", open(out["backup"]).read(), "sensor: []\n")

    m.BACKUP_KEEP = 3
    for i in range(6):
        m.backup(f"x{i}", "data")
    check("old backups are pruned", len(os.listdir(m.BACKUP_DIR)), 3)


def config_review(m):
    print("saving an automation")
    calls = []
    states = [{"entity_id": "light.keuken", "state": "on", "attributes": {}},
              {"entity_id": "binary_sensor.motion_hal", "state": "off", "attributes": {}}]
    services = [{"domain": "light", "services": {"turn_on": {}, "turn_off": {}}}]

    def core(method, path, body=None, params=None):
        calls.append((method, path))
        if path == "/states":
            return {"status": 200, "body": states}
        if path == "/services":
            return {"status": 200, "body": services}
        if method == "GET" and path.startswith("/config/automation/config/"):
            return {"status": 200, "body": {"alias": "old"}}
        return {"status": 200, "body": {"result": "ok"}}
    m.core = core
    cfg = {"alias": "Hal", "mode": "single",
           "triggers": [{"trigger": "state", "entity_id": "binary_sensor.motion_hal", "to": "on"}],
           "conditions": [{"condition": "template", "value_template": "{{ now().hour > 7 }}"}],
           "actions": [{"action": "light.turn_on", "target": {"entity_id": ["light.keuken", "light.hal"]}},
                       {"delay": "00:02:00"}, {"action": "light.turn_of", "entity_id": "light.keuken"}]}
    out, _ = call(m, "ha_config_save", {"kind": "automation", "object_id": "hal", "config_json": json.dumps(cfg)})
    check("the old version is read and backed up first", calls[0], ("GET", "/config/automation/config/hal"))
    check("the backup holds it", json.load(open(out["backup"])), {"alias": "old"})
    missing = " ".join(out["review"]["missing"])
    check("a missing entity is named", "light.hal" in missing, True)
    check("a misspelt service is named", "light.turn_of" in missing, True)
    check("an existing entity is not", "light.keuken does not" in missing, False)
    advice = " ".join(out["review"]["advice"])
    check("time of day in a template gets advice", "time condition" in advice, True)
    check("a motion automation with a delay in mode single", "mode: restart" in advice, True)

    cfg2 = {"use_blueprint": {"path": "x.yaml", "input": {"light": "light.nope"}}}
    out, _ = call(m, "ha_config_save", {"kind": "automation", "object_id": "bp", "config_json": json.dumps(cfg2)})
    check("a blueprint is not reviewed", "review" in out, False)

    m.core = lambda method, path, *a, **k: {"status": 0, "error": "down"}
    out, _ = call(m, "ha_config_save", {"kind": "automation", "object_id": "hal", "config_json": "{}"})
    check("no backup possible means no save", "could not be read" in out["error"], True)


def traces(m):
    print("traces")
    runs = [{"run_id": "1", "timestamp": {"start": "a"}, "state": "stopped", "trigger": "state of x"},
            {"run_id": "2", "timestamp": {"start": "b"}, "state": "stopped", "error": "boom"}]
    detail = {"run_id": "2", "timestamp": {"start": "b"}, "state": "stopped", "config": {"alias": "A"},
              "trace": {
                  "trigger/0": [{"path": "trigger/0", "changed_variables": {"trigger": {
                      "platform": "state", "entity_id": "switch.x",
                      "from_state": {"state": "off"}, "to_state": {"state": "on"}}}}],
                  "condition/0": [{"path": "condition/0", "result": {"result": False}}],
                  "action/0": [{"path": "action/0", "timestamp": "t1", "result": {"done": True},
                                "changed_variables": {"x": 1, "context": None}}],
                  "action/1": [{"path": "action/1", "timestamp": "t2", "changed_variables": {"x": 1}}]}}
    seen = []

    def ws(cmd, params=None):
        seen.append((cmd, params))
        if cmd == "config/entity_registry/get":
            return {"unique_id": "1700000000"}
        if cmd == "trace/list":
            return runs
        if cmd == "trace/get":
            return detail
    m.ws_cmd = ws
    out, _ = call(m, "ha_traces", {"entity_id": "automation.a"})
    check("traces are looked up by unique id", seen[1], ("trace/list", {"domain": "automation",
                                                                        "item_id": "1700000000"}))
    check("newest first", [r["run_id"] for r in out["runs"]], ["2", "1"])
    out, _ = call(m, "ha_traces", {"entity_id": "automation.a", "run_id": "2"})
    check("the trigger is summarised", out["trigger"], {"platform": "state", "entity_id": "switch.x",
                                                        "from_state": "off", "to_state": "on"})
    check("each condition result is shown", out["conditions"], [{"path": "condition/0", "result": False}])
    check("variables are shown once", ["variables" in x for x in out["actions"]], [True, False])
    out, _ = call(m, "ha_traces", {"entity_id": "light.x"})
    check("only automations and scripts", "automation" in out["error"], True)
    m.ws_cmd = lambda cmd, params=None: [] if cmd == "trace/list" else {}
    m.core = lambda *a, **k: {"status": 200, "body": {"state": "off", "attributes": {}}}
    out, _ = call(m, "ha_traces", {"entity_id": "automation.a"})
    check("no traces says why", "switched off" in out["why"], True)


def dashboards(m):
    print("dashboards")
    stored = {"views": [{"title": "Home", "cards": [{"type": "a"}, {"type": "b"}]}]}
    saved = []

    def ws(cmd, params=None):
        if cmd == "lovelace/config":
            return json.loads(json.dumps(stored))
        if cmd == "lovelace/config/save":
            saved.append(params)
            return None
    m.ws_cmd = ws
    out, _ = call(m, "ha_dashboard", {"action": "get"})
    check("get gives an outline", out["views"][0]["cards"], 2)
    out, _ = call(m, "ha_dashboard", {"action": "get", "path": "/views/0/cards/1"})
    check("or one part", out, {"type": "b"})
    ops = [{"op": "replace", "path": "/views/0/title", "value": "Thuis"},
           {"op": "remove", "path": "/views/0/cards/0"},
           {"op": "add", "path": "/views/0/cards/-", "value": {"type": "c"}}]
    out, _ = call(m, "ha_dashboard", {"action": "patch", "url_path": "dashboard-x", "patch_json": json.dumps(ops)})
    check("a patch saves the result", saved[-1]["config"]["views"][0],
          {"title": "Thuis", "cards": [{"type": "b"}, {"type": "c"}]})
    check("to the right dashboard", saved[-1]["url_path"], "dashboard-x")
    check("after a backup of the old one", json.load(open(out["backup"])), stored)
    before = len(saved)
    out, _ = call(m, "ha_dashboard", {"action": "patch", "patch_json": json.dumps(
        [{"op": "replace", "path": "/views/0/title", "value": "x"}, {"op": "remove", "path": "/views/5"}])})
    check("a failing operation saves nothing", (len(saved), "nothing was saved" in out["error"]), (before, True))


def search(m):
    print("search")
    states = [{"entity_id": "climate.zwembad_warmtepomp", "state": "heat",
               "attributes": {"friendly_name": "Zwembad warmtepomp"}},
              {"entity_id": "sensor.zwembad_temperatuur", "state": "27",
               "attributes": {"friendly_name": "Zwembad temperatuur"}},
              {"entity_id": "light.keuken", "state": "on", "attributes": {"friendly_name": "Keuken"}}]
    m.core = lambda method, path, *a, **k: {"status": 200, "body": states}

    def ws(cmd, params=None):
        if cmd == "config/entity_registry/list":
            return [{"entity_id": "light.keuken", "area_id": "kitchen", "aliases": ["kookeiland"]}]
        if cmd == "config/area_registry/list":
            return [{"area_id": "kitchen", "name": "Keuken"}]
        if cmd == "search/related":
            return {"automation": ["automation.boost"], "area": ["kitchen"]}
        return []
    m.ws_cmd = ws
    out, _ = call(m, "ha_search", {"query": "zwembad warmtepomp"})
    check("the best match comes first", out["entities"][0]["entity_id"], "climate.zwembad_warmtepomp")
    out, _ = call(m, "ha_search", {"query": "zwembd"})
    check("a typo still finds it", "sensor.zwembad_temperatuur" in [e["entity_id"] for e in out["entities"]], True)
    out, _ = call(m, "ha_search", {"query": "kookeiland"})
    check("aliases count", out["entities"][0]["entity_id"], "light.keuken")
    out, _ = call(m, "ha_search", {"query": "light.keuken"})
    check("an exact id says what uses it", out["used_by"], {"automation": ["automation.boost"]})
    out, _ = call(m, "ha_search", {"area": "keuken"})
    check("an area filter", [e["entity_id"] for e in out["entities"]], ["light.keuken"])


def history_and_logs(m):
    print("history, statistics and logs")
    got = {}

    def ws(cmd, params=None):
        got[cmd] = params
        if cmd == "history/history_during_period":
            return {"sensor.t": [{"s": "20", "lu": 1790000000.0}, {"s": "21", "lu": 1790000600.0}]}
        if cmd == "recorder/statistics_during_period":
            return {"sensor.e": [{"start": 1790000000000, "end": 1790086400000, "change": 12.34567}]}
        if cmd == "system_log/list":
            return [{"name": "custom_components.dobiss", "message": ["timeout"], "level": "ERROR",
                     "source": ["x.py", 1], "timestamp": 2, "first_occurred": 1, "count": 7, "exception": ""},
                    {"name": "py.warnings", "message": ["meh"], "level": "WARNING", "source": ["y.py", 2],
                     "timestamp": 3, "first_occurred": 3, "count": 1, "exception": ""}]
    m.ws_cmd = ws
    out, _ = call(m, "ha_history", {"entity_id": "sensor.t", "start": "6h"})
    check("history is compact pairs", [row[1] for row in out["entities"]["sensor.t"]["history"]], ["20", "21"])
    check("only significant changes by default",
          got["history/history_during_period"]["significant_changes_only"], True)
    out, _ = call(m, "ha_statistics", {"statistic_ids": "sensor.e", "period": "day"})
    check("statistics are rounded and timestamped", out["statistics"]["sensor.e"][0]["change"], 12.3457)
    check("the period is passed on", got["recorder/statistics_during_period"]["period"], "day")
    out, _ = call(m, "ha_error_log", {"source": "errors", "level": "error"})
    check("errors are filtered by level", [e["logger"] for e in out["entries"]], ["custom_components.dobiss"])
    check("with their count", out["entries"][0]["count"], 7)

    sent = {}

    def http(base, method, path, *a, **k):
        sent.update(k.get("headers") or {})
        return {"status": 200, "body": "\x1b[32m2026 INFO a\x1b[0m\n2026 ERROR b dobiss\n2026 WARNING c\n"}
    m._http = http
    out, _ = call(m, "ha_error_log", {"source": "core", "lines": 500})
    check("the raw log asks the Supervisor for a range", sent.get("Range"), "entries=:-499:500")
    check("colour codes are stripped", out["body"].startswith("2026 INFO a"), True)
    out, _ = call(m, "ha_error_log", {"source": "core", "level": "WARNING"})
    check("level filters the raw log", out["body"].splitlines(), ["2026 ERROR b dobiss", "2026 WARNING c"])
    out, _ = call(m, "ha_error_log", {"source": "core", "search": "DOBISS"})
    check("search too, case-insensitively", out["body"], "2026 ERROR b dobiss")
    check("and reaches further back", sent.get("Range"), "entries=:-2999:3000")


def restart_guard(m):
    print("restart guard")
    calls = []

    def core(method, path, body=None, params=None):
        calls.append(path)
        if path == "/config/core/check_config":
            return {"status": 200, "body": {"result": "invalid", "errors": "bad"}}
        return {"status": 200, "body": []}
    m.core = core
    out, _ = call(m, "ha_service", {"domain": "homeassistant", "service": "restart"})
    check("an invalid configuration blocks a restart", "restart refused" in out["error"], True)
    check("and the restart is never sent", "/services/homeassistant/restart" in calls, False)
    out, _ = call(m, "ha_service", {"domain": "homeassistant", "service": "restart", "skip_config_check": True})
    check("unless explicitly skipped", "/services/homeassistant/restart" in calls, True)
    out, _ = call(m, "ha_service", {"domain": "light", "service": "turn_on"})
    check("other services are not checked", calls[-1], "/services/light/turn_on")
    m.sup = lambda *a, **k: {"status": 200}
    out, _ = call(m, "ha_supervisor", {"endpoint": "core/restart", "method": "POST"})
    check("a Supervisor restart is guarded too", "restart refused" in out["error"], True)


def readonly(m):
    print("read-only token")
    resp = m.handle_rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, True)
    names = {t["name"] for t in resp["result"]["tools"]}
    check("write-only tools are hidden", {"ha_service", "ha_config_save", "ha_upload"} & names, set())
    check("reading tools are listed", {"ha_overview", "ha_states", "ha_rest"} <= names, True)
    m.core = lambda *a, **k: {"status": 200, "body": "fine"}
    m.ws_cmd = lambda *a, **k: {"ok": True}
    for name, args, allowed in [
            ("ha_service", {"domain": "light", "service": "turn_on"}, False),
            ("ha_rest", {"path": "/states"}, True),
            ("ha_rest", {"path": "/services/light/turn_on", "method": "POST"}, False),
            ("ha_ws", {"command": "config/entity_registry/list"}, True),
            ("ha_ws", {"command": "config/entity_registry/update"}, False),
            ("ha_ws", {"command": "config/auth/list"}, False),
            ("ha_file", {"action": "read", "path": "x"}, True),
            ("ha_file", {"action": "write", "path": "x", "content": ""}, False),
            ("ha_dashboard", {"action": "patch"}, False),
            ("ha_addon_action", {"slug": "x", "action": "stop"}, False)]:
        _, result = call(m, name, args, readonly=True)
        refused = "read-only token" in result["content"][0]["text"]
        check(f"{name} {args.get('method') or args.get('command') or args.get('action') or ''}".strip()
              + (" allowed" if allowed else " refused"), refused, not allowed)
    resp = m.handle_rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}, True)
    check("the instructions say so", "READ-ONLY" in resp["result"]["instructions"], True)


def camera(m):
    print("camera")
    m._http = lambda *a, **k: {"status": 200, "content_type": "image/jpeg", "data": b"\xff\xd8jpeg"}
    _, result = call(m, "ha_camera", {"entity_id": "camera.voordeur"})
    check("an image comes back as an image", result["content"][0]["type"], "image")
    check("base64 encoded", result["content"][0]["data"], "/9hqcGVn")


def catalogue(m):
    print("catalogue")
    for name, (spec, _) in m.TOOLS.items():
        props = spec["inputSchema"]["properties"]
        if not set(spec["inputSchema"]["required"]) <= set(props):
            check(f"{name} declares its required parameters", False, True)
    check("every tool carries annotations", all("annotations" in s for s, _ in m.TOOLS.values()), True)
    check("a delete is marked destructive",
          m.TOOLS["ha_config_delete"][0]["annotations"]["destructiveHint"], True)


def main():
    m = load()
    m.TOKEN = "t"
    with tempfile.TemporaryDirectory() as tmp:
        files(m, tmp)
        config_review(m)
        traces(m)
        dashboards(m)
    fresh = load()
    for test in (search, history_and_logs, restart_guard, readonly, camera, catalogue):
        m = load()
        m.SHARE = fresh.SHARE
        test(m)
    print()
    if failures:
        print(f"{len(failures)} failed: {', '.join(failures)}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
