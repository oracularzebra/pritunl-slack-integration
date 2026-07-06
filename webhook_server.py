import os
import sys
import json
import hmac
import hashlib
import logging
from copy import deepcopy

import requests
from flask import Flask, request, jsonify, abort
from pymongo import MongoClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__)

config = {}
collection = None
_loaded = False
_config_path = None
_hostnames_path = None
_hostnames = []


def _default_hostnames_path():
    if _config_path:
        base = os.path.dirname(os.path.abspath(_config_path))
        return os.path.join(base, "hostnames.json")
    return "hostnames.json"


def _load_hostnames():
    global _hostnames, _hostnames_path
    path = _hostnames_path or _default_hostnames_path()
    if os.path.exists(path):
        with open(path) as f:
            _hostnames = json.load(f)
    else:
        _hostnames = config.get("hostnames", [])
    _hostnames_path = path


def _write_hostnames():
    global _hostnames_path, _hostnames
    path = _hostnames_path or _default_hostnames_path()
    with open(path, "w") as f:
        json.dump(_hostnames, f, indent=2)
    log.info("Hostnames written to %s", path)


def load_config(path):
    global config, collection, _loaded, _config_path
    if _loaded:
        return
    _loaded = True
    _config_path = path
    global config, collection
    with open(path) as f:
        config = json.load(f)

    uri = config.get("mongodb_uri") or os.environ.get(
        "MONGODB_URI", "mongodb://localhost:27017"
    )
    db_name = config.get("mongodb_db", "pritunl")
    client = MongoClient(uri)
    collection = client[db_name]["servers"]

    server = collection.find_one({"name": config["server_name"]})
    if not server:
        log.error("Server '%s' not found", config["server_name"])
        sys.exit(1)

    _load_hostnames()


def _write_config():
    global _config_path
    if not _config_path:
        return
    with open(_config_path, "w") as f:
        json.dump(config, f, indent=2)
    log.info("Config written to %s", _config_path)


