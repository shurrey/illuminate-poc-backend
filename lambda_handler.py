"""
Illuminate Conversational Intelligence - API Proxy Lambda

Thin Lambda handler wrapping FastAPI/uvicorn via Lambda Web Adapter (LWA).
Uses chat_engine for all LLM orchestration and conversation_store for history.

Request flow:
    Frontend -> Lambda Function URL (RESPONSE_STREAM) -> LWA -> uvicorn/FastAPI
        -> chat_engine (Bedrock Converse) -> Snowflake / MCP tools
"""
import asyncio
import functools
import os
import json
import math
import logging
import re
import uuid
from pathlib import Path
from typing import Any, Optional

from fastapi import Body, FastAPI, HTTPException, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from semantic_layer.contract import QueryContract
import tenancy
from licenses import PREVIEW_REFUSAL, allows_learn
from tenancy import NOT_READY, Tenant, TenantNotReady
import roles

# JWT validation
from jose import jwt, JWTError
import requests as http_requests

# Configure logging
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("API-PROXY")
# The connector logs statement text and server messages at DEBUG and INFO, and those can carry row values.
logging.getLogger("snowflake.connector").setLevel(logging.WARNING)


# =============================================================================
# Post-processing PII filter — runs on EVERY response before returning to user
# =============================================================================

# Patterns for common PII types (programmatic, not prompt-dependent). Bare digit runs are not
# matched: query results are full of 9-10 digit counts and IDs.
_PII_PATTERNS = [
    (re.compile(r'\b\d{3}-\d{2}-\d{4}\b'), '[SSN REDACTED]'),                        # SSN with dashes
    (re.compile(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b'), '[EMAIL REDACTED]'),  # Email
    (re.compile(r'\(\d{3}\)\s*\d{3}-\d{4}\b'), '[PHONE REDACTED]'),                   # Phone (xxx) xxx-xxxx
    (re.compile(r'\b\d{3}\.\d{3}\.\d{4}\b'), '[PHONE REDACTED]'),                     # Phone xxx.xxx.xxxx
    (re.compile(r'\b\d{3}-\d{3}-\d{4}\b'), '[PHONE REDACTED]'),                       # Phone xxx-xxx-xxxx
    (re.compile(r'\b\d{4}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}\b'), '[CARD REDACTED]'),   # Credit card
]


def _json_safe(value):
    """jsonable_encoder output that json.dumps and browsers accept: bytes as hex, NaN and infinities as null."""
    def finite(v):
        if isinstance(v, float) and not math.isfinite(v):
            return None
        if isinstance(v, dict):
            return {k: finite(x) for k, x in v.items()}
        if isinstance(v, list):
            return [finite(x) for x in v]
        return v

    return finite(jsonable_encoder(value, custom_encoder={bytes: bytes.hex}))


def _scrub_pii(text: str) -> str:
    """Redact SSN, email, phone and card patterns from model text, before it is stored or returned."""
    for pattern, replacement in _PII_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


# =============================================================================
# Configuration from environment variables
# =============================================================================

USER_POOL_ID = os.environ.get("USER_POOL_ID", "")
USER_POOL_CLIENT_ID = os.environ.get("USER_POOL_CLIENT_ID", "")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
ALLOWED_ORIGINS = os.environ.get("ALLOWED_ORIGINS", "http://localhost:3000,http://localhost:5173")

# Data dictionary proxy
DATA_DICTIONARY_BASE_URL = "https://us.data.api.blackboard.com/api/v1/data/dictionary"
_dictionary_cache: dict[str, tuple[float, object]] = {}
DICTIONARY_CACHE_TTL = 3600  # 1 hour

# Input validation for Snowflake identifiers
_SAFE_IDENTIFIER = re.compile(r'^[A-Za-z0-9_]+$')


# =============================================================================
# JWT Token Validation (Cognito)
# =============================================================================

_jwks_cache: Optional[dict] = None
_jwks_cache_time: float = 0
JWKS_CACHE_TTL = 3600  # 1 hour


def _get_jwks() -> dict:
    """Get JWKS from Cognito (cached)."""
    global _jwks_cache, _jwks_cache_time
    import time

    if _jwks_cache and (time.time() - _jwks_cache_time) < JWKS_CACHE_TTL:
        return _jwks_cache

    jwks_url = f"https://cognito-idp.{AWS_REGION}.amazonaws.com/{USER_POOL_ID}/.well-known/jwks.json"
    response = http_requests.get(jwks_url, timeout=5)
    response.raise_for_status()

    _jwks_cache = response.json()
    _jwks_cache_time = time.time()
    return _jwks_cache


def _validate_token(token: str) -> Optional[dict]:
    """Validate a Cognito JWT token and return claims."""
    try:
        jwks = _get_jwks()
        unverified_header = jwt.get_unverified_header(token)
        kid = unverified_header["kid"]

        key = None
        for jwk in jwks["keys"]:
            if jwk["kid"] == kid:
                key = jwk
                break

        if not key:
            return None

        claims = jwt.decode(
            token,
            key,
            algorithms=["RS256"],
            issuer=f"https://cognito-idp.{AWS_REGION}.amazonaws.com/{USER_POOL_ID}",
            options={"verify_aud": False}
        )
        # Only ID tokens carry the tenant and group claims the API relies on.
        if claims.get("token_use") != "id" or claims.get("aud") != USER_POOL_CLIENT_ID:
            return None
        return claims

    except (JWTError, Exception):
        return None


def _get_user_from_token(authorization: Optional[str]) -> Optional[dict]:
    """Extract and validate user from Authorization header."""
    if not authorization:
        return None

    parts = authorization.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None

    return _validate_token(parts[1])


def _tenant_id_from_user(user: Optional[dict]) -> Optional[str]:
    """The user's `custom:tenant_id` claim (ID tokens only); None for access tokens or users without it."""
    if not user:
        return None
    value = user.get("custom:tenant_id")
    return value.strip() if isinstance(value, str) and value.strip() else None


def tenant_for(user: Optional[dict]) -> Tenant:
    """The caller's registered tenant, failing closed with a 403 when the token has no tenant claim,
    the tenant has no registry row, or its Snowflake secret or database is missing."""
    import snowflake_client

    tenant_id = _tenant_id_from_user(user)
    if not tenant_id:
        raise HTTPException(status_code=403, detail=NOT_READY)
    try:
        tenant = tenancy.get_tenant(tenant_id)
        snowflake_client.tenant_database(tenant)
    except TenantNotReady:
        raise HTTPException(status_code=403, detail=NOT_READY)
    return tenant


# =============================================================================
# Request/Response Models
# =============================================================================

class ChatRequest(BaseModel):
    """Chat request model - supports both simple and A2A formats."""
    message: Optional[str] = None
    context_id: Optional[str] = None
    request_id: Optional[str] = None
    jsonrpc: Optional[str] = None
    method: Optional[str] = None
    params: Optional[dict] = None
    id: Optional[str] = None

    def get_message_text(self) -> str:
        if self.message:
            return self.message
        if self.params and "message" in self.params:
            msg = self.params["message"]
            if "parts" in msg and msg["parts"]:
                for part in msg["parts"]:
                    if part.get("type") == "text":
                        return part.get("text", "")
        return ""

    def get_context_id(self) -> Optional[str]:
        if self.context_id:
            return self.context_id
        if self.params and "message" in self.params:
            return self.params["message"].get("contextId")
        return None

    def get_request_id(self) -> Optional[str]:
        if self.request_id:
            return self.request_id
        if self.id:
            return self.id
        if self.params and "message" in self.params:
            return self.params["message"].get("messageId")
        return None


class ChatResponse(BaseModel):
    """Chat response model."""
    text: str
    artifacts: list = []
    context_id: Optional[str] = None
    sources: Optional[list] = None


class HealthResponse(BaseModel):
    """Health check response."""
    status: str
    version: str
    mode: str = "proxy"


# =============================================================================
# Chat Engine Wrappers
# =============================================================================


def _owner(user: dict, tenant: Tenant) -> str:
    """Conversation owner key. Tenant-qualified so the same sub in two tenants never shares history."""
    return f"{tenant.tenant_id}#{user['sub']}"


def _conversation_id(requested: Optional[str], owner: str) -> str:
    """The caller's conversation id: theirs if they own it or it is unused, otherwise a new one."""
    from conversation_store import exists, owns

    if requested and (owns(requested, owner) or not exists(requested)):
        return requested
    return uuid.uuid4().hex


def _queries_from(artifacts: list[dict]) -> list[dict]:
    """What each answer ran, kept with the turn so follow-ups can modify it. An artifact with no kind is stored
    as SQL, which claims no governance its provenance does not state."""
    out = []
    for a in artifacts:
        if a.get("type") != "sql":
            continue
        provenance = a.get("provenance") or {}
        governed = provenance.get("governed") is True
        if a.get("kind") == "semantic":
            out.append({"kind": "semantic", "title": a.get("title"), "governed": governed, "query": a.get("query")})
        else:
            out.append({"kind": "sql", "title": a.get("title"), "governed": governed,
                        "governance": provenance.get("governance", "none"), "sql": a.get("data"),
                        "governed_parts": a.get("governed_parts") or {}})
    return out


def _model_turns(history: list[dict], message_text: str) -> tuple[list[dict], str]:
    """Converse messages for the history, and the new user text.

    The queries behind each answer are replayed at the start of the user turn that follows it, so
    the model can modify them without its own earlier answers showing the JSON to copy.
    """
    messages, pending = [], None
    for msg in history:
        text = msg["content"] if msg["content"].strip() else "(no answer)"
        if msg["role"] == "user" and pending:
            text = _with_queries(text, pending)
        pending = msg.get("queries") if msg["role"] == "assistant" else None
        messages.append({"role": msg["role"], "content": [{"text": text}]})
    return messages, _with_queries(message_text, pending) if pending else message_text


def _with_queries(text: str, queries: list[dict]) -> str:
    # Turns stored before queries carried a kind: only semantic ones have a query contract.
    queries = [q if "kind" in q else {"kind": "semantic" if "query" in q else "sql", **q} for q in queries]
    return f"<previous_queries>{json.dumps(queries)}</previous_queries>\n\n{text}"


def _engine_kwargs(tenant: Tenant, user: dict) -> dict:
    """The chat engine's tools and system prompt over the tenant's overlaid catalog and database."""
    from semantic_layer.chat_tools import ChatTools
    from semantic_layer.prompt import build_system_prompt

    from roles import role_allows_identity

    catalog, overlays = _catalog_for(tenant)
    database = _database_for(tenant)
    allow_identity = role_allows_identity(user)

    tools = ChatTools(catalog, tenant=tenant, overlays=overlays, allow_identity=allow_identity,
                      identity_audit=_identity_audit(user, catalog))
    return {"tools": tools,
            "system_prompt": build_system_prompt(catalog, database, learn=allows_learn(tenant),
                                            allow_identity=allow_identity)}


async def send_message(
    message_text: str,
    owner: str,
    context_id: Optional[str] = None,
    *,
    engine_kwargs: dict,
) -> dict:
    """Send a message via chat_engine (non-streaming). engine_kwargs: from `_engine_kwargs`."""
    from chat_engine import send_message as engine_send
    from conversation_store import load_history, save_turn

    context_id = _conversation_id(context_id, owner)
    bedrock_history, model_text = _model_turns(load_history(context_id, owner), message_text)

    loop = asyncio.get_event_loop()
    response_text, _, artifacts = await loop.run_in_executor(
        None, lambda: engine_send(model_text, bedrock_history, **engine_kwargs)
    )
    response_text = _scrub_pii(response_text)

    save_turn(context_id, owner, message_text, response_text, _queries_from(artifacts))

    return {"text": response_text, "artifacts": artifacts, "contextId": context_id}


async def send_message_streaming(
    message_text: str,
    owner: str,
    context_id: Optional[str] = None,
    *,
    engine_kwargs: dict,
):
    """Stream a response via chat_engine, yielding frontend events. engine_kwargs: from `_engine_kwargs`."""
    from chat_engine import send_message_streaming as engine_stream
    from conversation_store import load_history, save_turn

    yield {"type": "status", "message": "Processing your question..."}

    # DynamoDB calls are blocking; keep them off the event loop that serves the stream.
    loop = asyncio.get_running_loop()
    context_id = await loop.run_in_executor(None, _conversation_id, context_id, owner)
    history = await loop.run_in_executor(None, load_history, context_id, owner)
    bedrock_history, model_text = _model_turns(history, message_text)

    full_text, artifacts = "", []
    try:
        async for event in engine_stream(model_text, bedrock_history, **engine_kwargs):
            if event["type"] == "status":
                yield event
            elif event["type"] == "raw_complete":
                full_text, artifacts = _scrub_pii(event["text"]), event["artifacts"]
                if full_text.strip():
                    await loop.run_in_executor(
                        None, save_turn, context_id, owner, message_text, full_text, _queries_from(artifacts))

        if not full_text.strip():
            yield {"type": "error", "message": "Empty response"}
            return

        yield {
            "type": "complete",
            "data": {
                "text": full_text,
                "artifacts": artifacts,
                "contextId": context_id,
            },
        }

    except TenantNotReady:
        yield {"type": "error", "message": NOT_READY}
    except Exception as e:
        logger.error(f"Chat engine error: {e}")
        yield {"type": "error", "message": str(e)}


# =============================================================================
# FastAPI Application
# =============================================================================

_API_DOCS = os.environ.get("API_DOCS", "on") == "on"
app = FastAPI(
    title="Illuminate Conversational Intelligence - API",
    description="Semantic-layer queries, catalog, admin overlays and Bedrock chat",
    version="0.4.0",
    docs_url="/docs" if _API_DOCS else None,
    redoc_url="/redoc" if _API_DOCS else None,
    openapi_url="/openapi.json" if _API_DOCS else None,
)



@app.exception_handler(TenantNotReady)
async def _tenant_not_ready(request: Request, exc: TenantNotReady) -> JSONResponse:
    """A tenant with no secret or database is unusable; the body never names the tenant."""
    return JSONResponse(status_code=403, content={"detail": NOT_READY})


# CORS middleware
origins = [o.strip() for o in ALLOWED_ORIGINS.split(",")]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "If-None-Match"],
    expose_headers=["ETag"],
)

