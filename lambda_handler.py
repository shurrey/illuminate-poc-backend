"""
Illuminate Conversational Intelligence - API Proxy Lambda

Thin Lambda handler wrapping FastAPI/uvicorn via Lambda Web Adapter (LWA).
Uses chat_engine for all LLM orchestration and conversation_store for history.

Request flow:
    Frontend -> Lambda Function URL (RESPONSE_STREAM) -> LWA -> uvicorn/FastAPI
        -> chat_engine (Bedrock Converse) -> Snowflake / MCP tools
"""
import os
import json
import math
import logging
import re
import uuid
from typing import Optional

from fastapi import FastAPI, HTTPException, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.encoders import jsonable_encoder
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from semantic_layer.contract import QueryContract

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
SNOWFLAKE_SECRET_NAME = os.environ.get("SNOWFLAKE_SECRET_NAME", "illuminate/dev/snowflake")

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
    """Return the user's tenant_id Cognito claim, if present.

    Looks for `custom:tenant_id` (only in ID tokens). Returns None for
    access tokens or users without the attribute — caller falls back to
    canonical-only behavior.
    """
    if not user:
        return None
    value = user.get("custom:tenant_id")
    return value.strip() if isinstance(value, str) and value.strip() else None


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


def _engine_kwargs(user: Optional[dict]) -> dict:
    """Tools and a system prompt over the caller's overlaid catalog, when their tenant has overlays."""
    catalog, overlays = _catalog_for(user)
    if not overlays:
        return {}
    from chat_engine import _database
    from semantic_layer.chat_tools import ChatTools
    from semantic_layer.prompt import build_system_prompt

    return {"tools": ChatTools(catalog, _database, overlays=overlays),
            "system_prompt": build_system_prompt(catalog, _database)}


async def send_message(
    message_text: str,
    owner: str,
    context_id: Optional[str] = None,
    user: Optional[dict] = None,
) -> dict:
    """Send a message via chat_engine (non-streaming)."""
    import asyncio
    from chat_engine import send_message as engine_send
    from conversation_store import load_history, save_turn

    context_id = _conversation_id(context_id, owner)
    bedrock_history, model_text = _model_turns(load_history(context_id, owner), message_text)
    kwargs = _engine_kwargs(user)

    loop = asyncio.get_event_loop()
    response_text, _, artifacts = await loop.run_in_executor(
        None, lambda: engine_send(model_text, bedrock_history, **kwargs)
    )
    response_text = _scrub_pii(response_text)

    save_turn(context_id, owner, message_text, response_text, _queries_from(artifacts))

    return {"text": response_text, "artifacts": artifacts, "contextId": context_id}


