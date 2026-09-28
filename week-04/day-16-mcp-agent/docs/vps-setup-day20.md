# VPS setup for Day 20 (second MCP server, notifier)

This guide describes the one-time setup of the Day 20 notifier server (server B)
on a Linux VPS. It assumes the Day 17-19 deployment already exists: repository
`/opt/day16/repo`, user `day16`, backend `day16-backend` and search server
`day16-mcp`. All commands are run once, as root, on the server; nothing here is
executed automatically by the project.

Secrets values are never written into this document or into the repository. The
`deploy_vps.sh` script only checks that the files exist, are owned by `root`
with mode `600` and contain non-empty values; it never prints them.

## 1. User and directories

```bash
id day16 || useradd --system --create-home --home-dir /opt/day16 --shell /usr/sbin/nologin day16
install -d -o day16 -g day16 /var/lib/day16
install -d -o day16 -g day16 /var/lib/day16/notifier
```

* `/var/lib/day16/notifier` holds the notifier database
  (`day20-notifier.sqlite3`). It is **outside** the Git checkout, so a deploy
  never overwrites or removes it.
* The notifier database is created lazily on the first watch operation by the
  notifier process; the directory must exist and belong to `day16`.

## 2. Telegram environment file

Create `/etc/day16/notifier.env` (values are placeholders; never commit them):

```bash
install -o root -g root -m 600 /dev/null /etc/day16/notifier.env
cat > /etc/day16/notifier.env <<'EOF'
TELEGRAM_BOT_TOKEN=<bot token>
TELEGRAM_CHAT_ID=<recipient chat id>
NOTIFIER_DB_PATH=/var/lib/day16/notifier/day20-notifier.sqlite3
EOF
```

Only the notifier process (server B) reads this file. The backend's model `.env`
at `<repo>/week-04/day-16-mcp-agent/.env` must not contain
`TELEGRAM_BOT_TOKEN` or `TELEGRAM_CHAT_ID`; `deploy_vps.sh` refuses to deploy if
it does.

## 3. systemd unit `day16-notifier.service`

Create `/etc/systemd/system/day16-notifier.service`:

```ini
[Unit]
Description=Day 20 notifier MCP server (server B)
After=network.target

[Service]
Type=simple
User=day16
Group=day16
WorkingDirectory=/opt/day16/repo/week-04/day-16-mcp-agent
EnvironmentFile=/etc/day16/notifier.env
Environment=NOTIFIER_LOAD_DOTENV=0
ExecStart=/opt/day16/repo/week-04/day-16-mcp-agent/.venv/bin/python -m notifier_server
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

* `EnvironmentFile=/etc/day16/notifier.env` supplies the Telegram settings.
* `Environment=NOTIFIER_LOAD_DOTENV=0` stops the process from reading the
  project `.env`, so the backend model `.env` is never used by server B.
* The unit binds the notifier on `MCP_NOTIFIER_HOST`/`MCP_NOTIFIER_PORT`
  (default `127.0.0.1:8766`); keep the value used by `MCP_NOTIFIER_URL` in the
  backend `.env`.

Enable it once:

```bash
systemctl daemon-reload
systemctl enable day16-notifier
```

`deploy_vps.sh` also maintains the drop-in
`/etc/systemd/system/day16-notifier.service.d/day20-telegram.conf` with the same
`EnvironmentFile` and `NOTIFIER_LOAD_DOTENV=0`, so the settings survive an
edited base unit.

## 4. Startup and restart order

Run the services in this order; each one is checked before the next starts:

```bash
systemctl restart day16-mcp        # A: search and scheduled runs
systemctl is-active day16-mcp
systemctl restart day16-notifier   # B: watches and Telegram delivery
systemctl is-active day16-notifier
systemctl restart day16-backend    # FastAPI backend on top of A + B
systemctl is-active day16-backend
```

## 5. Verify the deployment

```bash
curl -fsS http://127.0.0.1:8600/api/health
curl -fsS http://127.0.0.1:8600/api/mcp/servers
```

`/api/mcp/servers` returns one entry per server. Server `A` and server `B` must
both report `connected: true`, and each entry carries the additive `tool_names`
list:

* `A.tool_names` must equal the names registered in
  `mcp_server/server.py`;
* `B.tool_names` must equal the names registered in
  `notifier_server/server.py`.

`deploy_vps.sh` performs exactly this check through
`harness/check_deploy_tools.py` and prints
`DEPLOY_STATUS: PASS` only when the backend is healthy and both lists match,
otherwise `DEPLOY_STATUS: FAIL` with the inspection command.

If `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` are missing, server B stays
`connected` but `send_notification` answers `not_configured`; it never invents a
delivery.
