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


def _scrub_pii(text: str) -> str:
    """Scrub PII patterns from response text as a last-resort safety net.

    Runs AFTER the LLM generates text, before returning to the user.
    This catches anything the Bedrock Guardrail or prompt-based approach missed.
    """
    for pattern, replacement in _PII_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


# =============================================================================
# Configuration from environment variables
# =============================================================================

USER_POOL_ID = os.environ.get("USER_POOL_ID", "")
USER_POOL_CLIENT_ID = os.environ.get("USER_POOL_CLIENT_ID", "")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
ACCOUNT_ID = os.environ.get("ACCOUNT_ID", "442606396405")
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
        # Verify client_id for access tokens or aud for ID tokens
        token_client = claims.get("client_id") or claims.get("aud")
        if token_client != USER_POOL_CLIENT_ID:
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


def _model_history(history: list[dict]) -> list[dict]:
    messages = []
    for msg in history:
        text = msg["content"]
        if msg.get("queries"):
            text += "\n\nQueries behind this answer (modify these for follow-up questions):\n" + json.dumps(msg["queries"])
        messages.append({"role": msg["role"], "content": [{"text": text}]})
    return messages

async def send_message(
    message_text: str,
    owner: str,
    context_id: Optional[str] = None,
) -> dict:
    """Send a message via chat_engine (non-streaming)."""
    import asyncio
    from chat_engine import send_message as engine_send
    from conversation_store import load_history, save_turn

    context_id = _conversation_id(context_id, owner)
    bedrock_history = _model_history(load_history(context_id, owner))

    loop = asyncio.get_event_loop()
    response_text, _, artifacts = await loop.run_in_executor(
        None, lambda: engine_send(message_text, bedrock_history)
    )

    save_turn(context_id, owner, message_text, response_text, _queries_from(artifacts))

    return {"text": response_text, "artifacts": artifacts, "contextId": context_id}


async def send_message_streaming(
    message_text: str,
    owner: str,
    context_id: Optional[str] = None,
):
    """Stream a response via chat_engine, yielding frontend events."""
    from chat_engine import send_message_streaming as engine_stream
    from conversation_store import load_history, save_turn

    yield {"type": "status", "message": "Processing your question..."}

    context_id = _conversation_id(context_id, owner)
    bedrock_history = _model_history(load_history(context_id, owner))

    full_text, artifacts = "", []
    try:
        async for event in engine_stream(message_text, bedrock_history):
            if event["type"] == "status":
                yield event
            elif event["type"] == "raw_complete":
                full_text, artifacts = event["text"], event["artifacts"]
                save_turn(context_id, owner, message_text, full_text, _queries_from(artifacts))

        if not full_text:
            yield {"type": "error", "message": "Empty response"}
            return

        yield {
            "type": "complete",
            "data": {
                "text": _scrub_pii(full_text),
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

app = FastAPI(
    title="Illuminate Conversational Intelligence - API",
    description="FastAPI handler using chat_engine for LLM orchestration",
    version="0.3.0"
)

# CORS middleware
origins = [o.strip() for o in ALLOWED_ORIGINS.split(",")]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
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
        version="0.3.0",
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
        )

        return ChatResponse(
            text=_scrub_pii(result.get("text", "")),
            artifacts=jsonable_encoder(result["artifacts"]),
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
    Send a message and receive streaming response via Server-Sent Events.

    The request is forwarded to the Orchestrator AgentCore runtime, and
    streaming events are relayed back to the frontend.
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

                # Tool artifacts carry Snowflake Decimal/date values that plain json.dumps rejects.
                event_data = json.dumps(jsonable_encoder(event))
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
# Dashboard Query Endpoint
# =============================================================================

class DashboardQueryRequest(BaseModel):
    """Request body for dashboard SQL queries. Supports optional bind parameters."""
    sql: str
    params: Optional[dict] = None


class DashboardMetricRequest(BaseModel):
    """Request body for canonical-metric-backed dashboard queries.

    The frontend ships a `metric_id` that resolves to a vetted SQL template in
    `canonical/metrics.yaml`. The backend compiles it (applying any tenant
    overlay — none in MVP) and executes against Snowflake.
    """
    metric_id: str


@app.post("/api/v1/dashboard/query")
async def dashboard_query(
    request: DashboardQueryRequest,
    authorization: Optional[str] = Header(None),
):
    """Execute caller-supplied SQL for dashboard widgets through the execution guard.

    The guard limits it to read-only CDM_* queries with PII only inside counts, the same
    rules as chat's freehand SQL. Returns columns + rows, or {error}.
    """
    user = _get_user_from_token(authorization)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    if not request.sql.strip():
        return {"error": "Empty SQL query"}

    try:
        import asyncio
        from snowflake_client import validate_and_execute

        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None, lambda: validate_and_execute(request.sql, params=request.params)
        )
        if "rows" in result:
            logger.info(f"Dashboard query returned {len(result['rows'])} rows")
        return result
    except ValueError as e:
        return {"error": str(e)}
    except Exception as e:
        logger.error(f"Dashboard query failed: {e}")
        return {"error": str(e)}


@app.post("/api/v1/dashboard/metric")
async def dashboard_metric(
    request: DashboardMetricRequest,
    authorization: Optional[str] = Header(None),
):
    """Execute a canonical metric for a dashboard widget.

    Same response shape as /api/v1/dashboard/query (`{columns, rows}`) so the
    frontend `useDashboardCards` hook needs no result-side changes. Returns
    `{error}` on validation failure or unknown metric_id.
    """
    user = _get_user_from_token(authorization)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    # Lazy import — the semantic_layer package and its YAML are only loaded
    # when the endpoint is hit, so Lambda cold start is not affected for the
    # chat path. chat_engine.py separately imports semantic_layer at module
    # top to inject the catalog summary into the system prompt; that import
    # has already happened by the time this endpoint fires.
    from semantic_layer.engine import (
        SqlSafetyError,
        compile_sql,
        load_canonical,
        resolve,
    )

    canonical = load_canonical()
    if request.metric_id not in canonical.metrics:
        return {
            "error": f"unknown metric_id: {request.metric_id!r}",
            "available_metrics": sorted(canonical.metrics.keys()),
        }

    # Load this user's tenant overlay (if any) and resolve the merged metric.
    tenant_id = _tenant_id_from_user(user)
    tenant = None
    if tenant_id:
        try:
            import tenant_store
            tenant = tenant_store.load_tenant(tenant_id)
        except Exception as e:  # noqa: BLE001
            logger.warning("Failed to load tenant %s: %s", tenant_id, e)

    try:
        merged = resolve(canonical, tenant, request.metric_id)
        # Resolve the database from chat_engine (already computed at import).
        from chat_engine import _database
        sql = compile_sql(
            merged, filters=[], dimensions=[], database=_database
        )
    except SqlSafetyError as e:
        return {"error": f"SQL safety violation in metric definition: {e}"}
    except KeyError as e:
        return {"error": f"failed to resolve metric: {e}"}

    try:
        import asyncio
        from snowflake_client import query_sql

        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, lambda: query_sql(sql, params=None))
        logger.info(
            "Dashboard metric %s returned %d rows",
            request.metric_id, len(result.get("rows", [])),
        )
        return result
    except ValueError as e:
        return {"error": str(e), "sql_attempted": sql}
    except Exception as e:
        logger.error(f"Dashboard metric {request.metric_id} failed: {e}")
        return {"error": str(e), "sql_attempted": sql}


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