# Track cancelled request IDs
_cancelled_requests: set[str] = set()
# request_id -> Cognito sub of the user streaming it. Per Lambda instance, so a cancel that
# lands on a different instance than its stream finds nothing and returns 404.
_request_owners: dict[str, str] = {}


@app.get("/health", response_model=HealthResponse)
async def health_check():
    """Health check endpoint."""
    return HealthResponse(
        status="healthy",
        version="0.4.0",
        mode="chat_engine"
    )


@app.get("/api/v1/me")
async def get_me(authorization: Optional[str] = Header(None)):
    """Tenant and roles for the caller."""
    user = _get_user_from_token(authorization)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    tenant = tenant_for(user)
    groups = set(user.get("cognito:groups") or [])

    user_roles = sorted([role for group, role in roles.GROUP_ROLES.items() if group in groups])
    if not user_roles:
        user_roles = [roles.role_of(user)]

    await _blocking(_record_in_directory, tenant.tenant_id, user)

    return {
        "tenant": {"name": tenant.name, "license": tenant.license},
        "roles": user_roles,
        "is_admin": ADMIN_GROUP in groups,
        "mcp_enabled": tenant.mcp_enabled,
    }


def _record_in_directory(tenant_id: str, user: dict) -> None:
    """Best-effort: a directory failure is logged and never fails the caller's request."""
    import user_items_store

    email = user.get("email") or ""
    name = user.get("name") or email.split("@")[0]
    if not (user.get("sub") and name):
        return
    try:
        user_items_store.upsert_directory(tenant_id, user["sub"], name, email)
    except Exception:
        logger.warning("directory upsert failed", exc_info=True)


MAX_DIRECTORY_RESULTS = 25
MAX_DIRECTORY_QUERY = 100


@app.get("/api/v1/directory")
async def search_directory(q: str = "", authorization: Optional[str] = Header(None)) -> dict:
    """Other users of the caller's tenant whose name or email contains q (case-insensitive), by name, at most 25."""
    import user_items_store

    tenant_id, sub = _cards_caller(authorization)
    needle = q.strip()[:MAX_DIRECTORY_QUERY].casefold()

    def search() -> dict:
        found = [
            {"sub": e["sub"], "name": e["name"], "email": e["email"]}
            for e in user_items_store.list_directory(tenant_id)
            if e["sub"] != sub and needle in f"{e['name']}\n{e['email']}".casefold()
        ]
        found.sort(key=lambda e: (e["name"].casefold(), e["sub"]))
        return {"users": found[:MAX_DIRECTORY_RESULTS]}

    return await _blocking(search)


CARD_FORMATS = ("number", "ratio", "percent")
MAX_CUSTOM_CARDS = 100
_CARD_ID = re.compile(r"c_[0-9a-f]{12}")


@functools.lru_cache(maxsize=1)
def _default_cards() -> list[dict]:
    path = Path(__file__).resolve().parent / "semantic_layer" / "data" / "default_cards.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _bad_request(*errors: str) -> HTTPException:
    return HTTPException(status_code=400, detail={"errors": list(errors)})


MAX_CONTRACT_BYTES = 16 * 1024
# Well under DynamoDB's 400 KB item limit, so an oversized body is a 400 here rather than a store error.
MAX_BODY_BYTES = 64 * 1024


def _size_errors(body: dict) -> list[str]:
    if len(json.dumps(body, ensure_ascii=False).encode()) > MAX_BODY_BYTES:
        return [f"this item is too large to save (at most {MAX_BODY_BYTES // 1024} KB)"]
    return []


def _contract_errors(contract) -> list[str]:
    from pydantic import ValidationError

    if not isinstance(contract, dict):
        return ["contract must be an object"]
    if len(json.dumps(contract)) > MAX_CONTRACT_BYTES:
        return [f"contract must be at most {MAX_CONTRACT_BYTES // 1024} KB"]
    try:
        QueryContract.model_validate(contract)
    except ValidationError as e:
        return [f"contract: {err['msg']}" for err in e.errors()]
    return []


def _card_body(payload) -> dict:
    """The validated card fields, or a 400 listing every problem."""
    if not isinstance(payload, dict):
        raise _bad_request("body must be an object")
    errors = []
    for field, limit in (("label", 200), ("description", 4000), ("longDescription", 4000), ("prompt", 4000)):
        value = payload.get(field)
        if not isinstance(value, str):
            errors.append(f"{field} is required")
        elif field == "label" and not value.strip():
            errors.append("label is required")
        elif len(value) > limit:
            errors.append(f"{field} must be at most {limit} characters")
    if payload.get("format") not in CARD_FORMATS:
        errors.append(f"format must be one of {', '.join(CARD_FORMATS)}")
    link = payload.get("reportLink")
    if link is not None and (not isinstance(link, str) or len(link) > 500 or not link.startswith("/")
                             or link.startswith("//") or "\\" in link):
        errors.append("reportLink must be an app-relative path starting with a single / (at most 500 characters)")
    errors += _contract_errors(payload.get("contract"))
    if errors:
        raise _bad_request(*errors)
    body = {k: payload[k] for k in ("label", "description", "longDescription", "prompt", "contract", "format")}
    if link is not None:
        body["reportLink"] = link
    errors = _size_errors(body)
    if errors:
        raise _bad_request(*errors)
    return body


def _custom_card(item: dict) -> dict:
    return {**{k: v for k, v in item.items() if k not in ("version", "updated_at")}, "isBuiltIn": False, "enabled": True}


def _load_layout(tenant_id: str, sub: str) -> tuple[dict, int]:
    import user_items_store

    item = user_items_store.get(tenant_id, sub, "layout")
    if item is None:
        return {"order": [], "disabled": [], "hidden": []}, 0
    return {k: list(item.get(k, [])) for k in ("order", "disabled", "hidden")}, item["version"]


_CARDS_CONFLICT = "Your cards changed elsewhere; reload and try again"


def _save_layout(tenant_id: str, sub: str, layout: dict, version: int) -> None:
    import user_items_store

    try:
        user_items_store.put(tenant_id, sub, "layout", None, layout, expected_version=version or None)
    except user_items_store.Conflict:
        raise HTTPException(status_code=409, detail=_CARDS_CONFLICT)


def _mutate_layout(tenant_id: str, sub: str, mutate) -> None:
    """Read, mutate(layout, version) and save the layout, re-reading on a concurrent write (up to 3 attempts).
    Only for edits that commute with other edits; raises 409 when every attempt loses the race."""
    for attempt in range(3):
        layout, version = _load_layout(tenant_id, sub)
        mutate(layout, version)
        try:
            _save_layout(tenant_id, sub, layout, version)
            return
        except HTTPException as e:
            if e.status_code != 409 or attempt == 2:
                raise


MAX_PINNED_SHARED = 50
_SHARE_ID = re.compile(r"sh_[0-9a-f]{16}")


def _pinned_card(tenant_id: str, sub: str, share_id: str, allow_identity: bool) -> Optional[dict]:
    """The live card behind a pinned share, shaped like a home card with the share id as its id; None unless the
    caller is currently a member of a card share. Without identity rights filter values and the prompt are hidden."""
    from semantic_layer.redact import redact_contract

    try:
        share, item, owner = _shared_item(tenant_id, sub, share_id)
    except HTTPException:
        return None
    if share["item_kind"] != "card":
        return None
    card = {k: item[k] for k in ("label", "description", "longDescription", "prompt", "contract", "format", "reportLink")
            if k in item}
    if not allow_identity:
        card.pop("prompt", None)
        card["contract"] = redact_contract(card.get("contract"))
    return {**card, "id": share_id, "shared": True, "owner": {"name": owner}, "isBuiltIn": False, "enabled": True}


def _cards_view(tenant_id: str, sub: str, allow_identity: bool = False) -> dict:
    """Built-ins merged with the user's custom cards in layout order; an unlisted built-in or card is appended.
    hidden lists the hidden built-ins in default order and disabled the disabled cards in layout order.
    Pinned shared cards resolve live and are omitted, without touching the layout, once the share is gone."""
    import user_items_store

    layout, version = _load_layout(tenant_id, sub)
    by_id = {c["id"]: {**c, "isBuiltIn": True} for c in _default_cards() if c["id"] not in layout["hidden"]}
    shared = _share_summaries(tenant_id, sub, "card")
    for item in user_items_store.list_kind(tenant_id, sub, "card"):
        by_id[item["id"]] = {**_custom_card(item), "shared": shared.get(item["id"], _NOT_SHARED)}
    for share_id in dict.fromkeys(i for i in layout["order"] if _SHARE_ID.fullmatch(i)):
        card = _pinned_card(tenant_id, sub, share_id, allow_identity)
        if card:
            by_id[share_id] = card
    ordered = [i for i in dict.fromkeys(layout["order"]) if i in by_id]
    ordered += [i for i in by_id if i not in ordered]
    return {
        "cards": [{**by_id[i], "enabled": i not in layout["disabled"]} for i in ordered],
        "hidden": [c["id"] for c in _default_cards() if c["id"] in layout["hidden"]],
        "disabled": [i for i in ordered if i in layout["disabled"]],
        "version": version,
    }


def _caller(authorization: Optional[str]) -> tuple[dict, Tenant]:
    """The authenticated user and their registered tenant."""
    user = _get_user_from_token(authorization)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    return user, tenant_for(user)


def _cards_caller(authorization: Optional[str]) -> tuple[str, str]:
    user = _get_user_from_token(authorization)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    return tenant_for(user).tenant_id, user["sub"]


def _cards_viewer(authorization: Optional[str]) -> tuple[str, str, bool]:
    from roles import role_allows_identity

    user, tenant = _caller(authorization)
    return tenant.tenant_id, user["sub"], role_allows_identity(user)


async def _blocking(fn, *args):
    return await asyncio.get_running_loop().run_in_executor(None, fn, *args)


@app.get("/api/v1/me/cards")
async def get_my_cards(authorization: Optional[str] = Header(None)) -> dict:
    """The caller's home cards in layout order, built-ins included."""
    tenant_id, sub, identity = _cards_viewer(authorization)
    return await _blocking(_cards_view, tenant_id, sub, identity)


@app.put("/api/v1/me/cards/layout")
async def put_my_card_layout(payload: dict, authorization: Optional[str] = Header(None)) -> dict:
    """Replace order, disabled and hidden; 400 on ids that are not built-in or the caller's own, 409 if stale."""
    import user_items_store

    tenant_id, sub, identity = _cards_viewer(authorization)
    version = payload.get("expected_version")
    lists = {k: payload.get(k) for k in ("order", "disabled", "hidden")}
    if isinstance(version, bool) or not isinstance(version, int) or version < 0:
        raise _bad_request("expected_version must be a non-negative integer")
    if any(not isinstance(v, list) or any(not isinstance(i, str) for i in v) for v in lists.values()):
        raise _bad_request("order, disabled and hidden must be lists of card ids")
    cap = len(_default_cards()) + MAX_CUSTOM_CARDS
    if any(sum(1 for i in v if not _SHARE_ID.fullmatch(i)) > cap for v in lists.values()):
        raise _bad_request(f"order, disabled and hidden can each list at most {cap} cards")
    lists = {k: list(dict.fromkeys(v)) for k, v in lists.items()}

    def save() -> dict:
        builtin = {c["id"] for c in _default_cards()}
        own = {i["id"] for i in user_items_store.list_kind(tenant_id, sub, "card")}
        pins = {i for k in ("order", "disabled") for i in lists[k] if _SHARE_ID.fullmatch(i)}
        if len(pins) > MAX_PINNED_SHARED:
            raise _bad_request(f"You can pin at most {MAX_PINNED_SHARED} shared cards")
        live = {i for i in pins if _pinned_card(tenant_id, sub, i, identity)}
        # A pin already in the stored layout may have been unshared since the client loaded it.
        stored, _ = _load_layout(tenant_id, sub)
        dead = (pins - live) & set(stored["order"] + stored["disabled"])
        for k in lists:
            lists[k] = [i for i in lists[k] if i not in dead]
        own |= live
        unknown = sorted({i for v in lists.values() for i in v} - builtin - own)
        not_builtin = sorted(set(lists["hidden"]) - builtin)
        if unknown or not_builtin:
            raise _bad_request(*[f"unknown card id {i}" for i in unknown],
                               *[f"only built-in cards can be hidden: {i}" for i in not_builtin if i not in unknown])
        _save_layout(tenant_id, sub, lists, version)
        return _cards_view(tenant_id, sub, identity)

    return await _blocking(save)


