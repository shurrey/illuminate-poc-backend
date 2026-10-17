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
from typing import Optional

from fastapi import FastAPI, HTTPException, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

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
    """What each answer ran, kept with the turn so follow-ups can modify it."""
    out = []
    for a in artifacts:
        if a.get("type") != "sql":
            continue
        governed = a.get("provenance", {}).get("governed", False)
        entry = {"title": a.get("title"), "governed": governed}
        entry["query" if governed else "sql"] = a.get("query") if governed else a.get("data")
        out.append(entry)
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
    return f"<previous_queries>{json.dumps(queries)}</previous_queries>\n\n{text}"


def _engine_kwargs(tenant: Tenant, user: dict) -> dict:
    """The chat engine's tools and system prompt over the tenant's overlaid catalog and database."""
    from semantic_layer.chat_tools import ChatTools
    from semantic_layer.prompt import build_system_prompt

    from roles import role_allows_identity

    catalog, overlays = _catalog_for(tenant)
    database = _database_for(tenant)
    allow_identity = role_allows_identity(user)

    def audit(kind: str, subject) -> None:
        if kind == "freehand":
            logger.info("identity_query sub=%s freehand=true", user.get("sub"))
        elif _selects_identity(subject, catalog):
            logger.info("identity_query sub=%s contract=%s", user.get("sub"), subject.model_dump_json(exclude_defaults=True))

    tools = ChatTools(catalog, database, tenant=tenant, overlays=overlays,
                      allow_identity=allow_identity, identity_audit=audit)
    return {"tools": tools,
            "system_prompt": build_system_prompt(catalog, database, learn=allows_learn(tenant))}


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

    return {
        "tenant": {"name": tenant.name, "license": tenant.license},
        "roles": user_roles,
        "is_admin": ADMIN_GROUP in groups,
    }


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


def _cards_view(tenant_id: str, sub: str) -> dict:
    """Built-ins merged with the user's custom cards in layout order; an unlisted built-in or card is appended.
    hidden lists the hidden built-ins in default order and disabled the disabled cards in layout order."""
    import user_items_store

    layout, version = _load_layout(tenant_id, sub)
    by_id = {c["id"]: {**c, "isBuiltIn": True} for c in _default_cards() if c["id"] not in layout["hidden"]}
    for item in user_items_store.list_kind(tenant_id, sub, "card"):
        by_id[item["id"]] = _custom_card(item)
    ordered = [i for i in dict.fromkeys(layout["order"]) if i in by_id]
    ordered += [i for i in by_id if i not in ordered]
    return {
        "cards": [{**by_id[i], "enabled": i not in layout["disabled"]} for i in ordered],
        "hidden": [c["id"] for c in _default_cards() if c["id"] in layout["hidden"]],
        "disabled": [i for i in ordered if i in layout["disabled"]],
        "version": version,
    }


def _cards_caller(authorization: Optional[str]) -> tuple[str, str]:
    user = _get_user_from_token(authorization)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    return tenant_for(user).tenant_id, user["sub"]


async def _blocking(fn, *args):
    return await asyncio.get_running_loop().run_in_executor(None, fn, *args)


@app.get("/api/v1/me/cards")
async def get_my_cards(authorization: Optional[str] = Header(None)) -> dict:
    """The caller's home cards in layout order, built-ins included."""
    tenant_id, sub = _cards_caller(authorization)
    return await _blocking(_cards_view, tenant_id, sub)


