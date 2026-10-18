"""Whether a user may use the MCP server, and where the MCP deployment publishes itself."""

from __future__ import annotations

import json
import os
import time

import boto3

from tenancy import Tenant

MCP_REFUSAL = "MCP access is not enabled for your organization"
_DISCOVERY_SECONDS = 300

_ssm = None
_discovery: tuple[float, tuple[str | None, str | None]] | None = None


def may_use_mcp(tenant: Tenant, user: dict) -> bool:
    """The single MCP access decision. Today it is the tenant's flag; a per-user flag would be checked here."""
    return tenant.mcp_enabled


def _mcp_discovery() -> tuple[str | None, str | None]:
    """(client_id, url) from SSM, cached 300 s per instance. A missing parameter reads as None."""
    global _ssm, _discovery
    now = time.monotonic()
    if _discovery and now - _discovery[0] <= _DISCOVERY_SECONDS:
        return _discovery[1]
    if _ssm is None:
        _ssm = boto3.client("ssm")
    prefix = f"/illuminate/{os.environ.get('ENVIRONMENT', 'dev')}"

    def read(name: str) -> str | None:
        try:
            return _ssm.get_parameter(Name=f"{prefix}/{name}")["Parameter"]["Value"]
        except _ssm.exceptions.ParameterNotFound:
            return None

    result = (read("mcp-client-id"), read("mcp-url"))
    _discovery = (now, result)
    return result


def setup_snippets(url: str) -> dict[str, str]:
    return {
        "claude_desktop": url,
        "claude_code": f"claude mcp add --transport http illuminate {url}",
        "vscode": json.dumps({"servers": {"illuminate": {"type": "http", "url": url}}}, indent=2),
    }