def verify_slack_request():
    signing_secret = config.get("slack_signing_secret")
    if not signing_secret:
        return True
    timestamp = request.headers.get("X-Slack-Request-Timestamp")
    signature = request.headers.get("X-Slack-Signature")
    if not timestamp or not signature:
        return False
    sig_basestring = f"v0:{timestamp}:{request.get_data(as_text=True)}"
    expected = "v0=" + hmac.new(
        signing_secret.encode(),
        sig_basestring.encode(),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def get_server():
    server = collection.find_one({"name": config["server_name"]})
    if not server:
        abort(500, description="Server not found")
    return server


def get_server_name():
    return config.get("server_name", "unknown")


# ─── Health Check ────────────────────────────────────────────


@app.route("/")
@app.route("/health")
def health():
    return "ok", 200


# ─── Hostname Management ─────────────────────────────────────


@app.route("/api/hostnames", methods=["GET"])
def list_hostnames():
    return jsonify({"hostnames": _hostnames})


@app.route("/api/hostnames", methods=["POST"])
def add_hostname():
    data = request.get_json(force=True)
    hostname = data.get("hostname")
    if not hostname:
        abort(400, description="'hostname' is required")
    if hostname in _hostnames:
        abort(409, description=f"Hostname {hostname} already tracked")
    _hostnames.append(hostname)
    _write_hostnames()
    log.info("Hostname added: %s", hostname)
    return jsonify({"hostname": hostname, "hostnames": _hostnames}), 201


@app.route("/api/hostnames", methods=["DELETE"])
def delete_hostname():
    data = request.get_json(force=True)
    hostname = data.get("hostname")
    if not hostname:
        abort(400, description="'hostname' is required")
    if hostname not in _hostnames:
        abort(404, description=f"Hostname {hostname} not found")
    _hostnames.remove(hostname)
    _write_hostnames()
    log.info("Hostname deleted: %s", hostname)
    return jsonify({"hostname": hostname, "hostnames": _hostnames})


# ─── REST API ────────────────────────────────────────────────


@app.route("/api/routes", methods=["GET"])
def list_routes():
    server = get_server()
    return jsonify(server.get("routes", []))


@app.route("/api/routes", methods=["POST"])
def add_route():
    data = request.get_json(force=True)
    network = data.get("network")
    if not network:
        abort(400, description="'network' is required")
    route = {
        "network": network,
        "comment": data.get("comment", ""),
        "nat": data.get("nat", True),
    }
    server = get_server()
    routes = server.get("routes", [])
    if any(r["network"] == network for r in routes):
        abort(409, description=f"Route {network} already exists")
    routes.append(route)
    collection.update_one({"name": get_server_name()}, {"$set": {"routes": routes}})
    log.info("Route added: %s", network)
    return jsonify(route), 201


@app.route("/api/routes/<path:network>", methods=["PUT"])
def update_route(network):
    data = request.get_json(force=True)
    server = get_server()
    routes = server.get("routes", [])
    for route in routes:
        if route["network"] == network:
            route["comment"] = data.get("comment", route.get("comment", ""))
            route["nat"] = data.get("nat", route.get("nat", True))
            break
    else:
        abort(404, description=f"Route {network} not found")
    collection.update_one({"name": get_server_name()}, {"$set": {"routes": routes}})
    log.info("Route updated: %s", network)
    return jsonify({"status": "updated"})


@app.route("/api/routes/<path:network>", methods=["DELETE"])
def delete_route(network):
    server = get_server()
    routes = server.get("routes", [])
    new_routes = [r for r in routes if r["network"] != network]
    if len(new_routes) == len(routes):
        abort(404, description=f"Route {network} not found")
    collection.update_one(
        {"name": get_server_name()}, {"$set": {"routes": new_routes}}
    )
    log.info("Route deleted: %s", network)
    return jsonify({"status": "deleted"})


@app.route("/api/restart", methods=["POST"])
def restart_openvpn():
    from update_routes import restart_openvpn as do_restart
    restart_mode = config.get("restart_mode", "openvpn_only")
    restart_cmd = config.get("openvpn_restart_cmd", "sudo systemctl restart pritunl")
    do_restart(restart_mode, restart_cmd)
    return jsonify({"status": "restarted"})


# ─── Slack Slash Command ─────────────────────────────────────


@app.route("/slack/command", methods=["POST"])
def slack_command():
    if not verify_slack_request():
        abort(403)

    cmd_text = request.form.get("text", "").strip()
    parts = cmd_text.split()
    command = parts[0] if parts else "list"

    server = get_server()
    routes = server.get("routes", [])

    if command == "list":
        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Routes for {get_server_name()}* ({len(routes)} total)",
                },
            }
        ]
        for r in routes:
            blocks.append(
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"`{r['network']}`  — {r.get('comment', '')}  (nat={r.get('nat', True)})",
                    },
                }
            )
        return jsonify({"blocks": blocks})

    elif command == "add":
        if len(parts) < 2:
            return jsonify({"text": "Usage: `/routes add <network> [comment]`"})
        network = parts[1]
        comment = " ".join(parts[2:]) if len(parts) > 2 else ""
        if any(r["network"] == network for r in routes):
            return jsonify({"text": f"Route `{network}` already exists."})
        route = {"network": network, "comment": comment, "nat": True}
        routes.append(route)
        collection.update_one(
            {"name": get_server_name()}, {"$set": {"routes": routes}}
        )
        return jsonify({"text": f"Route `{network}` added."})

    elif command == "delete":
        if len(parts) < 2:
            return jsonify({"text": "Usage: `/routes delete <network>`"})
        network = parts[1]
        new_routes = [r for r in routes if r["network"] != network]
        if len(new_routes) == len(routes):
            return jsonify({"text": f"Route `{network}` not found."})
        collection.update_one(
            {"name": get_server_name()}, {"$set": {"routes": new_routes}}
        )
        return jsonify({"text": f"Route `{network}` deleted."})

    elif command == "hostnames":
        lines = [f"*Tracked hostnames ({len(_hostnames)}):*"]
        for h in _hostnames:
            lines.append(f"• `{h}`")
        return jsonify({"text": "\n".join(lines)})

    elif command == "watch":
        if len(parts) < 2:
            return jsonify({"text": "Usage: `/routes watch <hostname>`"})
        hostname = parts[1]
        if hostname in _hostnames:
            return jsonify({"text": f"`{hostname}` is already being watched."})
        _hostnames.append(hostname)
        _write_hostnames()
        return jsonify({"text": f"Now watching `{hostname}`. The poller will track its IPs."})

    elif command == "unwatch":
        if len(parts) < 2:
            return jsonify({"text": "Usage: `/routes unwatch <hostname>`"})
        hostname = parts[1]
        if hostname not in _hostnames:
            return jsonify({"text": f"`{hostname}` is not being watched."})
        _hostnames.remove(hostname)
        _write_hostnames()
        return jsonify({"text": f"Stopped watching `{hostname}`."})

    else:
        return jsonify({
            "text": (
                "Available commands:\n"
                "• `/routes list` — show all routes\n"
                "• `/routes add <network> [comment]` — add a route\n"
                "• `/routes delete <network>` — delete a route\n"
                "• `/routes hostnames` — list tracked DNS hostnames\n"
                "• `/routes watch <hostname>` — start tracking a DNS hostname\n"
                "• `/routes unwatch <hostname>` — stop tracking a DNS hostname"
            )
        })


