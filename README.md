# Pritunl ALB Route Updater

Automatically update Pritunl VPN route entries when an ALB's IP addresses change. Resolves the ALB DNS name, replaces all existing `/32` routes on a Pritunl server with the resolved IPs, restarts OpenVPN, and sends a Slack notification.

## Problem

AWS Application Load Balancers (ALBs) can change their underlying IP addresses over time (e.g., after scale events, AZ failures, or recreation). If your Pritunl VPN routes point to ALB IPs as `/32` entries, they will break when the IPs change. This script polls the ALB DNS name, detects changes, and updates Pritunl automatically.

## How It Works

1. Resolves the ALB DNS name to its current A records
2. Reads current `/32` routes from the Pritunl server's MongoDB document
3. Compares old vs new IPs — exits early if unchanged
4. Replaces **all** routes on the server with the new IPs as `/32` entries
5. Restarts OpenVPN to apply the changes
6. Sends a Slack notification with old and new IPs

## Requirements

- Python 3.8+
- Access to the Pritunl MongoDB instance (default: `mongodb://localhost:27017/pritunl`)
- `sudo` access to restart OpenVPN (or a custom restart command)
- (Optional) Slack incoming webhook URL

## Installation

```bash
# Clone or copy the script to your Pritunl server
git clone <repo> /opt/pritunl-alb-updater
cd /opt/pritunl-alb-updater

# Install dependencies
pip install -r requirements.txt
```

## Usage

```bash
python update_alb_routes.py \
  --alb-dns my-alb-123456.elb.amazonaws.com \
  --server-name CloudKeeper \
  --slack-webhook https://hooks.slack.com/services/T00/B00/xxx
```

### Arguments

| Argument | Description | Default |
|---|---|---|
| `--alb-dns` | (Required) ALB DNS name to resolve | — |
| `--server-name` | (Required) Pritunl server name as stored in MongoDB | — |
| `--slack-webhook` | Slack incoming webhook URL | `$SLACK_WEBHOOK_URL` or none |
| `--openvpn-restart-cmd` | Command to restart OpenVPN | `sudo systemctl restart openvpn@*` |
| `--force` | Apply routes even if IPs haven't changed | off |

### Environment Variables

| Variable | Description | Default |
|---|---|---|
| `MONGODB_URI` | MongoDB connection string | `mongodb://localhost:27017` |
| `MONGODB_DB` | Database name | `pritunl` |
| `SLACK_WEBHOOK_URL` | Slack webhook URL (alternative to `--slack-webhook`) | — |

## Scheduling with Cron

Run every 5 minutes. Only restarts OpenVPN and sends Slack when a change is detected.

```bash
crontab -e
```

```
*/5 * * * * /usr/bin/python3 /opt/pritunl-alb-updater/update_alb_routes.py \
  --alb-dns my-alb-123456.elb.amazonaws.com \
  --server-name CloudKeeper \
  --slack-webhook https://hooks.slack.com/services/T00/B00/xxx \
  >> /var/log/alb-route-updater.log 2>&1
```

### Cron with environment variables (avoids exposing tokens in the command line)

```
*/5 * * * * SLACK_WEBHOOK_URL=https://hooks.slack.com/services/T00/B00/xxx \
  /usr/bin/python3 /opt/pritunl-alb-updater/update_alb_routes.py \
  --alb-dns my-alb-123456.elb.amazonaws.com \
  --server-name CloudKeeper \
  >> /var/log/alb-route-updater.log 2>&1
```

## MongoDB Configuration

The script connects to the `pritunl` database and reads/updates the `servers` collection. The server document is matched by the `name` field.

If your MongoDB requires authentication:

```bash
export MONGODB_URI="mongodb://user:pass@localhost:27017/pritunl"
```

## Log Output

```
2026-07-04 12:00:01 INFO Resolving ALB DNS: my-alb-123456.elb.amazonaws.com
2026-07-04 12:00:01 INFO Resolved IPs: ['10.0.1.50', '10.0.1.51']
2026-07-04 12:00:01 INFO Change detected!
2026-07-04 12:00:01 INFO   Old IPs: ['10.0.1.10/32', '10.0.1.11/32']
2026-07-04 12:00:01 INFO   New IPs: ['10.0.1.50/32', '10.0.1.51/32']
2026-07-04 12:00:01 INFO Routes updated in MongoDB
2026-07-04 12:00:01 INFO Restarting OpenVPN...
2026-07-04 12:00:03 INFO OpenVPN restarted
2026-07-04 12:00:04 INFO Slack notification sent
```

## Validating Changes

After the script runs, verify the routes on the Pritunl EC2 server:

```bash
# Check iptables NAT rules
sudo iptables -t nat -L POSTROUTING -n -v | grep <new_ip>

# Check routes in MongoDB
mongosh pritunl --eval 'db.servers.findOne({name:"CloudKeeper"}, {routes:1}).pretty()'
```

From a connected VPN client:

```bash
# Verify traffic routes through the tunnel
ip route get <new_ip>
traceroute <new_ip>
```
