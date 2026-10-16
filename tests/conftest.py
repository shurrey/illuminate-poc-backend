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
