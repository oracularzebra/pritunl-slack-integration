import os
import sys
import json
import time
import socket
import logging
import argparse
import subprocess
from copy import deepcopy

import requests
from pymongo import MongoClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


def resolve_ips(hostname):
    _, _, ips = socket.gethostbyname_ex(hostname)
    ips.sort()
    return ips


def get_mongo_collection(config):
    uri = config.get("mongodb_uri") or os.environ.get(
        "MONGODB_URI",
        os.environ.get("PRITUNL_MONGODB_URI", "mongodb://localhost:27017"),
    )
    db_name = config.get("mongodb_db") or os.environ.get(
        "MONGODB_DB", os.environ.get("PRITUNL_DB", "pritunl")
    )
    client = MongoClient(uri)
    return client[db_name]["servers"]


def get_pritunl_pids():
    result = subprocess.run(
        ["pgrep", "-f", "/usr/lib/pritunl.*pritunl start"],
        capture_output=True, text=True, timeout=10,
    )
    return [int(p) for p in result.stdout.strip().split() if p]


def get_openvpn_child_pids(parent_pid):
    result = subprocess.run(
        ["pgrep", "-P", str(parent_pid)],
        capture_output=True, text=True, timeout=10,
    )
    children = [int(p) for p in result.stdout.strip().split() if p]
    openvpn_pids = []
    for pid in children:
        try:
            cmdline = open(f"/proc/{pid}/cmdline", "rb").read().decode().replace("\0", " ")
            if "openvpn" in cmdline:
                openvpn_pids.append(pid)
        except (FileNotFoundError, ProcessLookupError):
            pass
    return openvpn_pids


def restart_openvpn(mode, restart_cmd):
    if mode == "full":
        log.info("Full Pritunl restart: %s", restart_cmd)
        ret = os.system(restart_cmd)
        if ret != 0:
            log.warning("Restart command returned exit code %d", ret)
        return

    log.info("Killing OpenVPN child processes (Pritunl should respawn)...")
    pids = get_pritunl_pids()
    killed = []
    for ppid in pids:
        for ovpn_pid in get_openvpn_child_pids(ppid):
            log.info("  Killing OpenVPN PID %d", ovpn_pid)
            try:
                os.kill(ovpn_pid, 15)
                killed.append(ovpn_pid)
            except ProcessLookupError:
                pass

    time.sleep(3)

    alive = []
    for ppid in pids:
        alive.extend(get_openvpn_child_pids(ppid))
    if not alive:
        log.warning("OpenVPN did not respawn. Falling back to full Pritunl restart.")
        os.system(restart_cmd)
    else:
        log.info("OpenVPN respawned (PIDs: %s)", alive)


def send_slack_interactive(webhook_url, changes, server_name, pending_file):
    lines = [f"*Pending Route Changes — {server_name}*"]
    for c in changes:
        hostname = c["hostname"]
        old = ", ".join(c["old_ips"]) if c["old_ips"] else "(none)"
        new = ", ".join(c["new_ips"])
        lines.append(f"• `{hostname}`\n  Old: `{old}`\n  New: `{new}`")
    lines.append("\n_Review the changes above and approve or reject._")
    payload = {
        "text": f"Pending route changes for {server_name}",
        "blocks": [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "\n\n".join(lines)},
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Approve"},
                        "style": "primary",
                        "action_id": "approve_route_update",
                        "value": pending_file,
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Reject"},
                        "style": "danger",
                        "action_id": "reject_route_update",
                        "value": pending_file,
                    },
                ],
            },
        ],
    }
    resp = requests.post(webhook_url, json=payload, timeout=10)
    resp.raise_for_status()


def load_config(path):
    with open(path) as f:
        cfg = json.load(f)
    if "server_name" not in cfg:
        log.error("Missing 'server_name' in config")
        sys.exit(1)
    return cfg


def load_hostnames(config, args):
    hostnames_path = args.hostnames or os.environ.get("HOSTNAMES_PATH", "")
    if not hostnames_path:
        base = os.path.dirname(os.path.abspath(args.config))
        hostnames_path = os.path.join(base, "hostnames.json")
    if os.path.exists(hostnames_path):
        with open(hostnames_path) as f:
            return json.load(f)
    return config.get("hostnames", [])


