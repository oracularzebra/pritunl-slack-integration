# Pritunl Route Updater

Polls DNS hostnames (ALBs, NLBs, etc.) for IP changes, updates Pritunl VPN route entries in MongoDB, and restarts OpenVPN.

## How It Works

1. Reads tracked hostnames from `hostnames.json`
2. For each hostname, resolves DNS to current IPs
3. Compares with existing routes in MongoDB (matched by `comment: "dns:<hostname>"`)
4. If any hostname's IPs changed, saves pending routes to a file and sends an interactive Slack message with **Approve** / **Reject** buttons
5. Clicking **Approve** applies the new routes to MongoDB, restarts OpenVPN, and removes the pending file
6. Only routes matching tracked hostnames are touched — other routes are left intact

## Requirements

- Python 3.8+
- Access to the Pritunl MongoDB instance
- `sudo` access to restart OpenVPN
- (Optional) Slack incoming webhook URL

## Installation

```bash
pip install pymongo requests flask gunicorn
```

## Configuration

### config.json

Connection and behaviour settings:

```json
{
  "server_name": "CloudKeeper",
  "slack_webhook": "https://hooks.slack.com/services/T00/B00/xxx",
  "slack_signing_secret": "your_slack_signing_secret",
  "openvpn_restart_cmd": "sudo systemctl restart pritunl",
  "restart_mode": "openvpn_only",
  "nat": true,
  "mongodb_uri": "mongodb://localhost:27017",
  "mongodb_db": "pritunl",
  "pending_file": "/tmp/pending_routes.json",
  "port": 5000
}
```

| Key | Required | Description |
|---|---|---|
| `server_name` | Yes | Pritunl server name (matches `name` in MongoDB `servers`) |
| `slack_webhook` | No | Slack incoming webhook URL (or `SLACK_WEBHOOK_URL` env var) |
| `slack_signing_secret` | No | Slack app signing secret (verifies requests) |
| `restart_mode` | No | `"openvpn_only"` (kill child, Pritunl respawns) or `"full"` (systemctl restart) |
| `openvpn_restart_cmd` | No | Full restart fallback command |
| `nat` | No | Enable NAT on routes (default: `true`) |
| `mongodb_uri` | No | Default: `mongodb://localhost:27017` (or `MONGODB_URI` env var) |
| `mongodb_db` | No | Default: `pritunl` |
| `pending_file` | No | Path for pending route changes (default: `/tmp/pending_routes.json`) |
| `port` | No | Flask listen port (default: `5000`) |

### hostnames.json

List of DNS hostnames to track — stored in a **separate file** so it can be managed independently:

```json
[
  "my-alb-1.us-east-1.elb.amazonaws.com",
  "my-alb-2.us-east-1.elb.amazonaws.com"
]
```

Managed via the API or Slack commands — no need to edit the file directly.

## Restart Mode

Pritunl manages OpenVPN as a child process. The `restart_mode` field controls how routes are applied:

**`openvpn_only` (default) — minimal downtime:**
1. Kills the OpenVPN child process(es) with SIGTERM
2. Pritunl detects the process died and respawns it with the updated config
3. If OpenVPN doesn't respawn within 3 seconds, falls back to full `systemctl restart pritunl`

**`full` — full service restart:**
1. Runs `systemctl restart pritunl` directly
2. Longer downtime but guaranteed to work

## Usage (Poller)

The poller resolves DNS for all tracked hostnames. If changes are detected, it saves them to a pending file and sends an interactive Slack message with **Approve** / **Reject** buttons. Routes are **not** applied until approved.

```bash
python update_routes.py --config config.json
python update_routes.py --config config.json --force   # show changes even if no diff
python update_routes.py --config config.json --hostnames /path/to/hostnames.json
```

### Cron / Systemd Timer

The poller is designed to run frequently (every 10s via the systemd timer). Each run that detects changes will send a new interactive Slack message:

```bash
*/5 * * * * /usr/bin/python3 /path/to/update_routes.py --config /path/to/config.json >> /var/log/route-updater.log 2>&1
```

## Route Identification

Each hostname's routes are tagged with `comment: "dns:<hostname>"`. The scripts find existing routes by this comment and only replace those. Other routes on the server (manually added, different hostnames) are preserved.

