import os
import sys
import json
import socket
import logging
import argparse
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
    required = ["server_name", "hostnames"]
    for key in required:
        if key not in cfg:
            log.error("Missing required config key: %s", key)
            sys.exit(1)
    return cfg


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
        "--force",
        action="store_true",
        help="Apply routes even if no change detected",
    )
    args = parser.parse_args()

    config = load_config(args.config)
    server_name = config["server_name"]
    hostnames = config["hostnames"]
    slack_webhook = config.get("slack_webhook") or os.environ.get("SLACK_WEBHOOK_URL", "")
    restart_cmd = config.get(
        "openvpn_restart_cmd", "sudo systemctl restart openvpn@*"
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

    log.info("Restarting OpenVPN...")
    ret = os.system(restart_cmd)
    if ret != 0:
        log.warning("OpenVPN restart returned exit code %d", ret)
    else:
        log.info("OpenVPN restarted")

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
