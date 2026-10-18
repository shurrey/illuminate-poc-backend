import os
import sys

import pytest  # noqa: E402

import overlay_store  # noqa: E402

REAL_LIST_OVERLAYS = overlay_store.list_overlays


@pytest.fixture(autouse=True)
def _no_overlay_store_calls(monkeypatch):
    """Route tests never reach DynamoDB; tests that need overlays patch or restore list_overlays themselves."""
    monkeypatch.setattr(overlay_store, "list_overlays", lambda tenant_id: [])
    handler = sys.modules.get("lambda_handler")
    if handler:
        handler._overlay_cache.clear()


_CHAT_TOOLS_MODULES = ("test_chat_tools", "test_warehouse_error_masking", "test_cdm_pii", "test_chat_engine",
                       "test_roles")


@pytest.fixture(autouse=True)
def _chat_tools_database(request, monkeypatch):
    """ChatTools tests build a GuardContext for fake tenants; give them the database the tests name."""
    if request.module.__name__.rpartition(".")[2] in _CHAT_TOOLS_MODULES:
        import snowflake_client
        monkeypatch.setattr(snowflake_client, "tenant_database", lambda tenant: "DB")