# ─── Interactive Components (Buttons) ────────────────────────


@app.route("/slack/interactive", methods=["POST"])
def slack_interactive():
    if not verify_slack_request():
        abort(403)

    payload = json.loads(request.form.get("payload", "{}"))
    action = payload.get("actions", [{}])[0]
    action_id = action.get("action_id", "")
    value = action.get("value", "")
    response_url = payload.get("response_url", "")
    user_name = payload.get("user", {}).get("username", "Someone")

    if action_id == "approve_route_update":
        pending_file = value or config.get("pending_file", "/tmp/pending_routes.json")
        if not os.path.exists(pending_file):
            return jsonify({"text": "No pending changes found."})
        with open(pending_file) as f:
            pending = json.load(f)

        collection.update_one(
            {"name": get_server_name()},
            {"$set": {"routes": pending["routes"]}},
        )
        from update_routes import restart_openvpn as do_restart
        restart_mode = config.get("restart_mode", "openvpn_only")
        restart_cmd = config.get("openvpn_restart_cmd", "sudo systemctl restart pritunl")
        do_restart(restart_mode, restart_cmd)
        os.remove(pending_file)

        if response_url:
            try:
                requests.post(response_url, json={
                    "text": None,
                    "replace_original": True,
                    "blocks": [
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": f"✅ *Route changes approved by {user_name} and applied.*",
                            },
                        }
                    ],
                }, timeout=5)
            except Exception:
                pass

        return jsonify({"text": "Route changes approved and applied."})

    elif action_id == "reject_route_update":
        pending_file = value or config.get("pending_file", "/tmp/pending_routes.json")
        if os.path.exists(pending_file):
            os.remove(pending_file)

        if response_url:
            try:
                requests.post(response_url, json={
                    "text": None,
                    "replace_original": True,
                    "blocks": [
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": f"❌ *Route changes rejected by {user_name}.*",
                            },
                        }
                    ],
                }, timeout=5)
            except Exception:
                pass

        return jsonify({"text": "Route changes rejected."})

    return jsonify({"text": "Unknown action."})


# Auto-load config on import (for gunicorn)
_config_path = os.environ.get("CONFIG_PATH")
if _config_path and os.path.exists(_config_path):
    load_config(_config_path)

if __name__ == "__main__":
    config_path = os.environ.get("CONFIG_PATH") or (
        sys.argv[1] if len(sys.argv) >= 2 else None
    )
    if not config_path:
        print("Usage: python webhook_server.py <config.json>")
        print("   Or: CONFIG_PATH=<config.json> python webhook_server.py")
        sys.exit(1)
    load_config(config_path)
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