@app.post("/api/v1/me/cards/reset")
async def reset_my_cards(authorization: Optional[str] = Header(None)) -> dict:
    """Unhide and enable the built-ins in their original order, followed by the caller's custom cards."""
    import user_items_store

    tenant_id, sub, identity = _cards_viewer(authorization)

    def reset() -> dict:
        builtin = [c["id"] for c in _default_cards()]
        custom = [i["id"] for i in user_items_store.list_kind(tenant_id, sub, "card")]

        def apply(layout: dict, version: int) -> None:
            keep = custom + [i for i in dict.fromkeys(layout["order"]) if _SHARE_ID.fullmatch(i)]
            order = builtin + [i for i in dict.fromkeys(layout["order"]) if i in keep]
            order += [i for i in keep if i not in order]
            layout.update(order=order, disabled=[i for i in layout["disabled"] if i in keep], hidden=[])

        _mutate_layout(tenant_id, sub, apply)
        return _cards_view(tenant_id, sub, identity)

    return await _blocking(reset)


@app.post("/api/v1/me/cards")
async def create_my_card(payload: dict, authorization: Optional[str] = Header(None)) -> dict:
    """Create a custom card with a server-generated id and append it to the layout; 400 at 100 cards."""
    tenant_id, sub = _cards_caller(authorization)
    body = _card_body(payload)
    return await _blocking(_create_card, tenant_id, sub, body)


def _create_card(tenant_id: str, sub: str, body: dict) -> dict:
    """Save a validated card body under a new id and append it to the layout; 400 at 100 cards."""
    import user_items_store

    if user_items_store.count(tenant_id, sub, "card") >= MAX_CUSTOM_CARDS:
        raise _bad_request(f"You can have at most {MAX_CUSTOM_CARDS} custom cards")
    card_id = f"c_{uuid.uuid4().hex[:12]}"
    saved = user_items_store.put(tenant_id, sub, "card", card_id, body)

    def append(layout: dict, version: int) -> None:
        if not version:
            layout["order"] = [c["id"] for c in _default_cards()]
        layout["order"].append(card_id)

    try:
        _mutate_layout(tenant_id, sub, append)
    except HTTPException:
        user_items_store.delete(tenant_id, sub, "card", card_id)
        raise
    return _custom_card(saved)


@app.put("/api/v1/me/cards/{card_id}")
async def update_my_card(card_id: str, payload: dict, authorization: Optional[str] = Header(None)) -> dict:
    """Update one of the caller's own custom cards: 400 for a built-in, 404 for any other id."""
    import user_items_store

    tenant_id, sub = _cards_caller(authorization)
    if card_id in {c["id"] for c in _default_cards()}:
        raise _bad_request("Built-in cards cannot be edited")
    body = _card_body(payload)

    def update() -> dict:
        existing = user_items_store.get(tenant_id, sub, "card", card_id) if _CARD_ID.fullmatch(card_id) else None
        if existing is None:
            raise HTTPException(status_code=404, detail="Card not found")
        try:
            return _custom_card(user_items_store.put(
                tenant_id, sub, "card", card_id, body, expected_version=existing["version"]))
        except user_items_store.Conflict:
            raise HTTPException(status_code=409, detail="This card changed elsewhere; reload and try again")

    return await _blocking(update)


@app.delete("/api/v1/me/cards/{card_id}")
async def delete_my_card(card_id: str, authorization: Optional[str] = Header(None)) -> dict:
    """Delete a custom card, or hide a built-in; 404 for any other id."""
    import user_items_store

    tenant_id, sub, identity = _cards_viewer(authorization)

    def remove() -> dict:
        def hide(layout: dict, version: int) -> None:
            if card_id not in layout["hidden"]:
                layout["hidden"].append(card_id)

        def forget(layout: dict, version: int) -> None:
            for key in layout:
                layout[key] = [i for i in layout[key] if i != card_id]

        if card_id in {c["id"] for c in _default_cards()}:
            _mutate_layout(tenant_id, sub, hide)
        elif _SHARE_ID.fullmatch(card_id) and any(
                card_id in _load_layout(tenant_id, sub)[0][k] for k in ("order", "disabled")):
            _mutate_layout(tenant_id, sub, forget)
        elif _CARD_ID.fullmatch(card_id) and _delete_item_and_share(tenant_id, sub, "card", card_id):
            _mutate_layout(tenant_id, sub, forget)
        else:
            raise HTTPException(status_code=404, detail="Card not found")
        return _cards_view(tenant_id, sub, identity)

    return await _blocking(remove)


@app.post("/api/v1/me/cards/pin-shared")
async def pin_shared_card(payload: dict, authorization: Optional[str] = Header(None)) -> dict:
    """Put a card shared with the caller on their home page (idempotent); 404 unless they are a current member of a
    card share, 400 at 50 pinned."""
    tenant_id, sub, identity = _cards_viewer(authorization)
    share_id = payload.get("share_id")

    def pin() -> dict:
        if not isinstance(share_id, str) or _pinned_card(tenant_id, sub, share_id, identity) is None:
            raise _not_shared()

        def append(layout: dict, version: int) -> None:
            dead = {i for i in layout["order"] + layout["disabled"]
                    if _SHARE_ID.fullmatch(i) and i != share_id and _pinned_card(tenant_id, sub, i, identity) is None}
            for key in ("order", "disabled"):
                layout[key] = [i for i in layout[key] if i not in dead]
            if share_id in layout["order"]:
                return
            if sum(1 for i in layout["order"] if _SHARE_ID.fullmatch(i)) >= MAX_PINNED_SHARED:
                raise _bad_request(f"You can pin at most {MAX_PINNED_SHARED} shared cards")
            if not version:
                layout["order"] = [c["id"] for c in _default_cards()]
            layout["order"].append(share_id)

        _mutate_layout(tenant_id, sub, append)
        return _cards_view(tenant_id, sub, identity)

    return await _blocking(pin)


MAX_QUERIES = 500
MAX_PREF_IDS = 200
MAX_RECENT_REPORTS = 5
WIDGETS = ("ai", "kpis", "feed", "quickaccess")
_QUERY_ID = re.compile(r"q_[0-9a-f]{12}")


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _iso_or(value, default):
    from datetime import datetime

    if isinstance(value, str) and len(value) <= 64:
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
            return value
        except ValueError:
            pass
    return default


def _query_body(payload) -> dict:
    """The validated saved-query fields, or a 400 listing every problem."""
    if not isinstance(payload, dict):
        raise _bad_request("body must be an object")
    errors = []
    for field, limit in (("name", 200), ("prompt", 4000), ("description", 4000)):
        value = payload.get(field)
        if not isinstance(value, str):
            errors.append(f"{field} is required")
        elif field == "name" and not value.strip():
            errors.append("name is required")
        elif len(value) > limit:
            errors.append(f"{field} must be at most {limit} characters")
    errors += _contract_errors(payload.get("contract"))
    if errors:
        raise _bad_request(*errors)
    body = {k: payload[k] for k in ("name", "prompt", "description", "contract")}
    errors = _size_errors(body)
    if errors:
        raise _bad_request(*errors)
    return body


QUERY_KINDS = ("governed", "sql")
_KIND_FIELDS = ("prompt", "contract", "sql", "governed_parts", "owner_name", "governance_hint")
MAX_SQL_CHARS = 20_000
DEFAULT_REASON = "Written in the query builder"


def _sql_query_body(payload: dict, user: dict, tenant: Tenant) -> dict:
    """The validated SQL query fields, checked against the guard as `user` (the saver); 400 on any problem."""
    from semantic_layer.sql_service import validate_sql

    errors = []
    name, sql = payload.get("name"), payload.get("sql")
    description = "" if payload.get("description") is None else payload["description"]
    parts = payload.get("governed_parts", {})
    if not isinstance(name, str) or not name.strip():
        errors.append("name is required")
    elif len(name) > 200:
        errors.append("name must be at most 200 characters")
    if not isinstance(description, str):
        errors.append("description must be a string")
    elif len(description) > 4000:
        errors.append("description must be at most 4000 characters")
    if not isinstance(sql, str) or not sql.strip():
        errors.append("sql is required")
    elif len(sql) > MAX_SQL_CHARS:
        errors.append(f"sql must be at most {MAX_SQL_CHARS} characters")
    if not isinstance(parts, dict):
        errors.append("governed_parts must be an object")
    else:
        for part_name, part in parts.items():
            if not isinstance(part, dict):
                errors.append(f"{part_name}: a governed part must be an object")
            elif len(json.dumps(part)) > MAX_CONTRACT_BYTES:
                errors.append(f"{part_name}: must be at most {MAX_CONTRACT_BYTES // 1024} KB")
    if errors:
        raise _bad_request(*errors)
    checked = validate_sql(guard_context(tenant, user), sql=sql, governed=parts or None)
    if not checked["ok"]:
        raise _bad_request(checked["error"])
    from semantic_layer.sql_service import contract_of

    parts = {n: contract_of(p).model_dump(mode="json", exclude_unset=True) for n, p in parts.items()}
    body = {"name": name, "description": description, "query_kind": "sql", "sql": sql, "governed_parts": parts,
            "governance_hint": _sql_hint(checked["governance_preview"])}
    owner = user.get("name") or user.get("email")
    if isinstance(owner, str) and owner.strip():
        body["owner_name"] = owner.strip()[:200]
    errors = _size_errors(body)
    if errors:
        raise _bad_request(*errors)
    return body


def _saved_query_body(payload, user: dict, tenant: Tenant) -> dict:
    """Governed or SQL fields per `query_kind` (default governed); other-kind fields are not kept."""
    if not isinstance(payload, dict):
        raise _bad_request("body must be an object")
    kind = payload.get("query_kind", "governed")
    if kind not in QUERY_KINDS:
        raise _bad_request(f"query_kind must be one of {', '.join(QUERY_KINDS)}")
    return _sql_query_body(payload, user, tenant) if kind == "sql" else {**_query_body(payload), "query_kind": "governed"}


def _sql_hint(preview: str) -> str:
    """An SQL query's list hint from its validate preview; at most "partial", since a run can be truncated."""
    return "ungoverned" if preview == "none" else "partial"


def _governance_hint(item: dict) -> str:
    """A label guess for lists only; the run's provenance is the real one. An SQL query's is never "governed"."""
    if item["query_kind"] != "sql":
        return "governed"
    if item.get("governance_hint") == "ungoverned":
        return "ungoverned"
    return "partial" if item.get("governed_parts") else "ungoverned"


def _stored_query(item: dict) -> dict:
    """The query's body fields; rows saved before the rename carry `kind` instead of `query_kind`."""
    body = {k: v for k, v in item.items() if k not in ("id", "version", "updated_at", "kind")}
    body["query_kind"] = item.get("query_kind") or item.get("kind") or "governed"
    return body


def _query_view(item: dict) -> dict:
    body = _stored_query(item)
    return {**body, "id": item["id"], "governance_hint": _governance_hint(body)}


def _query_item(tenant_id: str, sub: str, query_id: str) -> dict:
    import user_items_store

    item = user_items_store.get(tenant_id, sub, "query", query_id) if _QUERY_ID.fullmatch(query_id) else None
    if item is None:
        raise HTTPException(status_code=404, detail="Query not found")
    return item


def _replace_query(tenant_id: str, sub: str, query_id: str, change) -> dict:
    """Apply change(body) to the stored query, re-reading on a concurrent write (up to 3 attempts)."""
    import user_items_store

    for attempt in range(3):
        item = _query_item(tenant_id, sub, query_id)
        body = _stored_query(item)
        change(body)
        try:
            return _query_view(user_items_store.put(
                tenant_id, sub, "query", query_id, body, expected_version=item["version"]))
        except user_items_store.Conflict:
            if attempt == 2:
                raise HTTPException(status_code=409, detail="This query changed elsewhere; reload and try again")


