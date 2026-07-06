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


def send_slack_notification(webhook_url, changes, server_name):
    lines = [f"*Pritunl Routes Updated — {server_name}*"]
    for c in changes:
        hostname = c["hostname"]
        old = ", ".join(c["old_ips"]) if c["old_ips"] else "(none)"
        new = ", ".join(c["new_ips"])
        lines.append(f"• `{hostname}`\n  Old: `{old}`\n  New: `{new}`")
    payload = {"text": "\n\n".join(lines)}
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
        r for r in current_routes
        if (c := r.get("comment", "")).startswith("dns:") and c not in tracked_tags
    ]
    if orphaned:
        log.warning(
            "Orphaned routes found (hostnames not in config, not removed):"
        )
        for r in orphaned:
            tag = r.get("comment", "")
            hostname = tag[4:] if tag.startswith("dns:") else tag
            log.warning("  %s (comment: %s)", r.get("network"), tag)

    if not any_change:
        log.info("No changes detected for any hostname")
        return

    log.info("Updating routes in MongoDB...")
    collection.update_one(
        {"name": server_name},
        {"$set": {"routes": new_routes_all}},
    )
    log.info("Routes updated")

    restart_openvpn(restart_mode, restart_cmd)

    if slack_webhook:
        try:
            send_slack_notification(slack_webhook, changes, server_name)
            log.info("Slack notification sent")
        except Exception as e:
            log.error("Failed to send Slack notification: %s", e)

    for c in changes:
        log.info("Updated %s: %s -> %s", c["hostname"], c["old_ips"] or "(none)", c["new_ips"])


if __name__ == "__main__":
    main()
