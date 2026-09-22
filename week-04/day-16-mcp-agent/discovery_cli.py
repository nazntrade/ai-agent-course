"""MCP discovery CLI.

Connects to the MCP server over Streamable HTTP, performs the protocol
handshake, reads every page of ``tools/list`` until ``next_cursor`` is
exhausted, validates the payload, prints a compact report and closes the
session again.

Usage::

    python discovery_cli.py [--url URL] [--check] [--json]

Exit codes: 0 success, 2 failed discovery (unreachable, timeout, protocol or an
invalid response). A failure prints a coarse category and a safe message; no
credential, header, environment variable or secret is ever printed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from agent.mcp_adapter import inspect_tools
from agent.settings import resolve_settings

EXIT_OK = 0
EXIT_FAILURE = 2

# Categories that the CLI is allowed to report (mirrors the adapter).
REPORTED_CATEGORIES = ("unreachable", "timeout", "protocol", "invalid_response")


def _safe_category(error) -> str:
    category = str(getattr(error, "category", "") or "protocol")
    return category if category in REPORTED_CATEGORIES else "protocol"


def _schema_line(schema) -> str:
    """Render a schema compactly on one line."""
    try:
        return json.dumps(schema or {}, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return "{}"


def format_success(status, tools) -> str:
    """Render the human-readable success report."""
    lines = [
        "CONNECTED",
        f"PROTOCOL_VERSION: {status.protocol_version or 'unknown'}",
        f"SERVER_INFO name={status.server_name or 'unknown'} "
        f"version={status.server_version or 'unknown'}",
        f"TOOLS_COUNT: {len(tools)}",
    ]
    for tool in tools:
        lines.append(f"TOOL: {tool.name}")
        lines.append(f"DESCRIPTION: {tool.description}")
        lines.append(f"INPUT_SCHEMA: {_schema_line(tool.input_schema)}")
    return "\n".join(lines)


def format_failure(error) -> str:
    """Render the human-readable failure report."""
    category = _safe_category(error)
    message = str(getattr(error, "message", "") or "The MCP discovery failed")
    return "\n".join(
        [
            "CONNECTED: false",
            f"ERROR_CATEGORY: {category}",
            f"ERROR: {message}",
        ]
    )


def _success_payload(status, tools) -> dict:
    return {
        "connected": True,
        "protocol_version": status.protocol_version,
        "server": {"name": status.server_name, "version": status.server_version},
        "tools_count": len(tools),
        "tools": [
            {
                "name": tool.name,
                "description": tool.description,
                "input_schema": tool.input_schema,
            }
            for tool in tools
        ],
    }


def _failure_payload(error) -> dict:
    return {
        "connected": False,
        "error": {
            "category": _safe_category(error),
            "message": str(getattr(error, "message", "") or "The MCP discovery failed"),
        },
    }


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser."""
    parser = argparse.ArgumentParser(
        prog="discovery_cli.py",
        description="Discover the tools of a loopback MCP server.",
    )
    parser.add_argument(
        "--url",
        default=None,
        help="MCP endpoint URL (default: MCP_SERVER_URL or the loopback default).",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Quiet mode: only the exit code reports the result.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Print the result as JSON instead of the key/value report.",
    )
    return parser


async def discover(url: str, *, connect_timeout_s: float = 10.0):
    """Run the handshake and the full tool listing."""
    return await inspect_tools(url, connect_timeout_s=connect_timeout_s)


def main(argv=None) -> int:
    """Run the CLI and return its exit code."""
    args = build_parser().parse_args(argv)
    settings = resolve_settings()
    url = str(args.url or settings.mcp_server_url).strip() or settings.mcp_server_url

    try:
        status, tools = asyncio.run(
            discover(url, connect_timeout_s=settings.mcp_connect_timeout_seconds)
        )
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        return EXIT_FAILURE

    if status.connected:
        if args.check:
            return EXIT_OK
        if args.as_json:
            print(json.dumps(_success_payload(status, tools), ensure_ascii=False, indent=2))
        else:
            print(format_success(status, tools))
        return EXIT_OK

    error = status.error
    if args.check:
        return EXIT_FAILURE
    if args.as_json:
        print(json.dumps(_failure_payload(error), ensure_ascii=False, indent=2))
    else:
        print(format_failure(error))
    return EXIT_FAILURE


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