def _compile_contract(contract: QueryContract, authorization: str):
    """Authenticate, then compile; raises the HTTP error the caller should return."""
    user = _get_user_from_token(authorization)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    from semantic_layer.catalog import default_catalog
    from semantic_layer.compiler import CompileError, compile_query

    try:
        return compile_query(contract, default_catalog(), _semantic_database())
    except CompileError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/v1/semantic/compile")
async def semantic_compile(contract: QueryContract, authorization: Optional[str] = Header(None)) -> dict:
    """Compile a semantic query contract to SQL without executing it."""
    return _compile_contract(contract, authorization).model_dump()


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
    from semantic_layer.catalog import default_catalog
    from semantic_layer.catalog_view import public_catalog

    body = public_catalog(default_catalog())
    etag = '"' + hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:32] + '"'
    if if_none_match == etag:
        return Response(status_code=304, headers={"ETag": etag})
    return JSONResponse(body, headers={"ETag": etag})


@app.post("/api/v1/semantic/query")
async def semantic_query(contract: QueryContract, authorization: Optional[str] = Header(None)):
    """Compile a semantic query contract and run it through the execution guard."""
    compiled = _compile_contract(contract, authorization)

    import asyncio
    from snowflake_client import validate_and_execute

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, lambda: validate_and_execute(compiled.sql))
    if "error" in result:
        logger.error("Semantic query failed: %s", result["error"])
        raise HTTPException(status_code=502, detail={"error": result["error"], "sql": compiled.sql})
    from fastapi.encoders import jsonable_encoder
    from fastapi.responses import JSONResponse

    # jsonable_encoder turns Snowflake's Decimal into numbers; response-model serialisation makes them strings.
    return JSONResponse(jsonable_encoder({
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

class OverlayPutRequest(BaseModel):
    measure_sql: str
    diff_description: str = ""
    owner: str = ""
    last_reviewed: Optional[str] = None  # ISO date; defaults to today server-side


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


@app.get("/api/v1/admin/metrics")
async def admin_list_metrics(authorization: Optional[str] = Header(None)) -> dict:
    """List all canonical metrics + this tenant's current overlay state.

    Each entry: id, display_name, description, owner (canonical), entity,
    canonical_sql, and either `overlay` (the tenant's override) or
    `overlay: None`. This is the data the admin UI's metric list renders.
    """
    user = _get_user_from_token(authorization)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    tenant_id = _require_admin_tenant(user)

    from semantic_layer.engine import load_canonical
    import tenant_store

    canonical = load_canonical()
    tenant = tenant_store.load_tenant(tenant_id)
    out = []
    for mid, m in canonical.metrics.items():
        overlay = tenant.overlays.get(mid)
        out.append({
            "id": m.id,
            "display_name": m.display_name,
            "description": m.description.strip(),
            "owner": m.owner,
            "entity": m.entity,
            "canonical_sql": m.measure_sql,
            "synonyms": m.synonyms,
            "overlay": (
                {
                    "owner": overlay.owner,
                    "last_reviewed": overlay.last_reviewed.isoformat(),
                    "diff_description": overlay.diff_description,
                    "measure_sql": overlay.measure_sql,
                }
                if overlay
                else None
            ),
        })
    return {"tenant_id": tenant_id, "metrics": out}


@app.get("/api/v1/admin/overlay/{metric_id}")
async def admin_get_overlay(
    metric_id: str,
    authorization: Optional[str] = Header(None),
) -> dict:
    """Return the current overlay for one metric, or null if none exists."""
    user = _get_user_from_token(authorization)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    tenant_id = _require_admin_tenant(user)

    from semantic_layer.engine import load_canonical
    import tenant_store

    canonical = load_canonical()
    if metric_id not in canonical.metrics:
        raise HTTPException(status_code=404, detail=f"Unknown metric: {metric_id}")
    overlay = tenant_store.get_overlay(tenant_id, metric_id)
    if overlay is None:
        return {"tenant_id": tenant_id, "metric_id": metric_id, "overlay": None}
    return {
        "tenant_id": tenant_id,
        "metric_id": metric_id,
        "overlay": {
            "owner": overlay.owner,
            "last_reviewed": overlay.last_reviewed.isoformat(),
            "diff_description": overlay.diff_description,
            "measure_sql": overlay.measure_sql,
        },
    }


@app.put("/api/v1/admin/overlay/{metric_id}")
async def admin_put_overlay(
    metric_id: str,
    request: OverlayPutRequest,
    authorization: Optional[str] = Header(None),
) -> dict:
    """Create or update this tenant's overlay for one metric.

    Validates by compiling the overlay's SQL through the engine — same
    SELECT-only + allowed-tables guard the canonical metrics get. Bad SQL
    returns 400 with the validator's reason; nothing is persisted on failure.
    """
    user = _get_user_from_token(authorization)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    tenant_id = _require_admin_tenant(user)

    from semantic_layer.engine import (
        SqlSafetyError,
        compile_sql,
        load_canonical,
        resolve,
    )
    from semantic_layer.models import OverlayMetric
    from datetime import date
    import tenant_store

    canonical = load_canonical()
    if metric_id not in canonical.metrics:
        raise HTTPException(status_code=404, detail=f"Unknown metric: {metric_id}")

    # Build a candidate Tenant with just this overlay to validate end-to-end.
    candidate_overlay = OverlayMetric(
        canonical_id=metric_id,
        owner=request.owner or "Unknown",
        last_reviewed=(
            date.fromisoformat(request.last_reviewed)
            if request.last_reviewed
            else date.today()
        ),
        diff_description=request.diff_description,
        measure_sql=request.measure_sql,
    )
    from semantic_layer.models import Glossary, Tenant
    candidate_tenant = Tenant(
        id=tenant_id,
        display_name=tenant_id,
        overlays={metric_id: candidate_overlay},
        glossary=Glossary(synonyms={}),
    )

    try:
        merged = resolve(canonical, candidate_tenant, metric_id)
        from chat_engine import _database
        compile_sql(merged, filters=[], dimensions=[], database=_database)
    except SqlSafetyError as e:
        raise HTTPException(status_code=400, detail=f"SQL safety violation: {e}") from e
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to compile overlay: {e}") from e

    # Validation passed — persist.
    persisted = tenant_store.put_overlay(
        tenant_id=tenant_id,
        metric_id=metric_id,
        measure_sql=request.measure_sql,
        diff_description=request.diff_description,
        owner=request.owner or "Unknown",
        updated_by=user.get("sub", "unknown"),
        last_reviewed=request.last_reviewed,
    )
    logger.info(
        "Overlay saved: tenant=%s metric=%s by=%s", tenant_id, metric_id, user.get("sub")
    )
    return {
        "tenant_id": tenant_id,
        "metric_id": metric_id,
        "overlay": {
            "owner": persisted.owner,
            "last_reviewed": persisted.last_reviewed.isoformat(),
            "diff_description": persisted.diff_description,
            "measure_sql": persisted.measure_sql,
        },
    }


@app.delete("/api/v1/admin/overlay/{metric_id}")
async def admin_delete_overlay(
    metric_id: str,
    authorization: Optional[str] = Header(None),
) -> dict:
    """Remove this tenant's overlay for one metric. Canonical applies after."""
    user = _get_user_from_token(authorization)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    tenant_id = _require_admin_tenant(user)

    import tenant_store
    tenant_store.delete_overlay(tenant_id, metric_id)
    logger.info(
        "Overlay deleted: tenant=%s metric=%s by=%s",
        tenant_id, metric_id, user.get("sub"),
    )
    return {"tenant_id": tenant_id, "metric_id": metric_id, "overlay": None}


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
