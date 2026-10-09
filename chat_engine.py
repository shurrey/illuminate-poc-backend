"""
chat_engine.py — Bedrock Converse tool loop over the semantic layer.

The system prompt is built from the semantic catalog; the model answers through the
semantic tools (search_catalog, query_semantic) and falls back to labelled freehand SQL.
Tool artifacts are collected for the client alongside the model's text.
"""

import asyncio
import json
import logging
import os
from typing import Optional

import boto3

from semantic_layer.catalog import default_catalog
from semantic_layer.chat_tools import ChatTools
from semantic_layer.prompt import build_system_prompt

logger = logging.getLogger("API-PROXY")

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-6")

_bedrock = boto3.client("bedrock-runtime", region_name=AWS_REGION)


def _resolve_database() -> str:
    """SNOWFLAKE_DATABASE if set, else the database in the Snowflake secret, else ILLUMINATE."""
    db = os.environ.get("SNOWFLAKE_DATABASE", "")
    if db:
        return db
    try:
        secret_name = os.environ.get("SNOWFLAKE_SECRET_NAME", "illuminate/dev/snowflake")
        sm = boto3.client("secretsmanager", region_name=AWS_REGION)
        creds = json.loads(sm.get_secret_value(SecretId=secret_name)["SecretString"])
        return creds.get("database", "ILLUMINATE")
    except Exception as exc:
        logger.warning("Could not resolve database name from Secrets Manager: %s", exc)
        return "ILLUMINATE"


_database = _resolve_database()
SYSTEM_PROMPT = build_system_prompt(default_catalog(), _database)
_tools = ChatTools(default_catalog(), _database)

_MAX_ROUNDS = 6
_INFERENCE_CONFIG = {"temperature": 0.0, "maxTokens": 4096}
_STATUS = {
    "search_catalog": "Looking up governed metrics...",
    "query_semantic": "Running a governed query...",
    "describe_cdm_table": "Reading the data dictionary...",
    "execute_sql": "Running an ungoverned query...",
}


def _converse(messages: list, system_prompt: Optional[str] = None) -> dict:
    # cachePoints after the system prompt and tools let Bedrock reuse them across rounds and requests.
    return _bedrock.converse(
        modelId=MODEL_ID,
        system=[{"text": system_prompt or SYSTEM_PROMPT}, {"cachePoint": {"type": "default"}}],
        messages=messages,
        toolConfig={"tools": [{"toolSpec": t} for t in _tools.specs] + [{"cachePoint": {"type": "default"}}]},
        inferenceConfig=_INFERENCE_CONFIG,
    )


def _text_of(message: dict) -> str:
    return "\n".join(b["text"] for b in message.get("content", []) if isinstance(b, dict) and "text" in b)


def _tool_uses(message: dict) -> list[dict]:
    return [b["toolUse"] for b in message.get("content", []) if "toolUse" in b]


def _answer(response: dict, artifacts: list) -> str:
    # Claude can end a turn after tool results with no text; blank text also breaks the next Converse call.
    stop = response.get("stopReason")
    if stop not in (None, "end_turn", "tool_use"):
        logger.warning("Converse stopped with %s", stop)
    text = _text_of(response["output"]["message"]).strip()
    if stop == "max_tokens":
        return (text + "\n\n" if text else "") + "_(This answer was cut off; ask me to continue.)_"
    if text:
        return text
    return "Here are the results." if artifacts else "I couldn't find an answer to that."


def _run_tool(tool_use: dict, artifacts: list, called: list, tools: ChatTools) -> dict:
    result = tools.dispatch(tool_use["name"], tool_use.get("input", {}), called)
    called.append(tool_use["name"])
    if "error" in result.content:
        logger.warning("%s error: %s", tool_use["name"], result.content["error"])
    artifacts.extend(result.artifacts)
    return {"toolResult": {
        "toolUseId": tool_use["toolUseId"],
        "content": [{"text": json.dumps(result.content, default=str)}],
        **({"status": "error"} if "error" in result.content else {}),
    }}


def send_message(user_message: str, history: list, tools: Optional[ChatTools] = None,
                 system_prompt: Optional[str] = None) -> tuple[str, list, list]:
    """Returns (response_text, updated_messages, artifacts). tools, system_prompt: e.g. for a tenant's overlaid catalog."""
    tools = tools or _tools
    messages = list(history) + [{"role": "user", "content": [{"text": user_message}]}]
    artifacts: list = []
    called: list = []
    for _ in range(_MAX_ROUNDS):
        response = _converse(messages, system_prompt)
        output = response["output"]["message"]
        messages.append(output)
        uses = _tool_uses(output)
        if not uses:
            return _answer(response, artifacts), messages, artifacts
        messages.append({"role": "user", "content": [_run_tool(u, artifacts, called, tools) for u in uses]})
    return "I was unable to complete the request.", messages, artifacts


async def send_message_streaming(user_message: str, history: list, tools: Optional[ChatTools] = None,
                                 system_prompt: Optional[str] = None):
    """Yields {"type": "status", "message"} events, then {"type": "raw_complete", "text", "messages", "artifacts"}."""
    tools = tools or _tools
    loop = asyncio.get_running_loop()
    messages = list(history) + [{"role": "user", "content": [{"text": user_message}]}]
    artifacts: list = []
    called: list = []
    for _ in range(_MAX_ROUNDS):
        response = await loop.run_in_executor(None, _converse, messages, system_prompt)
        output = response["output"]["message"]
        messages.append(output)
        uses = _tool_uses(output)
        if not uses:
            yield {"type": "raw_complete", "text": _answer(response, artifacts), "messages": messages, "artifacts": artifacts}
            return
        results = []
        for use in uses:
            yield {"type": "status", "message": _STATUS.get(use["name"], "Working...")}
            results.append(await loop.run_in_executor(None, _run_tool, use, artifacts, called, tools))
        messages.append({"role": "user", "content": results})
    yield {"type": "raw_complete", "text": "I was unable to complete the request.",
           "messages": messages, "artifacts": artifacts}
