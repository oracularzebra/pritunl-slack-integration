# Pritunl Route Updater

Polls DNS hostnames (ALBs, NLBs, etc.) for IP changes, updates Pritunl VPN route entries in MongoDB, and restarts OpenVPN.

## How It Works

1. Reads a JSON config file with a list of hostnames and target Pritunl server
2. For each hostname, resolves DNS to current IPs
3. Compares with existing routes in MongoDB (matched by `comment: "dns:<hostname>"`)
4. If any hostname's IPs changed, updates MongoDB, restarts OpenVPN, sends Slack notification
5. Only routes matching tracked hostnames are touched — other routes are left intact

## Requirements

- Python 3.8+
- Access to the Pritunl MongoDB instance
- `sudo` access to restart OpenVPN
- (Optional) Slack incoming webhook URL

## Installation

```bash
pip install pymongo requests
```

## Config File

Create `config.json`:

```json
{
  "server_name": "CloudKeeper",
  "hostnames": [
    "my-alb-1.us-east-1.elb.amazonaws.com",
    "my-alb-2.us-east-1.elb.amazonaws.com"
  ],
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
| `server_name` | Yes | Pritunl server name (matches `name` field in MongoDB `servers` collection) |
| `hostnames` | Yes | Array of DNS names to track |
| `slack_webhook` | No | Slack incoming webhook URL (or `SLACK_WEBHOOK_URL` env var) |
| `restart_mode` | No | `"openvpn_only"` (kill child, Pritunl respawns) or `"full"` (systemctl restart) | `"openvpn_only"` |
| `openvpn_restart_cmd` | No | Used as fallback when `restart_mode: "openvpn_only"` or always when `"full"` | `sudo systemctl restart pritunl` |
| `nat` | No | Enable NAT on routes (default: `true`) |
| `mongodb_uri` | No | Default: `mongodb://localhost:27017` (or `MONGODB_URI` env var) |
| `mongodb_db` | No | Default: `pritunl` |

## Restart Mode

Pritunl manages OpenVPN as a child process. The `restart_mode` field controls how routes are applied:

**`openvpn_only` (default) — minimal downtime:**
1. Kills the OpenVPN child process(es) with SIGTERM
2. Pritunl detects the process died and respawns it with the updated config
3. If OpenVPN doesn't respawn within 3 seconds, falls back to full `systemctl restart pritunl`

**`full` — full service restart:**
1. Runs `systemctl restart pritunl` directly
2. Longer downtime but guaranteed to work

## Usage

```bash
python update_routes.py --config config.json
python update_routes.py --config config.json --force   # apply even if no change
```

## Cron

```bash
*/5 * * * * /usr/bin/python3 /path/to/update_routes.py --config /path/to/config.json >> /var/log/route-updater.log 2>&1
```

## Route Identification

Each hostname's routes are tagged with `comment: "dns:<hostname>"`. The script finds existing routes by this comment and only replaces those. Other routes on the server (manually added, different hostnames) are preserved.

## Slack Notification Format

```
Pritunl Routes Updated — CloudKeeper

• my-alb-1.us-east-1.elb.amazonaws.com
  Old: 10.0.1.10, 10.0.1.11
  New: 10.0.1.50, 10.0.1.51

• my-alb-2.us-east-1.elb.amazonaws.com
  Old: (none)
  New: 10.0.2.20, 10.0.2.21
```

## Webhook Server (CRUD + Slack)

`webhook_server.py` is a Flask app providing a REST API for routes and Slack integration.

### Run

```bash
pip install flask gunicorn
python webhook_server.py config.json
```

Or with gunicorn (production):

```bash
# Set CONFIG_PATH env var so the app finds it
CONFIG_PATH=/path/to/config.json gunicorn -b 0.0.0.0:5000 webhook_server:app
```

### REST API Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/routes` | List all routes |
| `POST` | `/api/routes` | Add a route (`{"network": "10.0.0.0/16", "comment": "...", "nat": true}`) |
| `PUT` | `/api/routes/<network>` | Update a route (URL-encode CIDR, e.g. `10.0.0.0%2F16`) |
| `DELETE` | `/api/routes/<network>` | Delete a route |
| `POST` | `/api/restart` | Trigger OpenVPN restart |

### Slack Integration

#### 1. Slash Command (`/routes`)

Create a Slack app with a Slash Command:

1. Go to https://api.slack.com/apps → Create New App
2. **Slash Commands** → Create New Command
   - Command: `/routes`
   - Request URL: `https://your-public-endpoint/slack/command`
   - Description: "Manage Pritunl routes"
3. **Basic Information** → copy **Signing Secret** → add to `config.json` as `slack_signing_secret`
4. Install the app to your workspace

**Available commands:**

```
/routes list              — show all routes
/routes add 10.0.0.0/16   — add a route
/routes delete 10.0.0.0/16 — delete a route
```

#### 2. Interactive Buttons (Approve/Reject)

For the approval flow (polling script saves pending changes, Slack asks for approval):

1. In your Slack app, enable **Interactivity**
2. Set **Request URL** to `https://your-public-endpoint/slack/interactive`
3. The poller (`update_routes.py`) saves pending changes to `pending_file`, then sends a Slack message with **Approve** / **Reject** buttons
4. Clicking **Approve** applies the routes and restarts OpenVPN

#### 3. Making the Endpoint Public

The Flask app must be publicly accessible with HTTPS. Options:

- **Behind your ALB** — add a listener rule forwarding `/slack/` to the EC2 instance on port 5000
- **ngrok** (testing) — `ngrok http 5000` gives a public HTTPS URL
- **Caddy / nginx** — reverse proxy with Let's Encrypt

### New Config Fields

| Key | Required | Description |
|---|---|---|
| `slack_signing_secret` | No | Slack app signing secret (verifies requests) |
| `pending_file` | No | Path to store pending route changes (default: `/tmp/pending_routes.json`) |
| `port` | No | Flask listen port (default: `5000`) |

## Verification

```bash
# Check iptables
sudo iptables -t nat -L POSTROUTING -n -v | grep <new_ip>

# Check routes in MongoDB
mongosh pritunl --eval 'db.servers.findOne({name:"CloudKeeper"}, {routes:1}).pretty()'
```