@app.get("/api/v1/me/queries")
async def get_my_queries(authorization: Optional[str] = Header(None)) -> dict:
    """The caller's saved queries, newest first."""
    import user_items_store

    tenant_id, sub = _cards_caller(authorization)

    def load() -> dict:
        items = user_items_store.list_kind(tenant_id, sub, "query")
        items.sort(key=lambda i: i.get("createdAt") or "", reverse=True)
        shared = _share_summaries(tenant_id, sub, "query")
        return {"queries": [{**_query_view(i), "shared": shared.get(i["id"], _NOT_SHARED)} for i in items]}

    return await _blocking(load)


@app.post("/api/v1/me/queries")
async def create_my_query(payload: dict, authorization: Optional[str] = Header(None)) -> dict:
    """Save a governed or SQL query with a server-generated id; 400 at 500 queries."""
    user, tenant = _caller(authorization)
    return await _blocking(_create_query, payload, user, tenant)


def _create_query(payload, user: dict, tenant: Tenant) -> dict:
    """Validate the payload as `user` and save it to their profile under a new id; 400 at 500 queries."""
    import user_items_store

    body = {**_saved_query_body(payload, user, tenant), "createdAt": _now_iso(), "lastUsedAt": None}
    tenant_id, sub = tenant.tenant_id, user["sub"]
    if user_items_store.count(tenant_id, sub, "query") >= MAX_QUERIES:
        raise _bad_request(f"You can save at most {MAX_QUERIES} queries")
    return _query_view(user_items_store.put(tenant_id, sub, "query", f"q_{uuid.uuid4().hex[:12]}", body))


@app.put("/api/v1/me/queries/{query_id}")
async def update_my_query(query_id: str, payload: dict, authorization: Optional[str] = Header(None)) -> dict:
    """Replace the content of one of the caller's queries; its dates are kept and the other kind's fields dropped."""
    user, tenant = _caller(authorization)

    def update() -> dict:
        body = _saved_query_body(payload, user, tenant)

        def change(stored: dict) -> None:
            for field in _KIND_FIELDS:
                stored.pop(field, None)
            stored.update(body)

        return _replace_query(tenant.tenant_id, user["sub"], query_id, change)

    return await _blocking(update)


@app.post("/api/v1/me/queries/{query_id}/run")
async def run_my_query(query_id: str, authorization: Optional[str] = Header(None)):
    """Run a saved query as the caller, whoever saved it; same response as /semantic/query or /sql/run."""
    user, tenant = _caller(authorization)
    item = await _blocking(_query_item, tenant.tenant_id, user["sub"], query_id)
    stored = _stored_query(item)
    owner = _text(stored, "owner_name")
    reason = _text(stored, "description") or (f"Written by {owner}" if owner else DEFAULT_REASON)
    return await _run_stored_query(item, user, tenant, reason, owner_writes=True)


def _text(stored: dict, field: str) -> str:
    value = stored.get(field)
    return value.strip() if isinstance(value, str) else ""


async def _run_contract(contract, user: dict, tenant: Tenant) -> JSONResponse:
    """Run a stored contract as /semantic/query does; 400 when it no longer validates."""
    try:
        parsed = QueryContract.model_validate(contract)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"This saved query is not valid: {e}")
    compiled = await _blocking(_compile_for, parsed, user, tenant)
    return _semantic_response(compiled, await _execute(compiled, user, tenant))


async def _run_stored_query(item: dict, user: dict, tenant: Tenant, reason: str, *, owner_writes: bool):
    """Run a stored query row as `user`. owner_writes records lastUsedAt, so only pass it when `user` owns the row."""
    stored = _stored_query(item)
    if stored["query_kind"] == "sql":
        sql = stored.get("sql") if isinstance(stored.get("sql"), str) else ""
        parts = stored.get("governed_parts")
        response = await _run_sql_for(user, tenant, sql, parts if isinstance(parts, dict) and parts else None,
                                      reason[:500])
    else:
        response = await _run_contract(stored.get("contract"), user, tenant)
    if owner_writes:
        now = _now_iso()
        try:
            await _blocking(_replace_query, tenant.tenant_id, user["sub"], item["id"],
                            lambda b: b.update(lastUsedAt=now))
        except HTTPException as e:
            logger.warning("Could not record lastUsedAt for saved query %s: %s", item["id"], e.detail)
    return response


@app.post("/api/v1/me/queries/{query_id}/used")
async def mark_my_query_used(query_id: str, authorization: Optional[str] = Header(None)) -> dict:
    """Set lastUsedAt to now."""
    tenant_id, sub = _cards_caller(authorization)
    now = _now_iso()
    return await _blocking(_replace_query, tenant_id, sub, query_id, lambda b: b.update(lastUsedAt=now))


@app.delete("/api/v1/me/queries/{query_id}")
async def delete_my_query(query_id: str, authorization: Optional[str] = Header(None)) -> dict:
    import user_items_store

    tenant_id, sub = _cards_caller(authorization)

    def remove() -> dict:
        if not (_QUERY_ID.fullmatch(query_id) and _delete_item_and_share(tenant_id, sub, "query", query_id)):
            raise HTTPException(status_code=404, detail="Query not found")
        return {"deleted": query_id}

    return await _blocking(remove)


MAX_SHARE_RECIPIENTS = 200
_NOT_SHARED = {"tenant": False, "count": 0}


def _share_summaries(tenant_id: str, sub: str, item_kind: str) -> dict[str, dict]:
    import user_items_store

    return {s["item_id"]: {"tenant": bool(s.get("tenant")), "count": len(s.get("recipients", []))}
            for s in user_items_store.list_shares(tenant_id, sub) if s["item_kind"] == item_kind}


def _delete_item_and_share(tenant_id: str, sub: str, item_kind: str, item_id: str) -> bool:
    """The share goes first, so a failure part-way leaves an unshared item rather than a share on a deleted one."""
    import user_items_store

    user_items_store.delete_share(tenant_id, sub, item_kind, item_id)
    return user_items_store.delete(tenant_id, sub, item_kind, item_id)


def _require_shareable(tenant_id: str, sub: str, item_kind: str, item_id: str) -> None:
    import user_items_store

    if item_kind == "card" and item_id in {c["id"] for c in _default_cards()}:
        raise _bad_request("Built-in cards can't be shared")
    pattern = _CARD_ID if item_kind == "card" else _QUERY_ID
    if not (pattern.fullmatch(item_id) and user_items_store.get(tenant_id, sub, item_kind, item_id)):
        raise HTTPException(status_code=404, detail=f"{item_kind.capitalize()} not found")


def _share_view(share: Optional[dict], names: dict[str, str]) -> dict:
    if share is None:
        return {"share_id": None, "recipients": [], "tenant": False}
    return {"share_id": share["share_id"], "tenant": bool(share.get("tenant")),
            "recipients": [{"sub": r, "name": names.get(r, r)} for r in share.get("recipients", [])]}


def _share_recipients(payload, tenant_id: str, sub: str, names: dict[str, str]) -> tuple[list[str], bool]:
    if not isinstance(payload, dict):
        raise _bad_request("Body must be an object with recipients and tenant")
    recipients, tenant = payload.get("recipients"), payload.get("tenant")
    if not isinstance(recipients, list):
        raise _bad_request("recipients must be a list")
    if not isinstance(tenant, bool):
        raise _bad_request("tenant must be true or false")
    if len(recipients) > MAX_SHARE_RECIPIENTS:
        raise _bad_request(f"You can share with at most {MAX_SHARE_RECIPIENTS} people")
    if len(set(map(str, recipients))) != len(recipients):
        raise _bad_request("A recipient is listed twice")
    if sub in recipients:
        raise _bad_request("You can't share with yourself")
    if not all(isinstance(r, str) and r in names for r in recipients):
        raise _bad_request("Every recipient must be in your tenant directory")
    return recipients, tenant


def _directory_names(tenant_id: str) -> dict[str, str]:
    import user_items_store

    return {e["sub"]: e["name"] for e in user_items_store.list_directory(tenant_id)}


def _set_share(tenant_id: str, sub: str, item_kind: str, item_id: str, payload) -> dict:
    """Create, update or (empty recipients and tenant false) remove the item's share. Re-sharing keeps the share id."""
    import user_items_store

    _require_shareable(tenant_id, sub, item_kind, item_id)
    names = _directory_names(tenant_id)
    recipients, tenant = _share_recipients(payload, tenant_id, sub, names)
    if not recipients and not tenant:
        user_items_store.delete_share(tenant_id, sub, item_kind, item_id)
        return _share_view(None, names)
    for attempt in range(3):
        current = user_items_store.get_share(tenant_id, sub, item_kind, item_id)
        try:
            if current is None:
                saved = user_items_store.put_share(tenant_id, sub, item_kind, item_id,
                                                   {"recipients": recipients, "tenant": tenant})
            else:
                saved = user_items_store.update_share(tenant_id, sub, item_kind, item_id,
                                                      {"recipients": recipients, "tenant": tenant}, current["version"])
            return _share_view(saved, names)
        except user_items_store.Conflict:
            if attempt == 2:
                raise HTTPException(status_code=409, detail="This share changed elsewhere; reload and try again")


def _share_routes(plural: str, item_kind: str) -> None:
    import user_items_store

    path = f"/api/v1/me/{plural}/{{item_id}}/share"

    @app.put(path, name=f"put_{item_kind}_share")
    async def put_share(item_id: str, payload: dict, authorization: Optional[str] = Header(None)) -> dict:
        """Share the caller's item with people in their tenant, or with everyone in it; empty unshares."""
        tenant_id, sub = _cards_caller(authorization)
        return await _blocking(_set_share, tenant_id, sub, item_kind, item_id, payload)

    @app.get(path, name=f"get_{item_kind}_share")
    async def get_share(item_id: str, authorization: Optional[str] = Header(None)) -> dict:
        tenant_id, sub = _cards_caller(authorization)

        def load() -> dict:
            _require_shareable(tenant_id, sub, item_kind, item_id)
            return _share_view(user_items_store.get_share(tenant_id, sub, item_kind, item_id), _directory_names(tenant_id))

        return await _blocking(load)

    @app.delete(path, name=f"delete_{item_kind}_share")
    async def delete_share(item_id: str, authorization: Optional[str] = Header(None)) -> dict:
        tenant_id, sub = _cards_caller(authorization)

        def remove() -> dict:
            _require_shareable(tenant_id, sub, item_kind, item_id)
            user_items_store.delete_share(tenant_id, sub, item_kind, item_id)
            return _share_view(None, {})

        return await _blocking(remove)


_share_routes("queries", "query")
_share_routes("cards", "card")


_SHARED_NOT_FOUND = "Shared item not found"
_SQL_WITHHELD = "The SQL text is hidden because it could not be checked for personal data."


def _not_shared() -> HTTPException:
    return HTTPException(status_code=404, detail=_SHARED_NOT_FOUND)


def _shared_with(share: dict, sub: str) -> bool:
    recipients = share.get("recipients")
    return share.get("owner_sub") != sub and (
        share.get("tenant") is True or (isinstance(recipients, list) and sub in recipients))


def _owner_display(entry: Optional[dict]) -> str:
    """The directory name, else the email's local part, else a neutral label."""
    for value in ((entry or {}).get("name"), ((entry or {}).get("email") or "").split("@")[0]):
        if isinstance(value, str) and value.strip():
            return value.strip()[:200]
    return "A colleague"


def _live_item(tenant_id: str, share: dict) -> Optional[dict]:
    import user_items_store

    try:
        return user_items_store.get(tenant_id, share["owner_sub"], share["item_kind"], share["item_id"])
    except (KeyError, ValueError):
        return None


def _shared_item(tenant_id: str, sub: str, share_id: str) -> tuple[dict, dict, str]:
    """The share, its live item and the owner's display name; one 404 for every reason the caller can't see it."""
    import user_items_store

    pointer = user_items_store.get_share_pointer(tenant_id, share_id)
    try:
        share = pointer and user_items_store.get_share(
            tenant_id, pointer["owner_sub"], pointer["item_kind"], pointer["item_id"])
    except (KeyError, ValueError):
        share = None
    if not share or share.get("share_id") != share_id or not _shared_with(share, sub):
        raise _not_shared()
    item = _live_item(tenant_id, share)
    if item is None:
        raise _not_shared()
    owner = _owner_display(user_items_store.get(tenant_id, share["owner_sub"], "directory"))
    return share, item, owner


def _shared_summary(share: dict, item: dict, owner: str) -> dict:
    out = {"share_id": share["share_id"], "item_kind": share["item_kind"], "owner": {"name": owner},
           "owners_note": item.get("description") if isinstance(item.get("description"), str) else "",
           "shared_at": share.get("shared_at")}
    if share["item_kind"] == "query":
        stored = _stored_query(item)
        out.update(name=item.get("name"), query_kind=stored["query_kind"], governance_hint=_governance_hint(stored))
    else:
        out.update(label=item.get("label"), format=item.get("format"))
    return out


