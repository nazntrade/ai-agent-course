#!/usr/bin/env bash
# This script is sent over SSH stdin by deploy_vps.bat; it runs on the VPS.
set -euo pipefail

EXPECTED_SHA="${1:-}"
REPO=/opt/day16/repo
PROJECT="$REPO/week-04/day-16-mcp-agent"
SEARCH_ENV_FILE=/etc/day16/search.env
DROPIN_DIR=/etc/systemd/system/day16-mcp.service.d
DROPIN_FILE="$DROPIN_DIR/day17-search.conf"

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
systemctl daemon-reload

echo 'Restarting MCP, then backend...'
systemctl restart day16-mcp
systemctl is-active --quiet day16-mcp
systemctl restart day16-backend

echo 'Waiting for the backend and the MCP tools...'
for attempt in {1..20}; do
    if curl -fsS --max-time 5 http://127.0.0.1:8600/api/health >/dev/null 2>&1; then
        status=$(curl -fsS --max-time 5 http://127.0.0.1:8600/api/mcp/status 2>/dev/null || true)
        if [[ "$status" =~ \"connected\"[[:space:]]*:[[:space:]]*true ]] && \
           [[ "$status" =~ \"tools_count\"[[:space:]]*:[[:space:]]*([0-9]+) ]] && \
           (( ${BASH_REMATCH[1]} >= 3 )); then
            echo 'DEPLOY_STATUS: PASS - backend healthy, MCP connected, at least 3 tools.'
            echo "DEPLOY_COMMIT: ${EXPECTED_SHA:0:12}"
            exit 0
        fi
    fi
    sleep 1
done

echo 'DEPLOY_STATUS: FAIL - backend/MCP did not report at least 3 tools.' >&2
echo 'Inspect: systemctl status day16-mcp day16-backend --no-pager' >&2
exit 1