@app.put("/api/v1/me/cards/layout")
async def put_my_card_layout(payload: dict, authorization: Optional[str] = Header(None)) -> dict:
    """Replace order, disabled and hidden; 400 on ids that are not built-in or the caller's own, 409 if stale."""
    import user_items_store

    tenant_id, sub = _cards_caller(authorization)
    version = payload.get("expected_version")
    lists = {k: payload.get(k) for k in ("order", "disabled", "hidden")}
    if isinstance(version, bool) or not isinstance(version, int) or version < 0:
        raise _bad_request("expected_version must be a non-negative integer")
    if any(not isinstance(v, list) or any(not isinstance(i, str) for i in v) for v in lists.values()):
        raise _bad_request("order, disabled and hidden must be lists of card ids")
    cap = len(_default_cards()) + MAX_CUSTOM_CARDS
    if any(len(v) > cap for v in lists.values()):
        raise _bad_request(f"order, disabled and hidden can each list at most {cap} cards")
    lists = {k: list(dict.fromkeys(v)) for k, v in lists.items()}

    def save() -> dict:
        builtin = {c["id"] for c in _default_cards()}
        own = {i["id"] for i in user_items_store.list_kind(tenant_id, sub, "card")}
        unknown = sorted({i for v in lists.values() for i in v} - builtin - own)
        not_builtin = sorted(set(lists["hidden"]) - builtin)
        if unknown or not_builtin:
            raise _bad_request(*[f"unknown card id {i}" for i in unknown],
                               *[f"only built-in cards can be hidden: {i}" for i in not_builtin if i not in unknown])
        _save_layout(tenant_id, sub, lists, version)
        return _cards_view(tenant_id, sub)

    return await _blocking(save)


@app.post("/api/v1/me/cards/reset")
async def reset_my_cards(authorization: Optional[str] = Header(None)) -> dict:
    """Unhide and enable the built-ins in their original order, followed by the caller's custom cards."""
    import user_items_store

    tenant_id, sub = _cards_caller(authorization)

    def reset() -> dict:
        builtin = [c["id"] for c in _default_cards()]
        custom = [i["id"] for i in user_items_store.list_kind(tenant_id, sub, "card")]

        def apply(layout: dict, version: int) -> None:
            order = builtin + [i for i in dict.fromkeys(layout["order"]) if i in custom]
            order += [i for i in custom if i not in order]
            layout.update(order=order, disabled=[i for i in layout["disabled"] if i in custom], hidden=[])

        _mutate_layout(tenant_id, sub, apply)
        return _cards_view(tenant_id, sub)

    return await _blocking(reset)


@app.post("/api/v1/me/cards")
async def create_my_card(payload: dict, authorization: Optional[str] = Header(None)) -> dict:
    """Create a custom card with a server-generated id and append it to the layout; 400 at 100 cards."""
    import user_items_store

    tenant_id, sub = _cards_caller(authorization)
    body = _card_body(payload)

    def create() -> dict:
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

    return await _blocking(create)


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

    tenant_id, sub = _cards_caller(authorization)

    def remove() -> dict:
        def hide(layout: dict, version: int) -> None:
            if card_id not in layout["hidden"]:
                layout["hidden"].append(card_id)

        def forget(layout: dict, version: int) -> None:
            for key in layout:
                layout[key] = [i for i in layout[key] if i != card_id]

        if card_id in {c["id"] for c in _default_cards()}:
            _mutate_layout(tenant_id, sub, hide)
        elif _CARD_ID.fullmatch(card_id) and user_items_store.delete(tenant_id, sub, "card", card_id):
            _mutate_layout(tenant_id, sub, forget)
        else:
            raise HTTPException(status_code=404, detail="Card not found")
        return _cards_view(tenant_id, sub)

    return await _blocking(remove)


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


def _stored_query(item: dict) -> dict:
    """The query's body fields; rows saved before the rename carry `kind` instead of `query_kind`."""
    body = {k: v for k, v in item.items() if k not in ("id", "version", "updated_at", "kind")}
    body["query_kind"] = item.get("query_kind") or item.get("kind") or "governed"
    return body


def _query_view(item: dict) -> dict:
    return {**_stored_query(item), "id": item["id"]}


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
        return {"queries": [_query_view(i) for i in items]}

    return await _blocking(load)


