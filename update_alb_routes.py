import os
import sys
import json
import socket
import logging
import argparse
from datetime import datetime, timezone

import requests
from pymongo import MongoClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


def resolve_alb_ips(alb_dns_name):
    _, _, ips = socket.gethostbyname_ex(alb_dns_name)
    ips.sort()
    return ips


def get_mongo_collection():
    uri = os.environ.get(
        "MONGODB_URI",
        os.environ.get("PRITUNL_MONGODB_URI", "mongodb://localhost:27017"),
    )
    db_name = os.environ.get("MONGODB_DB", os.environ.get("PRITUNL_DB", "pritunl"))
    client = MongoClient(uri)
    return client[db_name]["servers"]


def send_slack_notification(webhook_url, old_ips, new_ips, server_name):
    old_str = ", ".join(old_ips) if old_ips else "(none)"
    new_str = ", ".join(new_ips)
    payload = {
        "text": (
            f"*Pritunl Routes Updated*\n"
            f"Server: `{server_name}`\n"
            f"Old IPs: `{old_str}`\n"
            f"New IPs: `{new_str}`"
        ),
    }
    resp = requests.post(webhook_url, json=payload, timeout=10)
    resp.raise_for_status()


def main():
    parser = argparse.ArgumentParser(
        description="Poll ALB DNS, update Pritunl routes, notify Slack."
    )
    parser.add_argument(
        "--alb-dns",
        required=True,
        help="ALB DNS name to resolve (e.g. my-alb-123.elb.amazonaws.com)",
    )
    parser.add_argument(
        "--server-name",
        required=True,
        help="Pritunl server name (e.g. CloudKeeper)",
    )
    parser.add_argument(
        "--slack-webhook",
        default=os.environ.get("SLACK_WEBHOOK_URL", ""),
        help="Slack incoming webhook URL",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Apply routes even if no change detected",
    )
    parser.add_argument(
        "--openvpn-restart-cmd",
        default="sudo systemctl restart openvpn@*",
        help="Command to restart OpenVPN after route update",
    )
    args = parser.parse_args()

    alb_dns = args.alb_dns
    server_name = args.server_name
    slack_webhook = args.slack_webhook

    # Resolve current ALB IPs
    log.info("Resolving ALB DNS: %s", alb_dns)
    resolved_ips = resolve_alb_ips(alb_dns)
    log.info("Resolved IPs: %s", resolved_ips)
    if not resolved_ips:
        log.error("No IPs resolved for %s", alb_dns)
        sys.exit(1)

    new_routes = [
        {"network": f"{ip}/32", "comment": f"ALB {alb_dns}", "metric": 100}
        for ip in resolved_ips
    ]

    # Get current routes from MongoDB
    collection = get_mongo_collection()
    server_doc = collection.find_one({"name": server_name})
    if not server_doc:
        log.error("Server '%s' not found in MongoDB", server_name)
        sys.exit(1)

    current_routes = server_doc.get("routes", [])
    current_ip_routes = sorted(
        r.get("network", "") for r in current_routes if r.get("network", "").endswith("/32")
    )

    new_ip_routes = sorted(r["network"] for r in new_routes)

    if current_ip_routes == new_ip_routes and not args.force:
        log.info("No change detected. Current IPs match resolved IPs.")
        sys.exit(0)

    log.info("Change detected!")
    log.info("  Old IPs: %s", current_ip_routes)
    log.info("  New IPs: %s", new_ip_routes)

    # Update routes in MongoDB
    collection.update_one(
        {"name": server_name},
        {"$set": {"routes": new_routes}},
    )
    log.info("Routes updated in MongoDB")

    # Restart OpenVPN
    log.info("Restarting OpenVPN...")
    ret = os.system(args.openvpn_restart_cmd)
    if ret != 0:
        log.warning("OpenVPN restart command returned exit code %d", ret)
    else:
        log.info("OpenVPN restarted")

    # Send Slack notification
    if slack_webhook:
        try:
            send_slack_notification(
                slack_webhook,
                [r.split("/")[0] for r in current_ip_routes],
                resolved_ips,
                server_name,
            )
            log.info("Slack notification sent")
        except Exception as e:
            log.error("Failed to send Slack notification: %s", e)


if __name__ == "__main__":
    main()
