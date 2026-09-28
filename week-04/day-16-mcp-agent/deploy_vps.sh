#!/usr/bin/env bash
# This script is sent over SSH stdin by deploy_vps.bat; it runs on the VPS.
set -euo pipefail

EXPECTED_SHA="${1:-}"
REPO=/opt/day16/repo
PROJECT="$REPO/week-04/day-16-mcp-agent"
SEARCH_ENV_FILE=/etc/day16/search.env
DROPIN_DIR=/etc/systemd/system/day16-mcp.service.d
DROPIN_FILE="$DROPIN_DIR/day17-search.conf"
# Day 20: server B (the notifier) is its own service with its own Telegram
# environment file and its database outside the Git checkout.
NOTIFIER_ENV_FILE=/etc/day16/notifier.env
NOTIFIER_DB_DIR=/var/lib/day16/notifier
NOTIFIER_DB_PATH="$NOTIFIER_DB_DIR/day20-notifier.sqlite3"
NOTIFIER_DROPIN_DIR=/etc/systemd/system/day16-notifier.service.d
NOTIFIER_DROPIN_FILE="$NOTIFIER_DROPIN_DIR/day20-telegram.conf"

if [[ ! "$EXPECTED_SHA" =~ ^[[:xdigit:]]{40}$ ]]; then
    echo 'ERROR: Invalid expected Git commit.' >&2
    exit 1
fi
if [[ $(id -u) -ne 0 ]]; then
    echo 'ERROR: Run deployment over SSH as root.' >&2
    exit 1
fi
if [[ ! -d "$REPO/.git" || ! -f "$PROJECT/.env" ]]; then
    echo 'ERROR: Existing VPS deployment or its model .env is missing.' >&2
    exit 1
fi
if [[ ! -f "$SEARCH_ENV_FILE" ]]; then
    echo 'ERROR: Create /etc/day16/search.env with TAVILY_API_KEY first.' >&2
    exit 1
fi
# The backend already reads the project .env, so it must not contain this key.
if grep -Eq '^[[:space:]]*TAVILY_API_KEY=' "$PROJECT/.env"; then
    echo 'ERROR: Remove TAVILY_API_KEY from the VPS project .env (backend reads it).' >&2
    exit 1
fi
# Telegram credentials belong only to server B; the project .env is read by the
# backend, so it must not contain them either.
for key in TELEGRAM_BOT_TOKEN TELEGRAM_CHAT_ID; do
    if grep -Eq "^[[:space:]]*${key}=" "$PROJECT/.env"; then
        echo "ERROR: Remove ${key} from the VPS project .env (only server B reads it)." >&2
        exit 1
    fi
done
if [[ $(stat -c %U "$SEARCH_ENV_FILE") != root ||
      $(stat -c %a "$SEARCH_ENV_FILE") != 600 ]]; then
    echo 'ERROR: /etc/day16/search.env must be owned by root with mode 600.' >&2
    exit 1
fi
# Check without ever printing the key; keep it out of the backend's model .env.
if ! grep -Eq '^[[:space:]]*TAVILY_API_KEY=[^[:space:]#]+' "$SEARCH_ENV_FILE"; then
    echo 'ERROR: TAVILY_API_KEY is missing in /etc/day16/search.env.' >&2
    exit 1
fi
# Server B configuration: Telegram credentials live only here.
if [[ ! -f "$NOTIFIER_ENV_FILE" ]]; then
    echo 'ERROR: Create /etc/day16/notifier.env with the Telegram settings first.' >&2
    exit 1
fi
if [[ $(stat -c %U "$NOTIFIER_ENV_FILE") != root ||
      $(stat -c %a "$NOTIFIER_ENV_FILE") != 600 ]]; then
    echo 'ERROR: /etc/day16/notifier.env must be owned by root with mode 600.' >&2
    exit 1
fi
# Check the token and the recipient are present without ever printing them.
if ! grep -Eq '^[[:space:]]*TELEGRAM_BOT_TOKEN=[^[:space:]#]+' "$NOTIFIER_ENV_FILE"; then
    echo 'ERROR: TELEGRAM_BOT_TOKEN is missing in /etc/day16/notifier.env.' >&2
    exit 1
fi
if ! grep -Eq '^[[:space:]]*TELEGRAM_CHAT_ID=[^[:space:]#]+' "$NOTIFIER_ENV_FILE"; then
    echo 'ERROR: TELEGRAM_CHAT_ID is missing in /etc/day16/notifier.env.' >&2
    exit 1
fi
if ! grep -Fxq "NOTIFIER_DB_PATH=$NOTIFIER_DB_PATH" "$NOTIFIER_ENV_FILE"; then
    echo 'ERROR: /etc/day16/notifier.env must set NOTIFIER_DB_PATH to the external notifier database.' >&2
    exit 1