@app.post("/api/v1/me/queries")
async def create_my_query(payload: dict, authorization: Optional[str] = Header(None)) -> dict:
    """Save a governed query with a server-generated id; 400 at 500 queries."""
    import user_items_store

    tenant_id, sub = _cards_caller(authorization)
    body = {**_query_body(payload), "query_kind": "governed", "createdAt": _now_iso(), "lastUsedAt": None}

    def create() -> dict:
        if user_items_store.count(tenant_id, sub, "query") >= MAX_QUERIES:
            raise _bad_request(f"You can save at most {MAX_QUERIES} queries")
        return _query_view(user_items_store.put(tenant_id, sub, "query", f"q_{uuid.uuid4().hex[:12]}", body))

    return await _blocking(create)


@app.put("/api/v1/me/queries/{query_id}")
async def update_my_query(query_id: str, payload: dict, authorization: Optional[str] = Header(None)) -> dict:
    """Update name, description, prompt and contract of one of the caller's queries."""
    tenant_id, sub = _cards_caller(authorization)
    body = _query_body(payload)
    return await _blocking(_replace_query, tenant_id, sub, query_id, lambda b: b.update(body))


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
        if not (_QUERY_ID.fullmatch(query_id) and user_items_store.delete(tenant_id, sub, "query", query_id)):
            raise HTTPException(status_code=404, detail="Query not found")
        return {"deleted": query_id}

    return await _blocking(remove)


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


def _import_queries(tenant_id: str, sub: str, queries: list, skipped: list) -> int:
    import user_items_store

    room = MAX_QUERIES - user_items_store.count(tenant_id, sub, "query")
    created = 0
    for i, query in enumerate(queries):
        name = _name_of(query, "name")
        try:
            body = _query_body(query)
            if room <= 0:
                raise _bad_request(f"You can save at most {MAX_QUERIES} queries")
        except HTTPException as e:
            skipped.append({"kind": "query", "name": name, "reason": _skip_reason(e)})
            continue
        source_id = query.get("id") if isinstance(query.get("id"), str) else f"index-{i}"
        body.update(query_kind="governed", createdAt=_iso_or(query.get("createdAt"), _now_iso()),
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

    tenant_id, sub = _cards_caller(authorization)
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
        n_queries = _import_queries(tenant_id, sub, queries, skipped)
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
        logger.info("identity_query sub=%s contract=%s", user.get("sub"), contract.model_dump_json(exclude_defaults=True))
    return compiled


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

    # jsonable_encoder turns Snowflake's Decimal into numbers; response-model serialisation makes them strings.
    return JSONResponse(_json_safe({
        "columns": result["columns"],
        "rows": result["rows"],
        "sql": compiled.sql,
        "provenance": compiled.provenance,
    }))


async def _execute(compiled, user: dict, tenant: Tenant) -> dict:
    """Run compiled SQL in the tenant's warehouse through the guard, with the caller's identity rights and
    every dataset's declared PII columns; the named result, or a 502 carrying the SQL."""
    import asyncio
    import snowflake_client
    from roles import role_allows_identity
    from semantic_layer.compiler import named_result

    allow_identity = role_allows_identity(user)
    extra_pii = frozenset(c.upper() for ds in _catalog_for(tenant)[0].datasets.values() for c in ds.pii_columns)
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, lambda: snowflake_client.validate_and_execute(
        compiled.sql, compiled=True, allow_identity=allow_identity, extra_pii=extra_pii, tenant=tenant))
    if "error" in result:
        logger.error("Semantic query failed: %s", result["error"])
        message = "The warehouse could not run this query." if result.get("warehouse_error") else result["error"]
        raise HTTPException(status_code=502, detail={"error": message, "sql": compiled.sql})
    return named_result(result)


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
    from snowflake_client import validate_and_execute

    cached = _terms_cache.get(tenant.tenant_id)
    if cached and time.time() - cached[0] < _TERMS_TTL_SECONDS:
        return cached[1]
    contract = QueryContract(measures=["dataset.terms.v1:terms"], dimensions=["term_name", "term_start", "term_end"], limit=1000)
    sql = compile_query(contract, default_catalog(), _database_for(tenant)).sql
    result = validate_and_execute(sql, compiled=True, tenant=tenant)
    if "error" in result:
        logger.warning("Could not load terms for report defaults: %s", result["error"])
        return []
    rows = named_result(result)["rows"]
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