async def send_message_streaming(
    message_text: str,
    owner: str,
    context_id: Optional[str] = None,
    user: Optional[dict] = None,
):
    """Stream a response via chat_engine, yielding frontend events."""
    import asyncio
    from chat_engine import send_message_streaming as engine_stream
    from conversation_store import load_history, save_turn

    yield {"type": "status", "message": "Processing your question..."}

    # DynamoDB calls are blocking; keep them off the event loop that serves the stream.
    loop = asyncio.get_running_loop()
    context_id = await loop.run_in_executor(None, _conversation_id, context_id, owner)
    history = await loop.run_in_executor(None, load_history, context_id, owner)
    bedrock_history, model_text = _model_turns(history, message_text)
    engine_kwargs = await loop.run_in_executor(None, _engine_kwargs, user)

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

    try:
        result = await send_message(
            message_text=message_text,
            owner=user["sub"],
            context_id=context_id,
            user=user,
        )

        return ChatResponse(
            text=result.get("text", ""),
            artifacts=_json_safe(result["artifacts"]),
            context_id=result.get("contextId", context_id),
        )

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

    if request_id:
        _request_owners[request_id] = user["sub"]

    async def event_generator():
        """Relay SSE events from chat_engine to the frontend."""
        try:
            async for event in send_message_streaming(
                message_text=message_text,
                owner=user["sub"],
                context_id=context_id,
                user=user,
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
    if _request_owners.get(request_id) != user["sub"]:
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
    loop = asyncio.get_running_loop()
    return {"conversations": await loop.run_in_executor(None, list_for, user["sub"])}


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
    if not owns(context_id, user["sub"]):
        raise HTTPException(status_code=404, detail="Conversation not found")
    return {"messages": load_history(context_id, user["sub"])}


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
    if not clear_history(context_id, user["sub"]):
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
    Schema must start with CDM_, limit capped at 100.
    """
    user = _get_user_from_token(authorization)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    # Raw rows include person tables, so previews are for administrators only.
    _require_admin(user)

    # Validate identifiers
    if not _SAFE_IDENTIFIER.match(schema) or not _SAFE_IDENTIFIER.match(table):
        raise HTTPException(status_code=400, detail="Invalid identifier: only alphanumeric and underscore allowed")

    # Whitelist schemas to CDM_* only
    if not schema.upper().startswith("CDM_"):
        raise HTTPException(status_code=400, detail="Schema must start with CDM_")

    # Cap limit
    limit = min(max(1, limit), 100)

    try:
        import asyncio
        from snowflake_client import query_preview

        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None, lambda: query_preview(schema.upper(), table.upper(), limit)
        )
        logger.info(f"Preview: {schema}.{table} returned {len(result['rows'])} rows")
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Snowflake preview query failed: {e}")
        raise HTTPException(status_code=502, detail="Failed to query sample data")


# =============================================================================
# Semantic layer
# =============================================================================


def _semantic_database() -> str:
    """Snowflake database the semantic layer's {{ database }} placeholder resolves to."""
    db = os.environ.get("SNOWFLAKE_DATABASE")
    if db:
        return db
    from chat_engine import _database
    return _database


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


def _catalog_for(user: Optional[dict]) -> tuple:
    """The canonical catalog with the caller's tenant overlays applied, and the overlays used."""
    from semantic_layer.catalog import default_catalog

    tenant_id = _tenant_id_from_user(user)
    if not tenant_id:
        return default_catalog(), []
    catalog, applied, _ = _tenant_state(tenant_id)
    return catalog, applied


def _compile_contract(contract: QueryContract, authorization: str):
    """Authenticate, then compile; raises the HTTP error the caller should return."""
    user = _get_user_from_token(authorization)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    from semantic_layer.compiler import CompileError, compile_query
    from semantic_layer.overlays import overlays_used

    catalog, overlays = _catalog_for(user)
    try:
        compiled = compile_query(contract, catalog, _semantic_database())
    except CompileError as e:
        raise HTTPException(status_code=400, detail=str(e))
    compiled.provenance.overlays = overlays_used(compiled.provenance, catalog, overlays)
    return compiled


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

    import hashlib
    from fastapi import Response
    from fastapi.responses import JSONResponse
    from semantic_layer.catalog_view import public_catalog

    body = public_catalog(_catalog_for(user)[0])
    etag = '"' + hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:32] + '"'
    headers = {"ETag": etag, "Cache-Control": "private, no-cache"}
    if if_none_match and _etag_matches(if_none_match, etag):
        return Response(status_code=304, headers=headers)
    return JSONResponse(body, headers=headers)


@app.post("/api/v1/semantic/query")
async def semantic_query(contract: QueryContract, authorization: Optional[str] = Header(None)):
    """Compile a semantic query contract and run it through the execution guard."""
    compiled = _compile_contract(contract, authorization)

    import asyncio
    from snowflake_client import validate_and_execute

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, lambda: validate_and_execute(compiled.sql, compiled=True))
    if "error" in result:
        logger.error("Semantic query failed: %s", result["error"])
        message = "The warehouse could not run this query." if result.get("warehouse_error") else result["error"]
        raise HTTPException(status_code=502, detail={"error": message, "sql": compiled.sql})
    from fastapi.responses import JSONResponse
    from semantic_layer.compiler import named_result

    result = named_result(result)
    # jsonable_encoder turns Snowflake's Decimal into numbers; response-model serialisation makes them strings.
    return JSONResponse(_json_safe({
        "columns": result["columns"],
        "rows": result["rows"],
        "sql": compiled.sql,
        "provenance": compiled.provenance,
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


def _require_admin_tenant(user: Optional[dict]) -> str:
    """The caller's tenant_id; 403 unless they are in the admin group and carry a tenant."""
    _require_admin(user)
    tid = _tenant_id_from_user(user)
    if not tid:
        raise HTTPException(
            status_code=403,
            detail=(
                "No tenant_id on this token. The frontend must send the ID token "
                "(not the access token) to admin endpoints."
            ),
        )
    return tid


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
    return user, _require_admin_tenant(user)


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