def _shared_detail(share: dict, item: dict, owner: str, allow_identity: bool) -> dict:
    """The live item for viewing. Without identity rights literals and filter values are masked and prompts omitted."""
    from semantic_layer.redact import redact_contract, redact_sql

    out = {**_shared_summary(share, item, owner), "redacted": not allow_identity}
    contract = (lambda c: c) if allow_identity else redact_contract
    if allow_identity and isinstance(item.get("prompt"), str):
        out["prompt"] = item["prompt"]
    if share["item_kind"] == "card":
        out.update(longDescription=item.get("longDescription"), reportLink=item.get("reportLink"),
                   contract=contract(item.get("contract")))
        return out
    stored = _stored_query(item)
    if stored["query_kind"] != "sql":
        out["contract"] = contract(stored.get("contract"))
        return out
    sql = stored.get("sql") if isinstance(stored.get("sql"), str) else ""
    parts = stored.get("governed_parts") if isinstance(stored.get("governed_parts"), dict) else {}
    out["sql"] = sql if allow_identity else redact_sql(sql)
    out["governed_parts"] = {name: contract(part) for name, part in parts.items()}
    if out["sql"] is None:
        out["note"] = _SQL_WITHHELD
    return out


_COPY_HIDDEN = "This item contains values hidden for your role, so it can't be copied."


def _hides_values(item_kind: str, item: dict) -> bool:
    """True when the redacted view differs from the stored item, so a copy would hand the caller what the view hid."""
    from semantic_layer.redact import redact_contract, redact_sql

    prompt = item.get("prompt")
    if prompt and (not isinstance(prompt, str) or prompt.strip()):
        return True
    stored = item if item_kind == "card" else _stored_query(item)
    if item_kind == "card" or stored["query_kind"] != "sql":
        contract = stored.get("contract")
        return contract is not None and redact_contract(contract) != contract
    sql, parts = stored.get("sql"), stored.get("governed_parts")
    if redact_sql(sql) != sql:
        return True
    return parts not in (None, {}) and (
        not isinstance(parts, dict) or any(redact_contract(p) != p for p in parts.values()))


@app.get("/api/v1/shared")
async def list_shared_with_me(authorization: Optional[str] = Header(None)) -> dict:
    """Live items others in the caller's tenant shared with them or with everyone, newest share first."""
    import user_items_store

    tenant_id, sub = _cards_caller(authorization)

    def load() -> dict:
        directory = {e["sub"]: e for e in user_items_store.list_directory(tenant_id)}
        items = []
        for share in user_items_store.list_tenant_shares(tenant_id):
            if not _shared_with(share, sub):
                continue
            item = _live_item(tenant_id, share)
            if item is not None:
                items.append(_shared_summary(share, item, _owner_display(directory.get(share["owner_sub"]))))
        items.sort(key=lambda i: i["shared_at"] or "", reverse=True)
        return {"items": items}

    return await _blocking(load)


@app.get("/api/v1/shared/{share_id}")
async def get_shared_with_me(share_id: str, authorization: Optional[str] = Header(None)) -> dict:
    from roles import role_allows_identity

    user, tenant = _caller(authorization)
    share, item, owner = await _blocking(_shared_item, tenant.tenant_id, user["sub"], share_id)
    return _shared_detail(share, item, owner, role_allows_identity(user))


@app.post("/api/v1/shared/{share_id}/run")
async def run_shared_with_me(share_id: str, authorization: Optional[str] = Header(None)):
    """Run the live item as the caller, under the caller's role and licence; never writes the owner's row."""
    from roles import role_allows_identity

    user, tenant = _caller(authorization)
    share, item, owner = await _blocking(_shared_item, tenant.tenant_id, user["sub"], share_id)
    identity = role_allows_identity(user)
    try:
        if share["item_kind"] == "card":
            response = await _run_contract(item.get("contract"), user, tenant)
        else:
            note = _text(_stored_query(item), "description")
            reason = f"Owner's note: {note}" if note else f"Shared by {owner}"
            response = await _run_stored_query(item, user, tenant, reason, owner_writes=False)
    except HTTPException as e:
        if identity:
            raise
        hidden = _stored_values(share["item_kind"], item)
        raise HTTPException(status_code=e.status_code, detail=_scrub(_masked_run(e.detail), hidden))
    if identity:
        return response
    masked = _masked_run(response)
    if isinstance(masked, dict) and "error" in masked:
        masked["error"] = _scrub(masked["error"], _stored_values(share["item_kind"], item))
    return masked


def _stored_values(item_kind: str, item: dict) -> list[str]:
    """The item's filter values and SQL string literals, longest first: what a Viewer's view of it masks."""
    import sqlglot
    from semantic_layer.redact import _STRING_TOKENS

    stored = item if item_kind == "card" else _stored_query(item)
    contracts = [stored.get("contract")]
    if isinstance(stored.get("governed_parts"), dict):
        contracts += list(stored["governed_parts"].values())
    values = {str(v) for c in contracts if isinstance(c, dict) and isinstance(c.get("filters"), list)
              for f in c["filters"] if isinstance(f, dict) and isinstance(f.get("values"), list) for v in f["values"]}
    if isinstance(stored.get("sql"), str):
        try:
            tokens = sqlglot.Dialect.get_or_raise("snowflake").tokenize(stored["sql"])
            values |= {t.text for t in tokens if t.token_type in _STRING_TOKENS}
        except Exception:
            pass
    return sorted((v for v in values if v), key=len, reverse=True)


def _scrub(value, hidden: list[str]):
    """value with every occurrence of a hidden string replaced by '…', recursing through dicts and lists."""
    if isinstance(value, str):
        for h in hidden:
            value = value.replace(h, "…")
        return value
    if isinstance(value, dict):
        return {k: _scrub(v, hidden) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(v, hidden) for v in value]
    return value


def _masked_run(response):
    """The run response with the SQL and governed parts redacted as a shared view is; rows are already guarded."""
    from semantic_layer.redact import redact_contract, redact_sql

    is_json = isinstance(response, JSONResponse)
    body = json.loads(response.body) if is_json else response
    if not isinstance(body, dict):
        return response
    body = dict(body)
    for key in ("sql", "executed_sql"):
        if key in body:
            masked = redact_sql(body[key])
            body[key] = masked if masked is not None else ""
    if isinstance(body.get("governed_parts"), dict):
        body["governed_parts"] = {name: redact_contract(c) for name, c in body["governed_parts"].items()}
    return JSONResponse(body, status_code=response.status_code) if is_json else body


@app.post("/api/v1/shared/{share_id}/copy")
async def copy_shared_with_me(share_id: str, authorization: Optional[str] = Header(None)) -> dict:
    """Save a copy of the live item to the caller's profile through their own create path; 400 with the reason."""
    from roles import role_allows_identity

    user, tenant = _caller(authorization)

    def save() -> dict:
        share, item, _ = _shared_item(tenant.tenant_id, user["sub"], share_id)
        if not role_allows_identity(user) and _hides_values(share["item_kind"], item):
            raise _bad_request(_COPY_HIDDEN)
        if share["item_kind"] == "card":
            return _create_card(tenant.tenant_id, user["sub"], _card_body(item))
        stored = _stored_query(item)
        fields = ("name", "description", "query_kind", "sql", "governed_parts") if stored["query_kind"] == "sql" \
            else ("name", "prompt", "description", "contract")
        return _create_query({k: stored.get(k) for k in fields}, user, tenant)

    return await _blocking(save)


_PREF_LISTS = ("favorites", "recentReports", "readNotifications")
_PREF_LIMITS = {"favorites": MAX_PREF_IDS, "recentReports": MAX_RECENT_REPORTS, "readNotifications": MAX_PREF_IDS}
_PREFS_CONFLICT = "Your preferences changed elsewhere; reload and try again"


def _default_prefs() -> dict:
    return {"favorites": [], "recentReports": [], "readNotifications": [], "visibleWidgets": list(WIDGETS)}


def _prefs_view(tenant_id: str, sub: str) -> dict:
    import user_items_store

    item = user_items_store.get(tenant_id, sub, "prefs")
    if item is None:
        return {**_default_prefs(), "version": 0}
    return {**{k: item.get(k, v) for k, v in _default_prefs().items()}, "version": item["version"]}


def _prefs_body(payload) -> dict:
    errors = []
    body = {}
    for field in (*_PREF_LISTS, "visibleWidgets"):
        value = payload.get(field)
        if not isinstance(value, list) or any(not isinstance(i, str) or len(i) > 200 for i in value):
            errors.append(f"{field} must be a list of strings (at most 200 characters each)")
            continue
        value = list(dict.fromkeys(value))
        if field == "visibleWidgets":
            unknown = [i for i in value if i not in WIDGETS]
            if unknown:
                errors.append(f"unknown widgets: {', '.join(unknown)}")
        elif len(value) > _PREF_LIMITS[field]:
            errors.append(f"{field} can hold at most {_PREF_LIMITS[field]} entries")
        body[field] = value
    if not errors:
        errors = _size_errors(body)
    if errors:
        raise _bad_request(*errors)
    return body


@app.get("/api/v1/me/prefs")
async def get_my_prefs(authorization: Optional[str] = Header(None)) -> dict:
    tenant_id, sub = _cards_caller(authorization)
    return await _blocking(_prefs_view, tenant_id, sub)