def main():
    parser = argparse.ArgumentParser(
        description="Poll DNS hostnames, update Pritunl routes via config file."
    )
    parser.add_argument(
        "--config",
        required=True,
        help="Path to JSON config file",
    )
    parser.add_argument(
        "--hostnames",
        default="",
        help="Path to hostnames JSON file (default: hostnames.json next to config)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Apply routes even if no change detected",
    )
    args = parser.parse_args()

    config = load_config(args.config)
    server_name = config["server_name"]
    hostnames = load_hostnames(config, args)
    slack_webhook = config.get("slack_webhook") or os.environ.get("SLACK_WEBHOOK_URL", "")
    pending_file = config.get("pending_file", "/tmp/pending_routes.json")
    rejected_file = pending_file.replace("pending_", "rejected_")
    restart_mode = config.get("restart_mode", "openvpn_only")
    restart_cmd = config.get(
        "openvpn_restart_cmd",
        os.environ.get("OPENVPN_RESTART_CMD", "sudo systemctl restart pritunl"),
    )
    default_nat = config.get("nat", True)

    collection = get_mongo_collection(config)
    server_doc = collection.find_one({"name": server_name})
    if not server_doc:
        log.error("Server '%s' not found", server_name)
        sys.exit(1)

    current_routes = server_doc.get("routes", [])

    any_change = False
    changes = []
    new_routes_all = deepcopy(current_routes)

    for hostname in hostnames:
        comment_tag = f"dns:{hostname}"

        log.info("Resolving: %s", hostname)
        resolved_ips = resolve_ips(hostname)
        log.info("  IPs: %s", resolved_ips)

        if not resolved_ips:
            log.warning("  No IPs resolved, skipping")
            continue

        new_routes_for_host = [
            {"network": f"{ip}/32", "comment": comment_tag, "nat": default_nat}
            for ip in resolved_ips
        ]

        old_routes_for_host = [
            r for r in new_routes_all
            if r.get("comment") == comment_tag
        ]
        old_ips = sorted(r["network"] for r in old_routes_for_host)
        new_ips = sorted(r["network"] for r in new_routes_for_host)

        if old_ips == new_ips and not args.force:
            log.info("  No change")
            continue

        log.info("  Change detected!")
        log.info("    Old: %s", old_ips)
        log.info("    New: %s", new_ips)

        new_routes_all = [r for r in new_routes_all if r.get("comment") != comment_tag]
        new_routes_all.extend(new_routes_for_host)

        any_change = True
        changes.append({
            "hostname": hostname,
            "old_ips": [ip.split("/")[0] for ip in old_ips],
            "new_ips": resolved_ips,
        })

    tracked_tags = {f"dns:{h}" for h in hostnames}
    orphaned = [
        r for r in new_routes_all
        if (c := r.get("comment", "")).startswith("dns:") and c not in tracked_tags
    ]
    if orphaned:
        orphaned_hostnames = {}
        for r in orphaned:
            tag = r.get("comment", "")
            hostname = tag[4:] if tag.startswith("dns:") else tag
            orphaned_hostnames.setdefault(hostname, []).append(r["network"])
        for h, nets in orphaned_hostnames.items():
            log.info("  Orphaned: %s (%d routes)", h, len(nets))
            changes.append({
                "hostname": h,
                "old_ips": [n.split("/")[0] for n in nets],
                "new_ips": [],
            })
        new_routes_all = [r for r in new_routes_all if r not in orphaned]
        any_change = True

    if not any_change:
        log.info("No changes detected for any hostname")
        return

    # Check if same changes were already rejected — don't re-notify until DNS changes again
    if os.path.exists(rejected_file):
        try:
            with open(rejected_file) as f:
                rejected = json.load(f)
            if rejected.get("routes") == new_routes_all:
                log.info("Changes match previously rejected state — skipping notification")
                return
        except (json.JSONDecodeError, IOError):
            log.warning("Could not read rejected state file")

    # Check if same changes are already pending — skip duplicate notifications
    if os.path.exists(pending_file):
        try:
            with open(pending_file) as f:
                existing = json.load(f)
            if existing.get("routes") == new_routes_all:
                log.info("Changes already pending in %s — skipping duplicate notification", pending_file)
                return
            log.info("Pending file exists but routes differ — updating")
        except (json.JSONDecodeError, IOError):
            log.warning("Could not read existing pending file — will overwrite")

    pending = {
        "routes": new_routes_all,
        "changes": changes,
        "server_name": server_name,
    }
    with open(pending_file, "w") as f:
        json.dump(pending, f, indent=2)
    log.info("Pending changes saved to %s", pending_file)

    if slack_webhook:
        try:
            send_slack_interactive(slack_webhook, changes, server_name, pending_file)
            log.info("Interactive Slack notification sent — awaiting approval")
        except Exception as e:
            log.error("Failed to send Slack notification: %s", e)
    else:
        log.info("No Slack webhook configured, changes remain pending in %s", pending_file)

    for c in changes:
        log.info("Pending: %s: %s -> %s", c["hostname"], c["old_ips"] or "(none)", c["new_ips"])
    log.info("Waiting for approval via Slack interactive buttons...")


if __name__ == "__main__":
    main()
