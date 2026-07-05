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
  "openvpn_restart_cmd": "sudo systemctl restart pritunl",
  "nat": true,
  "mongodb_uri": "mongodb://localhost:27017",
  "mongodb_db": "pritunl"
}
```

| Key | Required | Description |
|---|---|---|
| `server_name` | Yes | Pritunl server name (matches `name` field in MongoDB `servers` collection) |
| `hostnames` | Yes | Array of DNS names to track |
| `slack_webhook` | No | Slack incoming webhook URL (or `SLACK_WEBHOOK_URL` env var) |
| `openvpn_restart_cmd` | No | Default: `sudo systemctl restart pritunl` |
| `nat` | No | Enable NAT on routes (default: `true`) |
| `mongodb_uri` | No | Default: `mongodb://localhost:27017` (or `MONGODB_URI` env var) |
| `mongodb_db` | No | Default: `pritunl` |

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

## Verification

```bash
# Check iptables
sudo iptables -t nat -L POSTROUTING -n -v | grep <new_ip>

# Check routes in MongoDB
mongosh pritunl --eval 'db.servers.findOne({name:"CloudKeeper"}, {routes:1}).pretty()'
```