@app.put("/api/v1/me/prefs")
async def put_my_prefs(payload: dict, authorization: Optional[str] = Header(None)) -> dict:
    """Replace the four preference lists; 409 when expected_version is stale."""
    import user_items_store

    tenant_id, sub = _cards_caller(authorization)
    version = payload.get("expected_version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 0:
        raise _bad_request("expected_version must be a non-negative integer")
    body = _prefs_body(payload)

    def save() -> dict:
        try:
            user_items_store.put(tenant_id, sub, "prefs", None, body, expected_version=version or None)
        except user_items_store.Conflict:
            raise HTTPException(status_code=409, detail=_PREFS_CONFLICT)
        return _prefs_view(tenant_id, sub)

    return await _blocking(save)


MAX_IMPORT_CARDS = 1000
MAX_IMPORT_QUERIES = 2000
MAX_IMPORT_SQL_CHECKS = 100  # each SQL item is validated (parsed and spliced) as the importing user


def _import_id(prefix: str, sub: str, kind: str, source_id) -> str:
    import hashlib

    return prefix + hashlib.sha256(f"{sub}:{kind}:{source_id}".encode()).hexdigest()[:12]


def _skip_reason(exc: HTTPException) -> str:
    detail = exc.detail
    return "; ".join(detail["errors"]) if isinstance(detail, dict) else str(detail)


def _write_failures() -> tuple:
    """Store errors one import write may hit; the item is skipped and the import goes on."""
    from botocore.exceptions import ClientError

    return ClientError, ValueError


def _write_failed(skipped: list, kind: str, name: str, exc: Exception) -> None:
    """Skip an item whose write failed, logging the error but never the item, which is user content.
    A store error is marked for retry, so the import ends `partial` without its marker; a ValueError would recur."""
    code = getattr(exc, "response", {}).get("Error", {}).get("Code") or type(exc).__name__
    logger.warning("profile import: a %s write failed (%s)", kind, code)
    skipped.append({"kind": kind, "name": name, "reason": "could not be saved", "_retry": not isinstance(exc, ValueError)})


def _name_of(item, field: str) -> str:
    value = item.get(field) if isinstance(item, dict) else None
    return value[:200] if isinstance(value, str) else ""


def _import_cards(tenant_id: str, sub: str, cards: list, skipped: list) -> int:
    """Create the custom cards, then apply order and enabled state for all cards in one layout write."""
    import user_items_store

    builtin = {c["id"] for c in _default_cards()}
    room = MAX_CUSTOM_CARDS - user_items_store.count(tenant_id, sub, "card")
    order, disabled, created = [], [], 0
    for card in cards:
        if not isinstance(card, dict) or not isinstance(card.get("id"), str) or len(card["id"]) > 200:
            skipped.append({"kind": "card", "name": _name_of(card, "label"), "reason": "card needs a string id"})
            continue
        if card["id"] in builtin:
            target = card["id"]
        elif card.get("isBuiltIn") is True:
            continue
        else:
            try:
                body = _card_body(card)
            except HTTPException as e:
                skipped.append({"kind": "card", "name": _name_of(card, "label"), "reason": _skip_reason(e)})
                continue
            target = _import_id("c_", sub, "card", card["id"])
            try:
                if room <= 0:
                    raise _bad_request(f"You can have at most {MAX_CUSTOM_CARDS} custom cards")
                user_items_store.put(tenant_id, sub, "card", target, body)
                room -= 1
                created += 1
            except HTTPException as e:
                skipped.append({"kind": "card", "name": _name_of(card, "label"), "reason": _skip_reason(e)})
                continue
            except user_items_store.Conflict:
                pass
            except _write_failures() as e:
                _write_failed(skipped, "card", _name_of(card, "label"), e)
                continue
        order.append(target)
        if card.get("enabled") is False:
            disabled.append(target)

    if order:
        def apply(layout: dict, version: int) -> None:
            existing = layout["order"] if version else [c["id"] for c in _default_cards()]
            layout["order"] = list(dict.fromkeys(order + existing))
            layout["disabled"] = list(dict.fromkeys(layout["disabled"] + disabled))

        try:
            _mutate_layout(tenant_id, sub, apply)
        except HTTPException as e:
            skipped.append({"kind": "layout", "name": "", "reason": _skip_reason(e)})
        except _write_failures() as e:
            _write_failed(skipped, "layout", "", e)
    return created


def _import_queries(user: dict, tenant: Tenant, queries: list, skipped: list) -> int:
    """SQL queries (query_kind "sql") are checked against the guard as the importing user."""
    tenant_id, sub = tenant.tenant_id, user["sub"]
    import user_items_store

    room = MAX_QUERIES - user_items_store.count(tenant_id, sub, "query")
    created = sql_checked = 0
    for i, query in enumerate(queries):
        name = _name_of(query, "name")
        try:
            if not isinstance(query, dict):
                raise _bad_request("query must be an object")
            if room <= 0:
                raise _bad_request(f"You can save at most {MAX_QUERIES} queries")
            is_sql = "sql" in (query.get("query_kind"), query.get("kind"))
            if is_sql:
                if sql_checked >= MAX_IMPORT_SQL_CHECKS:
                    raise _bad_request("Too many SQL queries in one import")
                sql_checked += 1
            body = _saved_query_body({**query, "query_kind": "sql" if is_sql else "governed"}, user, tenant)
        except HTTPException as e:
            skipped.append({"kind": "query", "name": name, "reason": _skip_reason(e)})
            continue
        source_id = query.get("id") if isinstance(query.get("id"), str) else f"index-{i}"
        body.update(createdAt=_iso_or(query.get("createdAt"), _now_iso()),
                    lastUsedAt=_iso_or(query.get("lastUsedAt"), None))
        try:
            user_items_store.put(tenant_id, sub, "query", _import_id("q_", sub, "query", source_id), body)
            room -= 1
            created += 1
        except user_items_store.Conflict:
            pass
        except _write_failures() as e:
            _write_failed(skipped, "query", name, e)
    return created


def _import_prefs(tenant_id: str, sub: str, prefs, skipped: list) -> None:
    """Only when the user has no stored prefs; over-long lists are truncated and unknown widgets dropped."""
    import user_items_store

    if prefs is None or user_items_store.get(tenant_id, sub, "prefs") is not None:
        return
    if not isinstance(prefs, dict):
        skipped.append({"kind": "prefs", "name": "", "reason": "prefs must be an object"})
        return
    cleaned = {}
    for field in _PREF_LISTS:
        value = prefs.get(field, [])
        cleaned[field] = [i for i in value if isinstance(i, str) and len(i) <= 200][:_PREF_LIMITS[field]] \
            if isinstance(value, list) else []
    widgets = prefs.get("visibleWidgets")
    cleaned["visibleWidgets"] = [w for w in dict.fromkeys(widgets) if w in WIDGETS] \
        if isinstance(widgets, list) and all(isinstance(w, str) for w in widgets) else list(WIDGETS)
    too_large = _size_errors(cleaned)
    if too_large:
        skipped.append({"kind": "prefs", "name": "", "reason": too_large[0]})
        return
    try:
        user_items_store.put(tenant_id, sub, "prefs", None, cleaned)
    except user_items_store.Conflict:
        pass
    except _write_failures() as e:
        _write_failed(skipped, "prefs", "", e)


@app.post("/api/v1/me/import")
async def import_my_items(payload: dict, authorization: Optional[str] = Header(None)) -> dict:
    """One-time import of a browser's saved cards, queries and preferences; later calls are no-ops.
    `partial` (no marker written) when a store error skipped an item, so the browser keeps its copy and retries."""
    import user_items_store

    user, tenant = _caller(authorization)
    tenant_id, sub = tenant.tenant_id, user["sub"]
    cards, queries, prefs = payload.get("cards", []), payload.get("queries", []), payload.get("prefs")
    if not isinstance(cards, list) or len(cards) > MAX_IMPORT_CARDS:
        raise _bad_request(f"cards must be a list of at most {MAX_IMPORT_CARDS}")
    if not isinstance(queries, list) or len(queries) > MAX_IMPORT_QUERIES:
        raise _bad_request(f"queries must be a list of at most {MAX_IMPORT_QUERIES}")

    def run() -> dict:
        if user_items_store.get(tenant_id, sub, "imported") is not None:
            return {"status": "already_imported"}
        skipped: list = []
        n_cards = _import_cards(tenant_id, sub, cards, skipped)
        n_queries = _import_queries(user, tenant, queries, skipped)
        _import_prefs(tenant_id, sub, prefs, skipped)
        retry = [s.pop("_retry", False) for s in skipped]
        if any(retry):
            return {"status": "partial", "cards": n_cards, "queries": n_queries, "skipped": skipped}
        try:
            user_items_store.put(tenant_id, sub, "imported", None, {"at": _now_iso()})
        except user_items_store.Conflict:
            return {"status": "already_imported"}
        return {"status": "imported", "cards": n_cards, "queries": n_queries, "skipped": skipped}

    return await _blocking(run)


@app.post("/api/chat", response_model=ChatResponse)
async def chat(
    request: ChatRequest,
    authorization: Optional[str] = Header(None)
):
    """Send a message via chat_engine (non-streaming)."""
    user = _get_user_from_token(authorization)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    logger.info(f"Authenticated user: {user.get('email', user.get('sub', 'unknown'))}")

    message_text = request.get_message_text()
    context_id = request.get_context_id()

    if not message_text:
        raise HTTPException(status_code=400, detail="No message provided")

    logger.info(f"Chat request: message='{message_text[:100]}...', context_id={context_id}")

    tenant = tenant_for(user)
    engine_kwargs = await asyncio.get_running_loop().run_in_executor(None, _engine_kwargs, tenant, user)

    try:
        result = await send_message(
            message_text=message_text,
            owner=_owner(user, tenant),
            context_id=context_id,
            engine_kwargs=engine_kwargs,
        )

        return ChatResponse(
            text=result.get("text", ""),
            artifacts=_json_safe(result["artifacts"]),
            context_id=result.get("contextId", context_id),
        )

    except (HTTPException, TenantNotReady):
        raise
    except Exception as e:
        logger.error(f"Chat engine error: {e}")
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/api/chat/stream")
async def chat_stream(
    request: ChatRequest,
    authorization: Optional[str] = Header(None)
):
    """
    Send a message and receive the reply as Server-Sent Events: status events while the chat
    engine works, then one complete (or error) event.
    """
    user = _get_user_from_token(authorization)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    message_text = request.get_message_text()
    context_id = request.get_context_id()
    request_id = request.get_request_id()

    if not message_text:
        raise HTTPException(status_code=400, detail="No message provided")

    logger.info(
        f"Streaming chat request: message='{message_text[:100]}...', "
        f"context_id={context_id}, request_id={request_id}"
    )

    tenant = tenant_for(user)
    engine_kwargs = await asyncio.get_running_loop().run_in_executor(None, _engine_kwargs, tenant, user)

    if request_id:
        _request_owners[request_id] = _owner(user, tenant)

    async def event_generator():
        """Relay SSE events from chat_engine to the frontend."""
        try:
            async for event in send_message_streaming(
                message_text=message_text,
                owner=_owner(user, tenant),
                context_id=context_id,
                engine_kwargs=engine_kwargs,
            ):
                # Check if request was cancelled
                if request_id and request_id in _cancelled_requests:
                    logger.info(f"Request {request_id} was cancelled")
                    cancelled_event = json.dumps({
                        "type": "cancelled",
                        "message": "Request cancelled by user"
                    })
                    yield f"data: {cancelled_event}\n\n"
                    _cancelled_requests.discard(request_id)
                    break

                # Tool artifacts carry Snowflake Decimal/date/binary values that plain json.dumps rejects.
                event_data = json.dumps(_json_safe(event))
                yield f"data: {event_data}\n\n"

        except TenantNotReady:
            yield f"data: {json.dumps({'type': 'error', 'message': NOT_READY})}\n\n"
        except Exception as e:
            logger.error(f"Error in streaming relay: {e}")
            error_event = json.dumps({
                "type": "error",
                "message": str(e)
            })
            yield f"data: {error_event}\n\n"
        finally:
            if request_id:
                _cancelled_requests.discard(request_id)
                _request_owners.pop(request_id, None)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )


@app.post("/api/chat/cancel/{request_id}")
async def cancel_chat(
    request_id: str,
    authorization: Optional[str] = Header(None)
):
    """Cancel an in-progress chat request owned by the caller."""
    user = _get_user_from_token(authorization)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    if _request_owners.get(request_id) != _owner(user, tenant_for(user)):
        raise HTTPException(status_code=404, detail="No such request in progress")

    logger.info(f"Cancelling request: {request_id}")
    _cancelled_requests.add(request_id)
    return {"success": True, "request_id": request_id}


@app.get("/api/conversations")
async def list_conversations(authorization: Optional[str] = Header(None)):
    """The caller's recent conversations, newest first."""
    user = _get_user_from_token(authorization)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    import asyncio
    from conversation_store import list_conversations as list_for
    owner = _owner(user, tenant_for(user))
    loop = asyncio.get_running_loop()
    return {"conversations": await loop.run_in_executor(None, list_for, owner)}


@app.get("/api/conversations/{context_id}")
async def get_conversation(
    context_id: str,
    authorization: Optional[str] = Header(None)
):
    """Get conversation history by context ID."""
    user = _get_user_from_token(authorization)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    logger.info(f"Conversation history requested for context: {context_id}")
    from conversation_store import load_history, owns
    owner = _owner(user, tenant_for(user))
    if not owns(context_id, owner):
        raise HTTPException(status_code=404, detail="Conversation not found")
    return {"messages": load_history(context_id, owner)}


@app.delete("/api/conversations/{context_id}")
async def clear_conversation(
    context_id: str,
    authorization: Optional[str] = Header(None)
):
    """Clear a conversation context."""
    user = _get_user_from_token(authorization)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    logger.info(f"Clear conversation requested for context: {context_id}")
    from conversation_store import clear_history
    if not clear_history(context_id, _owner(user, tenant_for(user))):
        raise HTTPException(status_code=404, detail="Conversation not found")
    return {"success": True}


# =============================================================================
# Data Dictionary Endpoints
# =============================================================================

async def _proxy_dictionary_request(path: str) -> object:
    """Fetch from the Blackboard data dictionary API with in-memory TTL cache."""
    import time
    import asyncio

    cache_key = path
    if cache_key in _dictionary_cache:
        cached_time, cached_data = _dictionary_cache[cache_key]
        if (time.time() - cached_time) < DICTIONARY_CACHE_TTL:
            return cached_data

    loop = asyncio.get_event_loop()
    url = f"{DATA_DICTIONARY_BASE_URL}/{path}"

    def _fetch():
        resp = http_requests.get(url, timeout=15)
        resp.raise_for_status()
        return resp.json()

    try:
        data = await loop.run_in_executor(None, _fetch)
        _dictionary_cache[cache_key] = (time.time(), data)
        logger.info(f"Cached dictionary data for '{path}'")
        return data
    except Exception as e:
        logger.error(f"Dictionary proxy failed for {path}: {e}")
        raise HTTPException(status_code=502, detail="Data dictionary service unavailable")


@app.get("/api/v1/dictionary/submodels")
async def dictionary_submodels(authorization: Optional[str] = Header(None)):
    """Returns all CDM domains with display names."""
    user = _get_user_from_token(authorization)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    return await _proxy_dictionary_request("submodels")


@app.get("/api/v1/dictionary/definitions")
async def dictionary_definitions(authorization: Optional[str] = Header(None)):
    """Returns all column definitions."""
    user = _get_user_from_token(authorization)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    return await _proxy_dictionary_request("definitions")


@app.get("/api/v1/dictionary/erd")
async def dictionary_erd(authorization: Optional[str] = Header(None)):
    """Returns entity relationships (foreign keys)."""
    user = _get_user_from_token(authorization)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    return await _proxy_dictionary_request("erd")