fi
# The notifier database must live outside the Git checkout and its directory
# must belong to day16, so a deploy never overwrites or removes it.
if [[ "$NOTIFIER_DB_PATH" == "$REPO"/* ]]; then
    echo 'ERROR: NOTIFIER_DB_PATH must be outside the repository.' >&2
    exit 1
fi
if [[ ! -d "$NOTIFIER_DB_DIR" ]]; then
    echo 'ERROR: Create /var/lib/day16/notifier owned by day16 first.' >&2
    exit 1
fi
if [[ $(stat -c %U "$NOTIFIER_DB_DIR") != day16 ]]; then
    echo 'ERROR: /var/lib/day16/notifier must be owned by day16.' >&2
    exit 1
fi
if [[ $(runuser -u day16 -- git -C "$REPO" branch --show-current) != main ]]; then
    echo 'ERROR: The VPS repository is not on main.' >&2
    exit 1
fi
if [[ -n $(runuser -u day16 -- git -C "$REPO" status --porcelain) ]]; then
    echo 'ERROR: The VPS repository has local changes. Resolve them manually.' >&2
    exit 1
fi

runuser -u day16 -- git -C "$REPO" fetch --quiet origin main
FETCHED_SHA=$(runuser -u day16 -- git -C "$REPO" rev-parse FETCH_HEAD)
if [[ "$FETCHED_SHA" != "$EXPECTED_SHA" ]]; then
    echo 'ERROR: VPS fetched a different commit from the verified local main.' >&2
    exit 1
fi
runuser -u day16 -- git -C "$REPO" merge-base --is-ancestor HEAD FETCH_HEAD || {
    echo 'ERROR: VPS changes cannot be fast-forwarded.' >&2
    exit 1
}

echo 'Updating VPS checkout with a fast-forward merge...'
runuser -u day16 -- git -C "$REPO" merge --quiet --ff-only FETCH_HEAD

# Only MCP reads search.env; the backend's model .env never receives this key.
mkdir -p "$DROPIN_DIR"
if [[ -e "$DROPIN_FILE" ]]; then
    if ! grep -Fxq "EnvironmentFile=$SEARCH_ENV_FILE" "$DROPIN_FILE" ||
       ! grep -Fxq 'Environment=MCP_LOAD_DOTENV=0' "$DROPIN_FILE"; then
        echo 'ERROR: Existing MCP service override differs; inspect it manually.' >&2
        exit 1
    fi
else
    printf '[Service]\nEnvironmentFile=%s\nEnvironment=MCP_LOAD_DOTENV=0\n' \
        "$SEARCH_ENV_FILE" > "$DROPIN_FILE"
fi

# Only server B reads the Telegram settings; its service gets the dedicated
# environment file and never reads the project .env.
mkdir -p "$NOTIFIER_DROPIN_DIR"
if [[ -e "$NOTIFIER_DROPIN_FILE" ]]; then
    if ! grep -Fxq "EnvironmentFile=$NOTIFIER_ENV_FILE" "$NOTIFIER_DROPIN_FILE" ||
       ! grep -Fxq 'Environment=NOTIFIER_LOAD_DOTENV=0' "$NOTIFIER_DROPIN_FILE"; then
        echo 'ERROR: Existing notifier service override differs; inspect it manually.' >&2
        exit 1
    fi
else
    printf '[Service]\nEnvironmentFile=%s\nEnvironment=NOTIFIER_LOAD_DOTENV=0\n' \
        "$NOTIFIER_ENV_FILE" > "$NOTIFIER_DROPIN_FILE"
fi
systemctl daemon-reload

echo 'Restarting MCP (A), then notifier (B), then backend...'
systemctl restart day16-mcp
systemctl is-active --quiet day16-mcp
systemctl restart day16-notifier
systemctl is-active --quiet day16-notifier
systemctl restart day16-backend
systemctl is-active --quiet day16-backend

echo 'Waiting for the backend and the deployed A+B tool contract...'
for attempt in {1..20}; do
    if curl -fsS --max-time 5 http://127.0.0.1:8600/api/health >/dev/null 2>&1; then
        servers_response=$(curl -fsS --max-time 5 http://127.0.0.1:8600/api/mcp/servers 2>/dev/null || true)
        if [[ -n "$servers_response" ]] && \
           printf '%s' "$servers_response" | python3 "$PROJECT/harness/check_deploy_tools.py" \
               "$PROJECT/mcp_server/server.py" "$PROJECT/notifier_server/server.py" >/dev/null 2>&1; then
            echo 'DEPLOY_STATUS: PASS - backend healthy, A and B connected, registered tools match the deployed sources.'
            echo "DEPLOY_COMMIT: ${EXPECTED_SHA:0:12}"
            exit 0
        fi
    fi
    sleep 1
done

echo 'DEPLOY_STATUS: FAIL - backend health or the A/B connectivity and tool names did not match the deployed sources.' >&2
echo 'Inspect: systemctl status day16-mcp day16-notifier day16-backend --no-pager' >&2
exit 1
