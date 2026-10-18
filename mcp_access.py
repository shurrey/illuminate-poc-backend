"""Whether a user may use the MCP server, and where the MCP deployment publishes itself."""

from __future__ import annotations

import json
import logging
import math
import os
import time

import boto3

from tenancy import Tenant

MCP_REFUSAL = "MCP access is not enabled for your organization"
MCP_ADMIN_REFUSAL = "Admin actions aren't available through MCP"
MCP_RATE_REFUSAL = "Too many MCP requests; try again shortly"
RATE_LIMIT = 120
SESSIONS_TABLE = os.environ.get("MCP_SESSIONS_TABLE", "illuminate-mcp-sessions-dev")
_DISCOVERY_SECONDS = 300

logger = logging.getLogger(__name__)
_ssm = None
_sessions_table = None
_discovery: tuple[float, tuple[str | None, str | None]] | None = None


def may_use_mcp(tenant: Tenant, user: dict) -> bool:
    """The single MCP access decision. Today it is the tenant's flag; a per-user flag would be checked here."""
    return tenant.mcp_enabled


def _mcp_discovery() -> tuple[str | None, str | None]:
    """(client_id, url) from SSM, cached 300 s per instance. Missing reads as None; any other SSM error
    reads as (None, None) and is not cached, so the next call retries."""
    global _ssm, _discovery
    now = time.monotonic()
    if _discovery and now - _discovery[0] <= _DISCOVERY_SECONDS:
        return _discovery[1]
    try:
        if _ssm is None:
            _ssm = boto3.client("ssm")
        prefix = f"/illuminate/{os.environ.get('ENVIRONMENT', 'dev')}"

        def read(name: str) -> str | None:
            try:
                return _ssm.get_parameter(Name=f"{prefix}/{name}")["Parameter"]["Value"]
            except _ssm.exceptions.ParameterNotFound:
                return None

        result = (read("mcp-client-id"), read("mcp-url"))
    except Exception:
        logger.warning("MCP discovery failed", exc_info=True)
        return None, None
    _discovery = (now, result)
    return result


def mcp_client_id() -> str | None:
    """The MCP app client id, or None when it is not published or cannot be read. Never raises."""
    try:
        return _mcp_discovery()[0]
    except Exception:
        logger.warning("MCP discovery failed", exc_info=True)
        return None


def _sessions():
    global _sessions_table
    if _sessions_table is None:
        _sessions_table = boto3.resource("dynamodb").Table(SESSIONS_TABLE)
    return _sessions_table


def charge(tenant_id: str, sub: str, now: float) -> int | None:
    """Count one MCP request in the user's fixed one-minute window (now is epoch seconds).
    None when within RATE_LIMIT, else the Retry-After seconds until the next window."""
    minute = int(now // 60)
    attrs = _sessions().update_item(
        Key={"pk": f"rate#{tenant_id}#{sub}#{minute}"},
        UpdateExpression="ADD n :one SET expires_at = :exp",
        ExpressionAttributeValues={":one": 1, ":exp": (minute + 2) * 60},
        ReturnValues="UPDATED_NEW",
    )["Attributes"]
    if int(attrs["n"]) <= RATE_LIMIT:
        return None
    return max(1, math.ceil((minute + 1) * 60 - now))


def setup_snippets(url: str) -> dict[str, str]:
    return {
        "claude_desktop": url,
        "claude_code": f"claude mcp add --transport http illuminate {url}",
        "vscode": json.dumps({"servers": {"illuminate": {"type": "http", "url": url}}}, indent=2),
    }