@app.get("/api/v1/dictionary/preview")
async def dictionary_preview(
    schema: str,
    table: str,
    limit: int = 20,
    authorization: Optional[str] = Header(None),
):
    """Preview sample rows from a CDM table (administrators only).

    Returns { columns: string[], rows: Record<string, unknown>[] }.
    Schema must start with CDM_ or be LEARN (Premium), limit capped at 100.
    """
    user = _get_user_from_token(authorization)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    # Raw rows include person tables, so previews are for administrators only.
    _require_admin(user)
    tenant = tenant_for(user)

    # Validate identifiers
    if not _SAFE_IDENTIFIER.match(schema) or not _SAFE_IDENTIFIER.match(table):
        raise HTTPException(status_code=400, detail="Invalid identifier: only alphanumeric and underscore allowed")

    if schema.upper() == "LEARN":
        if not allows_learn(tenant):
            raise HTTPException(status_code=403, detail=PREVIEW_REFUSAL)
    elif not schema.upper().startswith("CDM_"):
        raise HTTPException(status_code=400, detail="Schema must start with CDM_ or be LEARN")

    # Cap limit
    limit = min(max(1, limit), 100)

    try:
        import asyncio
        from snowflake_client import query_preview

        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None, lambda: query_preview(schema.upper(), table.upper(), limit, tenant=tenant)
        )
        logger.info(f"Preview: {schema}.{table} returned {len(result['rows'])} rows")
        return result
    except (HTTPException, TenantNotReady):
        raise
    except Exception as e:
        logger.error(f"Snowflake preview query failed: {e}")
        raise HTTPException(status_code=502, detail="Failed to query sample data")


# =============================================================================
# Semantic layer
# =============================================================================


def _database_for(tenant: Tenant) -> str:
    """Snowflake database the semantic layer's {{ database }} placeholder resolves to for the tenant."""
    import snowflake_client
    return snowflake_client.tenant_database(tenant)


_OVERLAY_TTL_SECONDS = 60
_overlay_cache: dict[str, tuple[float, tuple]] = {}


def _apply_each(catalog, overlays: list) -> tuple:
    """Apply overlays one at a time, skipping any that no longer validate against the definitions.

    Returns (catalog, applied overlays, {target: problems} for those skipped).
    """
    from pydantic import ValidationError
    from semantic_layer.overlays import OverlayError, apply_overlays, validate_overlay

    applied, skipped = [], {}
    for ov in overlays:
        try:
            problems = validate_overlay(ov, catalog)
            if not problems:
                catalog = apply_overlays(catalog, [ov])
                applied.append(ov)
        except (OverlayError, ValidationError) as e:
            problems = [str(e)]
        if problems:
            logger.warning("Skipping overlay %s: %s", ov.target, problems)
            skipped[ov.target] = problems
    return catalog, applied, skipped


def _tenant_state(tenant_id: str) -> tuple:
    """(catalog, applied, skipped) for the tenant, cached briefly per instance; admin writes here invalidate it."""
    import time
    import overlay_store
    from semantic_layer.catalog import default_catalog

    hit = _overlay_cache.get(tenant_id)
    if hit and time.monotonic() - hit[0] < _OVERLAY_TTL_SECONDS:
        return hit[1]
    try:
        stored = overlay_store.list_overlays(tenant_id)
    except Exception as e:
        # Canonical definitions keep the tenant's queries working; not cached, so recovery is immediate.
        logger.error("Overlay store unavailable for tenant %s: %s", tenant_id, e)
        return default_catalog(), [], {}
    state = _apply_each(default_catalog(), stored)
    _overlay_cache[tenant_id] = (time.monotonic(), state)
    return state


def _catalog_for(tenant: Tenant) -> tuple:
    """The canonical catalog with the tenant's overlays applied, and the overlays used."""
    catalog, applied, _ = _tenant_state(tenant.tenant_id)
    return catalog, applied


def _compile_contract(contract: QueryContract, authorization: str):
    """Authenticate, then compile; raises the HTTP error the caller should return."""
    user = _get_user_from_token(authorization)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    return _compile_for(contract, user, tenant_for(user))


def _compile_for(contract: QueryContract, user: dict, tenant: Tenant):
    """Compile against the tenant's catalog and database with the caller's identity rights; 400 on a compile error."""
    from semantic_layer.compiler import CompileError, compile_query
    from semantic_layer.overlays import overlays_used

    from roles import role_allows_identity

    catalog, overlays = _catalog_for(tenant)
    allow_identity = role_allows_identity(user)
    database = _database_for(tenant)
    try:
        compiled = compile_query(contract, catalog, database, allow_identity=allow_identity)
    except CompileError as e:
        raise HTTPException(status_code=400, detail=str(e))
    compiled.provenance.overlays = overlays_used(compiled.provenance, catalog, overlays)
    if allow_identity and _selects_identity(contract, catalog):
        _log_identity_query(user, contract)
    return compiled


def _log_identity_query(user: dict, contract: QueryContract) -> None:
    """Audit line naming what was selected; filter values can be PII, so only filter dimensions and ops are logged."""
    logger.info(
        "identity_query sub=%s metrics=%s measures=%s dimensions=%s filters=%s", user.get("sub"),
        contract.metrics, contract.measures, contract.dimensions, [(f.dimension, f.op) for f in contract.filters])


def _selects_identity(contract: QueryContract, catalog) -> bool:
    return any(_is_identity(d, catalog) for d in contract.dimensions)


def _is_identity(ref: str, catalog) -> bool:
    """True when a dimension of this name is personally identifiable on any dataset (may over-match)."""
    name = ref.rpartition(":")[2].partition("__")[0]
    return any(ds.is_pii(dim.column) for ds in catalog.datasets.values() for dim in ds.dimensions if dim.name == name)


@app.post("/api/v1/semantic/compile")
async def semantic_compile(contract: QueryContract, authorization: Optional[str] = Header(None)) -> dict:
    """Compile a semantic query contract to SQL without executing it."""
    return _compile_contract(contract, authorization).model_dump()


def _etag_matches(if_none_match: str, etag: str) -> bool:
    """RFC 9110 weak comparison: '*', or any listed tag equal to etag once a W/ prefix is ignored."""
    tags = [t.strip() for t in if_none_match.split(",")]
    return "*" in tags or any(t.removeprefix("W/") == etag for t in tags)


@app.get("/api/v1/semantic/catalog")
async def semantic_catalog(
    authorization: Optional[str] = Header(None),
    if_none_match: Optional[str] = Header(default=None),
):
    """Public datasets, dimensions, measures and metrics; supports If-None-Match."""
    user = _get_user_from_token(authorization)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    tenant = tenant_for(user)
    import hashlib
    from fastapi import Response
    from semantic_layer.catalog_view import public_catalog

    from roles import role_allows_identity
    body = public_catalog(_catalog_for(tenant)[0], allow_identity=role_allows_identity(user))
    etag = '"' + hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:32] + '"'
    headers = {"ETag": etag, "Cache-Control": "private, no-cache"}
    if if_none_match and _etag_matches(if_none_match, etag):
        return Response(status_code=304, headers=headers)
    return JSONResponse(body, headers=headers)


@app.post("/api/v1/semantic/query")
async def semantic_query(contract: QueryContract, authorization: Optional[str] = Header(None)):
    """Compile a semantic query contract and run it through the execution guard."""
    user = _get_user_from_token(authorization)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    tenant = tenant_for(user)
    compiled = _compile_for(contract, user, tenant)
    result = await _execute(compiled, user, tenant)

    return _semantic_response(compiled, result)


def _semantic_response(compiled, result: dict) -> JSONResponse:
    # jsonable_encoder turns Snowflake's Decimal into numbers; response-model serialisation makes them strings.
    return JSONResponse(_json_safe({
        "columns": result["columns"],
        "rows": result["rows"],
        "sql": compiled.sql,
        "provenance": compiled.provenance,
    }))


class SqlRequest(BaseModel):
    sql: str = Field(max_length=20_000)
    governed: Optional[dict[str, dict]] = None
    reason: str = Field(default=DEFAULT_REASON, max_length=500)


def _identity_audit(user: dict, catalog):
    def audit(kind: str, subject) -> None:
        if kind == "freehand":
            logger.info("identity_query sub=%s freehand=true", user.get("sub"))
        elif _selects_identity(subject, catalog):
            _log_identity_query(user, subject)

    return audit


async def _run_sql_for(user: dict, tenant: Tenant, sql: str, governed: Optional[dict], reason: str) -> JSONResponse:
    """Run SQL through the guard as `user`; a refusal is a 400 and a warehouse failure a 502."""
    from semantic_layer.sql_service import run_sql

    def work():
        catalog, overlays = _catalog_for(tenant)
        ctx = guard_context(tenant, user, catalog)
        return run_sql(ctx, sql=sql, governed=governed, reason=reason, overlays=overlays,
                       identity_audit=_identity_audit(user, catalog))

    result = await _blocking(work)
    if "error" in result:
        if result.get("warehouse"):
            logger.error("SQL run failed: %s", result["error"])
            raise HTTPException(status_code=502, detail="The warehouse could not run this query.")
        raise HTTPException(status_code=400, detail=result["error"])
    return JSONResponse(_json_safe(result))


@app.post("/api/v1/sql/run")
async def sql_run(body: SqlRequest, authorization: Optional[str] = Header(None)):
    """Run SQL through the execution guard as the caller; `governed` parts are spliced from query contracts."""
    user, tenant = _caller(authorization)
    return await _run_sql_for(user, tenant, body.sql, body.governed, body.reason.strip() or DEFAULT_REASON)


@app.post("/api/v1/sql/validate")
async def sql_validate(body: SqlRequest, authorization: Optional[str] = Header(None)):
    """Check SQL against the guard for the caller without running it."""
    from semantic_layer.sql_service import validate_sql

    user = _get_user_from_token(authorization)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    tenant = tenant_for(user)
    return await _blocking(lambda: validate_sql(guard_context(tenant, user), sql=body.sql, governed=body.governed))


@app.get("/api/v1/schema/describe")
async def schema_describe(schema: str, table: str, authorization: Optional[str] = Header(None)):
    """Columns of a CDM or LEARN table from the data dictionary; LEARN needs Premium."""
    from semantic_layer.sql_service import describe

    user = _get_user_from_token(authorization)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    tenant = tenant_for(user)
    if not _SAFE_IDENTIFIER.match(schema) or not _SAFE_IDENTIFIER.match(table):
        raise HTTPException(status_code=400, detail="Invalid identifier: only alphanumeric and underscore allowed")
    result = await _blocking(lambda: describe(guard_context(tenant, user), schema, table))
    if "error" in result:
        raise HTTPException(status_code=result["status"], detail=result["error"])
    return result


def guard_context(tenant: Tenant, user: dict, catalog=None):
    """The SQL guard's context for this request: the caller's identity rights over the tenant's overlaid catalog
    (pass `catalog` when already fetched) and database, with every dataset's declared PII columns."""
    import snowflake_client
    from roles import role_allows_identity

    return snowflake_client.GuardContext.of(tenant, _catalog_for(tenant)[0] if catalog is None else catalog,
                                            allow_identity=role_allows_identity(user))


def _guarded(compiled, ctx):
    """Validate compiled SQL in Compiled mode and run it: the guard's Refusal or the warehouse Result."""
    import snowflake_client

    checked = snowflake_client.validate(compiled.sql, ctx, snowflake_client.Compiled(compiled))
    return checked if isinstance(checked, snowflake_client.Refusal) else snowflake_client.execute(checked, ctx)


async def _execute(compiled, user: dict, tenant: Tenant) -> dict:
    """Run compiled SQL in the tenant's warehouse through the guard; the named result, or a 502 carrying the SQL."""
    import asyncio
    import snowflake_client
    from semantic_layer.compiler import named_result

    ctx = guard_context(tenant, user)
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, lambda: _guarded(compiled, ctx))
    if isinstance(result, snowflake_client.Refusal) or result.refused:
        message = result.message if isinstance(result, snowflake_client.Refusal) else result.error
        logger.error("Semantic query failed: %s", message)
        raise HTTPException(status_code=502, detail={"error": message, "sql": compiled.sql})
    if result.error is not None:
        logger.error("Semantic query failed: %s", result.error)
        raise HTTPException(status_code=502, detail={"error": "The warehouse could not run this query.",
                                                     "sql": compiled.sql})
    return named_result({"columns": result.columns, "rows": result.rows, "truncated": result.truncated})


# =============================================================================
# Reports
# =============================================================================

_TERMS_TTL_SECONDS = 3600
_terms_cache: dict[str, tuple[float, list]] = {}
_loaded_reports: Optional[dict] = None


def _reports() -> dict:
    """The canonical report definitions, loaded once per instance."""
    from semantic_layer.reports import load_reports
    global _loaded_reports
    if _loaded_reports is None:
        _loaded_reports = load_reports()
    return _loaded_reports


