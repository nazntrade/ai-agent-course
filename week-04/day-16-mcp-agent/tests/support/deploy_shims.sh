#!/usr/bin/env bash
# Deterministic stand-ins for the privileged VPS commands used by deploy_vps.sh.
#
# The test copies this file under each command name (git, stat, runuser, ...),
# puts that directory first on PATH and drives every decision through SHIM_*
# environment variables. No test run touches the network, real services or
# real system paths. Each invocation is appended to $SHIM_LOG when it is set.
set -u

name="$(basename "$0")"

log() {
    if [[ -n "${SHIM_LOG:-}" ]]; then
        printf '%s %s\n' "$name" "$*" >> "$SHIM_LOG"
    fi
}

case "$name" in
    id)
        log "$*"
        # The deploy gate only asks for the effective uid; the sandbox is "root".
        if [[ "${1:-}" == "-u" ]]; then
            echo 0
        fi
        exit 0
        ;;
    stat)
        log "$*"
        fmt="${2:-}"
        path="${3:-}"
        case "$fmt" in
            %U)
                if [[ -n "${SHIM_DB_DIR:-}" && "$path" == "$SHIM_DB_DIR" ]]; then
                    echo day16
                else
                    echo root
                fi
                ;;
            %a)
                echo 600
                ;;
            *)
                echo 0
                ;;
        esac
        exit 0
        ;;
    runuser)
        log "$*"
        # Drop "-u <user> --" and execute the wrapped command.
        while [[ $# -gt 0 && "$1" != "--" ]]; do
            shift
        done
        if [[ $# -gt 0 ]]; then
            shift
        fi
        exec "$@"
        ;;
    systemctl)
        log "$*"
        args=("$@")
        last="${args[${#args[@]}-1]}"
        if [[ "${1:-}" == "is-active" ]]; then
            if [[ -n "${SHIM_FAIL_UNIT:-}" && "$last" == "$SHIM_FAIL_UNIT" ]]; then
                exit 1
            fi
            exit 0
        fi
        # restart and daemon-reload succeed silently.
        exit 0
        ;;
    git)
        log "$*"
        # Skip git global options (-C <dir>, -c <key=value>) used by the deploy.
        while [[ $# -gt 0 ]]; do
            case "$1" in
                -C|-c)
                    shift 2
                    ;;
                --git-dir=*)
                    shift
                    ;;
                *)
                    break
                    ;;
            esac
        done
        sub="${1:-}"
        shift || true
        case "$sub" in
            branch)
                echo main
                ;;
            status)
                # Clean tree: no porcelain output.
                ;;
            rev-parse)
                case "${1:-}" in
                    FETCH_HEAD)
                        echo "${SHIM_FETCH_SHA:-${SHIM_HEAD_SHA:?}}"
                        ;;
                    *)
                        echo "${SHIM_HEAD_SHA:?}"
                        ;;
                esac
                ;;
            merge-base)
                exit 0
                ;;
            fetch)
                if [[ -n "${SHIM_FETCH_MARKER:-}" ]]; then
                    : > "$SHIM_FETCH_MARKER"
                fi
                exit "${SHIM_FETCH_EXIT:-0}"
                ;;
            merge)
                exit 0
                ;;
            *)
                exit 0
                ;;
        esac
        exit 0
        ;;
    curl)
        log "$*"
        url="${!#}"
        if [[ "$url" == *api/health* ]]; then
            exit "${SHIM_HEALTH_EXIT:-0}"
        fi
        if [[ "$url" == *api/mcp/servers* ]]; then
            if [[ "${SHIM_HEALTH_EXIT:-0}" -ne 0 ]]; then
                exit 1
            fi
            printf '%s' "${SHIM_SERVERS_JSON:-{\"servers\": []}}"
            exit 0
        fi
        exit 0
        ;;
    python3)
        log "$*"
        # Consume the piped tool-list JSON; the exit code is the point.
        cat >/dev/null
        exit "${SHIM_PYTHON3_EXIT:-0}"
        ;;
    sleep)
        log "$*"
        exit 0
        ;;
    timeout)
        log "$*"
        if [[ -n "${SHIM_TIMEOUT_EXIT:-}" ]]; then
            exit "$SHIM_TIMEOUT_EXIT"
        fi
        shift || true
        exec "$@"
        ;;
    *)
        exit 0
        ;;
esac
