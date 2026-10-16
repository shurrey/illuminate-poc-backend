"""
chat_engine.py — Bedrock Converse tool loop over the semantic layer.

The caller supplies the tenant's tools and system prompt; the model answers through the
semantic tools (search_catalog, query_semantic) and falls back to labelled freehand SQL.
Tool artifacts are collected for the client alongside the model's text.
"""

import asyncio
import json
import logging
import os

import boto3

from semantic_layer.chat_tools import ChatTools

logger = logging.getLogger("API-PROXY")

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-6")

_bedrock = boto3.client("bedrock-runtime", region_name=AWS_REGION)

_MAX_ROUNDS = 6
_INFERENCE_CONFIG = {"temperature": 0.0, "maxTokens": 4096}
_STATUS = {
    "search_catalog": "Looking up governed metrics...",
    "query_semantic": "Running a governed query...",
    "describe_cdm_table": "Reading the data dictionary...",
    "execute_sql": "Running an ungoverned query...",
}


def _converse(messages: list, tools: ChatTools, system_prompt: str) -> dict:
    # cachePoints after the system prompt and tools let Bedrock reuse them across rounds and requests.
    return _bedrock.converse(
        modelId=MODEL_ID,
        system=[{"text": system_prompt}, {"cachePoint": {"type": "default"}}],
        messages=messages,
        toolConfig={"tools": [{"toolSpec": t} for t in tools.specs] + [{"cachePoint": {"type": "default"}}]},
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


def send_message(user_message: str, history: list, *, tools: ChatTools,
                 system_prompt: str) -> tuple[str, list, list]:
    """Returns (response_text, updated_messages, artifacts)."""
    messages = list(history) + [{"role": "user", "content": [{"text": user_message}]}]
    artifacts: list = []
    called: list = []
    for _ in range(_MAX_ROUNDS):
        response = _converse(messages, tools, system_prompt)
        output = response["output"]["message"]
        messages.append(output)
        uses = _tool_uses(output)
        if not uses:
            return _answer(response, artifacts), messages, artifacts
        messages.append({"role": "user", "content": [_run_tool(u, artifacts, called, tools) for u in uses]})
    return "I was unable to complete the request.", messages, artifacts


async def send_message_streaming(user_message: str, history: list, *, tools: ChatTools, system_prompt: str):
    """Yields {"type": "status", "message"} events, then {"type": "raw_complete", "text", "messages", "artifacts"}."""
    loop = asyncio.get_running_loop()
    messages = list(history) + [{"role": "user", "content": [{"text": user_message}]}]
    artifacts: list = []
    called: list = []
    for _ in range(_MAX_ROUNDS):
        response = await loop.run_in_executor(None, _converse, messages, tools, system_prompt)
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