def _current_terms(tenant: Tenant) -> list[dict]:
    """Every term's name, start and end in the tenant's warehouse, cached per instance and tenant for an hour."""
    import time
    from semantic_layer.catalog import default_catalog
    from semantic_layer.compiler import compile_query, named_result
    from snowflake_client import GuardContext, Refusal

    cached = _terms_cache.get(tenant.tenant_id)
    if cached and time.time() - cached[0] < _TERMS_TTL_SECONDS:
        return cached[1]
    contract = QueryContract(measures=["dataset.terms.v1:terms"], dimensions=["term_name", "term_start", "term_end"], limit=1000)
    catalog = default_catalog()
    ctx = GuardContext.of(tenant, catalog, allow_identity=False)
    result = _guarded(compile_query(contract, catalog, ctx.database), ctx)
    error = result.message if isinstance(result, Refusal) else result.error
    if error is not None:
        logger.warning("Could not load terms for report defaults: %s", error)
        return []
    rows = named_result({"columns": result.columns, "rows": result.rows})["rows"]
    _terms_cache[tenant.tenant_id] = (time.time(), rows)
    return rows


def _report_or_404(report_id: str):
    report = _reports().get(report_id)
    if report is None:
        raise HTTPException(status_code=404, detail=f"Unknown report {report_id}")
    return report


@app.get("/api/v1/reports")
async def list_reports(authorization: Optional[str] = Header(None)):
    """Every report's id, title, area and description."""
    user = _get_user_from_token(authorization)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    tenant_for(user)
    return {"reports": [{"id": r.id, "title": r.title, "area": r.area, "description": r.description}
                        for r in _reports().values()]}


@app.get("/api/v1/reports/{report_id}")
async def get_report(report_id: str, authorization: Optional[str] = Header(None)):
    """A report definition and its filters' default values (current term, last 30 days)."""
    user = _get_user_from_token(authorization)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    tenant = tenant_for(user)
    import asyncio
    from datetime import date
    from semantic_layer.reports import resolve_defaults

    report = _report_or_404(report_id)
    terms = await asyncio.get_running_loop().run_in_executor(None, _current_terms, tenant)
    return {"report": report.model_dump(mode="json"), "defaults": resolve_defaults(report, date.today(), terms)}


class ReportRun(BaseModel):
    visual: str
    query: str
    values: dict = {}


@app.post("/api/v1/reports/{report_id}/run")
async def run_report_query(report_id: str, request: ReportRun, authorization: Optional[str] = Header(None)):
    """Run one query of one visual with the filter-bar values merged in.

    Viewers never see identity: identity dimensions are dropped, and a query of nothing else is unavailable.
    """
    user = _get_user_from_token(authorization)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    tenant = tenant_for(user)
    from roles import role_allows_identity
    from semantic_layer.reports import ReportValueError, merged_contract, output_renames

    report = _report_or_404(report_id)
    visual = report.visual(request.visual)
    if visual is None or request.query not in visual.queries:
        raise HTTPException(status_code=404, detail=f"{report_id} has no query {request.visual}/{request.query}")
    missing = [f for f in report.filters if f.id in report.required_filters and not request.values.get(f.id)]
    if missing:
        return {"unavailable": f"Choose {missing[0].label} to see this"}
    catalog, _ = _catalog_for(tenant)
    try:
        contract, ignored = merged_contract(report, visual, request.query, request.values, catalog, _database_for(tenant))
    except ReportValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not role_allows_identity(user):
        if visual.requires_identity:
            return {"unavailable": "Requires Author access"}
        kept = [d for d in contract.dimensions if not _is_identity(d, catalog)]
        if contract.dimensions and not kept:
            return {"unavailable": "Requires Author access"}
        if len(kept) != len(contract.dimensions):
            kept_names = {d.rpartition(":")[2] for d in kept}
            contract = contract.model_copy(update={
                "dimensions": kept,
                "order_by": [o for o in contract.order_by if o.field in kept_names or not _is_identity(o.field, catalog)],
            })
    compiled = _compile_for(contract, user, tenant)
    result = await _execute(compiled, user, tenant)

    renames = output_renames(report, visual, request.query, request.values)
    columns = [renames.get(c, c) for c in result["columns"]]
    rows = [{renames.get(k, k): v for k, v in r.items()} for r in result["rows"]] if renames else result["rows"]
    return JSONResponse(_json_safe({
        "columns": columns, "rows": rows, "truncated": result.get("truncated", False),
        "sql": compiled.sql, "provenance": compiled.provenance,
        "contract": contract.model_dump(mode="json", exclude_defaults=True), "ignored_filters": ignored,
    }))



# =============================================================================
# Admin: Overlay management
# =============================================================================
# Customer-facing endpoints for managing this tenant's metric overlays. The
# tenant_id is taken from the user's Cognito custom:tenant_id claim — users
# can only see/edit their own tenant's overlays.

ADMIN_GROUP = "illuminate-admins"


def _require_admin(user: Optional[dict]) -> None:
    if ADMIN_GROUP not in (user or {}).get("cognito:groups", []):
        raise HTTPException(status_code=403, detail=f"Requires membership of the {ADMIN_GROUP} group.")


def _require_admin_tenant(user: Optional[dict]) -> Tenant:
    """The caller's registered tenant; 403 unless they are in the admin group and their tenant is set up."""
    _require_admin(user)
    return tenant_for(user)


class OverlayWrite(BaseModel):
    expr: Optional[str] = None
    sql: Optional[str] = None
    default_filters: Optional[list[str]] = None
    description: str = ""
    expected_version: int = 0


class OverlayRevert(BaseModel):
    version: int
    expected_version: int


def _admin_tenant(authorization: Optional[str]) -> tuple[dict, str]:
    user = _get_user_from_token(authorization)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    return user, _require_admin_tenant(user).tenant_id


def _checked_target(target: str) -> str:
    from semantic_layer.overlays import OverlayError, parse_target

    try:
        parse_target(target)
    except OverlayError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return target


def _canonical_value(target: str) -> Optional[dict]:
    """The canonical field an overlay on target replaces; None when the target has no canonical definition."""
    from semantic_layer.catalog import default_catalog
    from semantic_layer.overlays import parse_target

    catalog = default_catalog()
    kind, owner, name = parse_target(target)
    if kind == "metric":
        metric = catalog.metrics.get(owner)
        return {"default_filters": list(metric.default_filters)} if metric else None
    ds = catalog.datasets.get(owner)
    found = ds and (ds.measure(name) if kind == "measure" else ds.filter(name))
    if not found:
        return None
    return {"expr": found.expr} if kind == "measure" else {"sql": found.sql}


def _overlay_errors(tenant_id: str, target: str, candidate) -> list[str]:
    """Problems with the tenant's overlays once target is replaced by candidate (None: removed)."""
    import overlay_store
    from semantic_layer.catalog import default_catalog
    from semantic_layer.overlays import OverlayError, parse_target, validate_overlay

    others = [o for o in overlay_store.list_overlays(tenant_id) if o.target != target]
    base, _, _ = _apply_each(default_catalog(), others)
    if candidate is not None:
        try:
            return validate_overlay(candidate, base)
        except OverlayError as e:
            return [str(e)]
    kind, owner, name = parse_target(target)
    if kind != "filter" or base.datasets.get(owner) is None or base.datasets[owner].filter(name) is not None:
        return []
    users = [o.target for o in others if o.kind == "metric" and o.target.split(":", 1)[1] in base.metrics
             and base.metrics[o.target.split(":", 1)[1]].dataset_id == owner and name in (o.default_filters or [])]
    return [f"{t} uses filter {name!r}; change it first" for t in users]


def _save_checked(tenant_id: str, target: str, candidate, save) -> dict:
    import overlay_store

    errors = [re.sub(r"\x1b\[[0-9;]*m", "", e) for e in _overlay_errors(tenant_id, target, candidate)]
    if errors:
        raise HTTPException(status_code=400, detail={"errors": errors})
    try:
        saved = save()
    except overlay_store.OverlayConflict:
        raise HTTPException(status_code=409, detail="This overlay changed since you loaded it; reload and try again.")
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e).strip("'"))
    _overlay_cache.pop(tenant_id, None)
    return {"tenant_id": tenant_id, "target": target, "overlay": saved.model_dump() if saved else None}


@app.get("/api/v1/admin/settings/mcp")
async def admin_get_mcp_setting(authorization: Optional[str] = Header(None)) -> dict:
    user = _get_user_from_token(authorization)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    return {"enabled": _require_admin_tenant(user).mcp_enabled}


@app.put("/api/v1/admin/settings/mcp")
async def admin_put_mcp_setting(body: Any = Body(None), authorization: Optional[str] = Header(None)) -> dict:
    _, tenant_id = _admin_tenant(authorization)
    enabled = body.get("enabled") if isinstance(body, dict) else None
    if not isinstance(enabled, bool):
        raise HTTPException(status_code=400, detail="enabled must be true or false")
    return {"enabled": (await _blocking(tenancy.set_mcp_enabled, tenant_id, enabled)).mcp_enabled}


@app.get("/api/v1/admin/mcp/setup")
async def admin_mcp_setup(authorization: Optional[str] = Header(None)) -> dict:
    import mcp_access

    _admin_tenant(authorization)
    _, url = await _blocking(mcp_access._mcp_discovery)
    if not url:
        return {"deployed": False}
    return {"deployed": True, "url": url, "snippets": mcp_access.setup_snippets(url)}


@app.get("/api/v1/admin/overlays")
async def admin_list_overlays(authorization: Optional[str] = Header(None)) -> dict:
    """This tenant's current semantic-layer overlays."""
    import overlay_store

    from semantic_layer.catalog import default_catalog

    _, tenant_id = _admin_tenant(authorization)
    overlays = overlay_store.list_overlays(tenant_id)
    _, _, skipped = _apply_each(default_catalog(), overlays)
    return {"tenant_id": tenant_id, "overlays": [
        o.model_dump() | {"status": "skipped" if o.target in skipped else "active", "problems": skipped.get(o.target, [])}
        for o in overlays
    ]}


@app.get("/api/v1/admin/overlay/{target}")
async def admin_get_overlay(target: str, authorization: Optional[str] = Header(None)) -> dict:
    import overlay_store

    _, tenant_id = _admin_tenant(authorization)
    overlay = overlay_store.get_overlay(tenant_id, _checked_target(target))
    return {"tenant_id": tenant_id, "target": target, "overlay": overlay.model_dump() if overlay else None,
            "canonical": _canonical_value(target)}


@app.put("/api/v1/admin/overlay/{target}")
async def admin_put_overlay(target: str, request: OverlayWrite, authorization: Optional[str] = Header(None)) -> dict:
    """Validate, then save as the next version; 400 with reasons, 409 if expected_version is stale."""
    import overlay_store
    from pydantic import ValidationError
    from semantic_layer.overlays import Overlay

    user, tenant_id = _admin_tenant(authorization)
    _checked_target(target)
    try:
        candidate = Overlay(target=target, **request.model_dump(exclude={"expected_version"}))
    except ValidationError as e:
        raise HTTPException(status_code=400, detail={"errors": [err["msg"] for err in e.errors()]})
    result = _save_checked(tenant_id, target, candidate, lambda: overlay_store.put_overlay(
        tenant_id, candidate, user.get("sub", "unknown"), request.expected_version))
    logger.info("Overlay saved: tenant=%s target=%s by=%s", tenant_id, target, user.get("sub"))
    return result


@app.delete("/api/v1/admin/overlay/{target}")
async def admin_delete_overlay(target: str, expected_version: int,
                               authorization: Optional[str] = Header(None)) -> dict:
    """Remove the overlay (its history stays); 400 if another overlay depends on it, 409 if it changed."""
    import overlay_store

    user, tenant_id = _admin_tenant(authorization)
    _checked_target(target)
    result = _save_checked(tenant_id, target, None,
                           lambda: overlay_store.delete_overlay(tenant_id, target, expected_version))
    logger.info("Overlay deleted: tenant=%s target=%s by=%s", tenant_id, target, user.get("sub"))
    return result


@app.get("/api/v1/admin/overlay/{target}/history")
async def admin_overlay_history(target: str, authorization: Optional[str] = Header(None)) -> dict:
    import overlay_store

    _, tenant_id = _admin_tenant(authorization)
    history = overlay_store.history(tenant_id, _checked_target(target))
    return {"tenant_id": tenant_id, "target": target, "history": [o.model_dump() for o in history]}


@app.post("/api/v1/admin/overlay/{target}/revert")
async def admin_revert_overlay(target: str, request: OverlayRevert, authorization: Optional[str] = Header(None)) -> dict:
    """Save an earlier version's content as the newest version, after validating it."""
    import overlay_store

    user, tenant_id = _admin_tenant(authorization)
    _checked_target(target)
    old = next((o for o in overlay_store.history(tenant_id, target) if o.version == request.version), None)
    if old is None:
        raise HTTPException(status_code=404, detail=f"{target} has no version {request.version}")
    return _save_checked(tenant_id, target, old, lambda: overlay_store.revert(
        tenant_id, target, request.version, user.get("sub", "unknown"), request.expected_version))


# =============================================================================
# Lambda Web Adapter entry point
# =============================================================================
# When run as __main__ (via run.sh), start uvicorn.  LWA handles proxying
# Lambda invocations to the local HTTP server and streaming responses back.

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8080"))
    logger.info(f"Starting uvicorn on port {port}")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
