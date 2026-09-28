#!/usr/bin/env bash
# This script is sent over SSH stdin by deploy_vps.bat; it runs on the VPS.
set -euo pipefail

EXPECTED_SHA="${1:-}"
# Defaults target the production VPS; the DAY16_* overrides exist so the deploy
# flow can be exercised in an isolated sandbox without touching real paths.
REPO="${DAY16_REPO:-/opt/day16/repo}"
PROJECT="$REPO/week-04/day-16-mcp-agent"
SEARCH_ENV_FILE="${DAY16_SEARCH_ENV_FILE:-/etc/day16/search.env}"
DROPIN_DIR="${DAY16_MCP_DROPIN_DIR:-/etc/systemd/system/day16-mcp.service.d}"
DROPIN_FILE="${DAY16_MCP_DROPIN_FILE:-$DROPIN_DIR/day17-search.conf}"
# Day 20: server B (the notifier) is its own service with its own Telegram
# environment file and its database outside the Git checkout.
NOTIFIER_ENV_FILE="${DAY16_NOTIFIER_ENV_FILE:-/etc/day16/notifier.env}"
NOTIFIER_DB_DIR="${DAY16_NOTIFIER_DB_DIR:-/var/lib/day16/notifier}"
NOTIFIER_DB_PATH="$NOTIFIER_DB_DIR/day20-notifier.sqlite3"
NOTIFIER_DROPIN_DIR="${DAY16_NOTIFIER_DROPIN_DIR:-/etc/systemd/system/day16-notifier.service.d}"
NOTIFIER_DROPIN_FILE="${DAY16_NOTIFIER_DROPIN_FILE:-$NOTIFIER_DROPIN_DIR/day20-telegram.conf}"
# The notifier unit must start from the dedicated virtual environment; the
# systemd default (the repository .venv) is missing on the VPS and fails 203/EXEC.
NOTIFIER_PYTHON="${DAY16_NOTIFIER_PYTHON:-/opt/day16/venv/bin/python}"

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
# Checked before any service is restarted, so a missing interpreter reports the
# real cause instead of a systemd 203/EXEC status line.
if [[ ! -x "$NOTIFIER_PYTHON" ]]; then
    echo "ERROR: $NOTIFIER_PYTHON not found; day16-notifier would fail with 203/EXEC. See docs/vps-setup-day20.md." >&2
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

HEAD_SHA=$(runuser -u day16 -- git -C "$REPO" rev-parse HEAD)
ALREADY_DEPLOYED=0
if [[ "$HEAD_SHA" == "$EXPECTED_SHA" ]]; then
    # Re-running the same release must not depend on GitHub being reachable.
    ALREADY_DEPLOYED=1
    echo "VPS checkout already matches ${EXPECTED_SHA:0:12}; skipping the GitHub fetch."
else
    # Bound the fetch: a VPS that cannot reach github.com must fail with a named
    # reason and a hard time limit instead of hanging the deploy.
    fetch_rc=0
    fetch_output=$(timeout 60 runuser -u day16 -- git -C "$REPO" \
        -c http.lowSpeedLimit=1000 -c http.lowSpeedTime=15 \
        fetch --quiet origin main 2>&1) || fetch_rc=$?
    if [[ $fetch_rc -eq 124 ]]; then
        echo 'ERROR: VPS could not reach github.com:443 within 60s (git fetch timed out).' >&2
        echo 'Check the VPS network and DNS, then run the deploy again.' >&2
        exit 1
    fi
    if [[ $fetch_rc -ne 0 ]]; then
        echo "ERROR: VPS could not reach github.com:443 (git fetch exited with ${fetch_rc})." >&2
        printf '%s\n' "$fetch_output" | tail -n 3 >&2 || true
        echo 'Check the VPS network and DNS, then run the deploy again.' >&2
        exit 1
    fi
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
fi

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

restart_and_check() {
    local unit="$1"
    systemctl restart "$unit"
    if ! systemctl is-active --quiet "$unit"; then
        echo "ERROR: $unit did not become active after restart." >&2
        echo "Inspect: systemctl status $unit --no-pager" >&2
        exit 1
    fi
}

echo 'Restarting MCP (A), then notifier (B), then backend...'
restart_and_check day16-mcp
restart_and_check day16-notifier
restart_and_check day16-backend

echo 'Waiting for the backend and the deployed A+B tool contract...'
health_ok=0
servers_ok=0
tools_ok=0
for attempt in {1..20}; do
    if curl -fsS --max-time 5 http://127.0.0.1:8600/api/health >/dev/null 2>&1; then
        health_ok=1
        servers_response=$(curl -fsS --max-time 5 http://127.0.0.1:8600/api/mcp/servers 2>/dev/null || true)
        if [[ -n "$servers_response" ]]; then
            servers_ok=1
            if printf '%s' "$servers_response" | python3 "$PROJECT/harness/check_deploy_tools.py" \
                   "$PROJECT/mcp_server/server.py" "$PROJECT/notifier_server/server.py" >/dev/null 2>&1; then
                tools_ok=1
                break
            fi
        fi
    fi
    sleep 1
done

if [[ $tools_ok -eq 1 ]]; then
    echo 'backend /api/health: OK'
    echo 'A/B connectivity and tool lists: OK'
    if [[ $ALREADY_DEPLOYED -eq 1 ]]; then
        echo 'DEPLOY_STATUS: PASS - already deployed; backend healthy, A and B connected, registered tools match the deployed sources.'
    else
        echo 'DEPLOY_STATUS: PASS - backend healthy, A and B connected, registered tools match the deployed sources.'
    fi
    echo "DEPLOY_COMMIT: ${EXPECTED_SHA:0:12}"
    exit 0
fi

if [[ $health_ok -eq 0 ]]; then
    reason='backend /api/health did not become ready'
elif [[ $servers_ok -eq 0 ]]; then
    reason='the A/B connectivity check returned no server list'
else
    reason='the A/B tool names did not match the deployed sources'
fi
echo "DEPLOY_STATUS: FAIL - ${reason}." >&2
echo 'Inspect: systemctl status day16-mcp day16-notifier day16-backend --no-pager' >&2
exit 1
