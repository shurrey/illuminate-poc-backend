"""Whether a user may use the MCP server, and where the MCP deployment publishes itself."""

from __future__ import annotations

import json
import logging
import math
import os
import re
import time

import boto3
from botocore.exceptions import ClientError
from fastapi import HTTPException

from tenancy import Tenant

MCP_REFUSAL = "MCP access is not enabled for your organization"
MCP_ADMIN_REFUSAL = "Admin actions aren't available through MCP"
MCP_RATE_REFUSAL = "Too many MCP requests; try again shortly"
SESSION_REFUSAL = "This MCP session belongs to someone else; start a new one"
SESSION_HEADER_REFUSAL = "X-Mcp-Session must be 1 to 128 letters, digits, '-' or '_'"
SESSION_REQUIRED = "Running raw SQL through MCP needs an X-Mcp-Session header"
GOVERNED_FIRST_REFUSAL = ("Try a governed tool first (run_query or compile_query); raw SQL is for what the semantic "
                          "layer can't answer.")
REASON_REFUSAL = "Give a reason of at least 20 characters for running raw SQL"
MIN_REASON = 20
SESSION_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
SESSION_SECONDS = 24 * 3600
RATE_LIMIT = 120
SESSIONS_TABLE = os.environ.get("MCP_SESSIONS_TABLE", "illuminate-mcp-sessions-dev")
_DISCOVERY_SECONDS = 300
_DISCOVERY_ERROR_SECONDS = 30

logger = logging.getLogger(__name__)
_ssm = None
_sessions_table = None
_discovery: tuple[float, tuple[str | None, str | None]] | None = None  # (expires at, result)


def may_use_mcp(tenant: Tenant, user: dict) -> bool:
    """The single MCP access decision. Today it is the tenant's flag; a per-user flag would be checked here."""
    return tenant.mcp_enabled


def _mcp_discovery() -> tuple[str | None, str | None]:
    """(client_id, url) from SSM, cached 300 s per instance. Missing reads as None; any other SSM error
    reads as (None, None), cached 30 s."""
    global _ssm, _discovery
    now = time.monotonic()
    if _discovery and now <= _discovery[0]:
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
        _discovery = (now + _DISCOVERY_ERROR_SECONDS, (None, None))
        return None, None
    _discovery = (now + _DISCOVERY_SECONDS, result)
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


def _lost_condition(e: ClientError) -> bool:
    return e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"


def session(session_id: str, tenant_id: str, sub: str, now: float | None = None) -> dict:
    """The session item, created ungoverned for the caller on first sight and kept 24 h.
    When it belongs to another tenant or user it is deleted and this raises a 403."""
    now = time.time() if now is None else now
    key = {"pk": f"session#{session_id}"}
    for _ in range(3):
        item = _sessions().get_item(Key=key, ConsistentRead=True).get("Item")
        # TTL deletion lags, so an expired item still present counts as absent.
        if item and int(item.get("expires_at", 0)) > now:
            if item.get("tenant_id") != tenant_id or item.get("sub") != sub:
                _sessions().delete_item(Key=key)
                raise HTTPException(status_code=403, detail=SESSION_REFUSAL)
            return item
        fresh = {**key, "tenant_id": tenant_id, "sub": sub, "governed": False,
                 "expires_at": int(now) + SESSION_SECONDS}
        try:
            _sessions().put_item(Item=fresh, ConditionExpression=("attribute_not_exists(pk) OR attribute_not_exists(expires_at) "
                                                      "OR expires_at <= :now"),
                                 ExpressionAttributeValues={":now": int(now)})
            return fresh
        except ClientError as e:
            if not _lost_condition(e):
                raise
    raise HTTPException(status_code=409, detail="This MCP session changed while it was being set up; try again")


def mark_governed(session_id: str, tenant_id: str, sub: str) -> None:
    """Record a successful governed call. A no-op when the session is gone or now belongs to someone else."""
    try:
        _sessions().update_item(
            Key={"pk": f"session#{session_id}"},
            UpdateExpression="SET governed = :g",
            ConditionExpression="#t = :t AND #s = :s",
            ExpressionAttributeNames={"#t": "tenant_id", "#s": "sub"},
            ExpressionAttributeValues={":g": True, ":t": tenant_id, ":s": sub},
        )
    except ClientError as e:
        if not _lost_condition(e):
            raise


def setup_snippets(url: str) -> dict[str, str]:
    return {
        "claude_desktop": url,
        "claude_code": f"claude mcp add --transport http illuminate {url}",
        "vscode": json.dumps({"servers": {"illuminate": {"type": "http", "url": url}}}, indent=2),
    }