## Interactive Slack Message

When IP changes are detected, the poller sends an interactive Slack message with the changes and **Approve** / **Reject** buttons:

```
Pending Route Changes — CloudKeeper

• my-alb-1.us-east-1.elb.amazonaws.com
  Old: 10.0.1.10, 10.0.1.11
  New: 10.0.1.50, 10.0.1.51

• my-alb-2.us-east-1.elb.amazonaws.com
  Old: (none)
  New: 10.0.2.20, 10.0.2.21

_Review the changes above and approve or reject._
```

Clicking **Approve** updates the routes in MongoDB, restarts OpenVPN, and replaces the message with a confirmation. Clicking **Reject** discards the pending changes.

## Webhook Server (CRUD + Slack)

`webhook_server.py` is a Flask app providing a REST API for routes and hostnames, plus Slack integration.

### Run

```bash
python webhook_server.py config.json
```

Or with gunicorn (production):

```bash
CONFIG_PATH=/path/to/config.json gunicorn -b 0.0.0.0:5000 webhook_server:app
```

### REST API Endpoints

#### Routes (stored in MongoDB)

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/routes` | List all routes |
| `POST` | `/api/routes` | Add a route |
| `PUT` | `/api/routes/<network>` | Update a route |
| `DELETE` | `/api/routes/<network>` | Delete a route |
| `POST` | `/api/restart` | Trigger OpenVPN restart |

#### Hostnames (stored in hostnames.json)

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/hostnames` | List tracked hostnames |
| `POST` | `/api/hostnames` | Add a hostname `{"hostname": "my-alb.elb.amazonaws.com"}` |
| `DELETE` | `/api/hostnames` | Remove a hostname `{"hostname": "my-alb.elb.amazonaws.com"}` |

### Slack Integration

#### 1. Slash Command (`/routes`)

Create a Slack app:

1. Go to https://api.slack.com/apps → Create New App
2. **Slash Commands** → Create New Command
   - Command: `/routes`
   - Request URL: `https://your-endpoint/slack/command`
3. **Basic Information** → copy **Signing Secret** → add to `config.json` as `slack_signing_secret`
4. Install the app to your workspace

**Available commands:**

```
/routes list              — show all routes
/routes add 10.0.0.0/16   — add a route
/routes delete 10.0.0.0/16 — delete a route
/routes hostnames          — list tracked hostnames
/routes watch my-alb.elb.amazonaws.com    — start tracking a hostname
/routes unwatch my-alb.elb.amazonaws.com  — stop tracking
```

#### 2. Interactive Buttons (Approve/Reject)

The poller sends interactive Slack messages with **Approve** / **Reject** buttons when IP changes are detected. Clicking a button updates the original message with a confirmation.

1. Enable **Interactivity** in your Slack app
2. Set **Request URL** to `https://your-endpoint/slack/interactive`
3. The poller saves pending changes and sends an interactive message
4. Clicking **Approve** applies the routes to MongoDB, restarts OpenVPN, and removes the pending file
5. Clicking **Reject** discards the pending changes

#### 3. Making the Endpoint Public

The Flask app must be publicly accessible with HTTPS:

- **Behind your ALB** — add listener rules forwarding `/api/*` and `/slack/*` to port 5000
- **ngrok** (testing) — `ngrok http 5000`

## Files Overview

| File | Purpose |
|---|---|
| `config.json` | Connection settings, server name, restart mode |
| `hostnames.json` | List of DNS hostnames to track |
| `update_routes.py` | Poller — resolves DNS, updates routes, restarts OpenVPN |
| `webhook_server.py` | Flask API + Slack integration |
| `pritunl-route-updater.service` | Systemd oneshot service for poller |
| `pritunl-route-updater.timer` | Systemd timer (every 10s) for poller |
| `pritunl-webhook.service` | Systemd service for webhook server |

## Verification

```bash
# Check iptables
sudo iptables -t nat -L POSTROUTING -n -v | grep <new_ip>

# Check routes in MongoDB
mongosh pritunl --eval 'db.servers.findOne({name:"CloudKeeper"}, {routes:1}).pretty()'
```
