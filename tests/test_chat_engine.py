import asyncio
import json
import os

import pytest

os.environ.setdefault("SNOWFLAKE_DATABASE", "TESTDB")

import chat_engine  # noqa: E402
from semantic_layer.chat_tools import ToolResult  # noqa: E402


def _text(text):
    return {"output": {"message": {"role": "assistant", "content": [{"text": text}]}}, "stopReason": "end_turn"}


def _tool(name, tool_input, use_id="t1"):
    return {"output": {"message": {"role": "assistant", "content": [
        {"toolUse": {"toolUseId": use_id, "name": name, "input": tool_input}}]}}, "stopReason": "tool_use"}


class ScriptedBedrock:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)


class RecordingTools:
    specs = [{"name": "query_semantic", "description": "d", "inputSchema": {"json": {"type": "object"}}}]

    def __init__(self):
        self.calls = []

    def dispatch(self, name, tool_input, called=()):
        self.calls.append((name, tool_input))
        self.called = tuple(called)
        return ToolResult({"rows": [{"N": 3}]}, [{"id": "a1", "type": "table", "data": {"columns": ["N"], "rows": [{"N": 3}]}}])


@pytest.fixture
def tools(monkeypatch):
    t = RecordingTools()
    monkeypatch.setattr(chat_engine, "_tools", t)
    return t


def test_tool_results_go_back_to_the_model_and_artifacts_to_the_caller(monkeypatch, tools):
    bedrock = ScriptedBedrock(_tool("query_semantic", {"metrics": ["metric.x.v1"]}), _text("There are 3."))
    monkeypatch.setattr(chat_engine, "_bedrock", bedrock)
    text, messages, artifacts = chat_engine.send_message("how many?", [])
    assert text == "There are 3."
    assert tools.calls == [("query_semantic", {"metrics": ["metric.x.v1"]})]
    # messages: user question, assistant tool call, tool result, final answer
    tool_result = messages[2]["content"][0]["toolResult"]
    assert json.loads(tool_result["content"][0]["text"]) == {"rows": [{"N": 3}]}
    assert [a["id"] for a in artifacts] == ["a1"]


def test_system_prompt_and_tools_are_cached(monkeypatch, tools):
    bedrock = ScriptedBedrock(_text("hi"))
    monkeypatch.setattr(chat_engine, "_bedrock", bedrock)
    chat_engine.send_message("hello", [])
    call = bedrock.calls[0]
    assert call["system"][-1] == {"cachePoint": {"type": "default"}}
    assert call["toolConfig"]["tools"][-1] == {"cachePoint": {"type": "default"}}
    assert call["toolConfig"]["tools"][0]["toolSpec"]["name"] == "query_semantic"


def test_running_out_of_rounds_returns_a_message_and_any_artifacts(monkeypatch, tools):
    bedrock = ScriptedBedrock(*[_tool("query_semantic", {}, f"t{i}") for i in range(chat_engine._MAX_ROUNDS)])
    monkeypatch.setattr(chat_engine, "_bedrock", bedrock)
    text, _, artifacts = chat_engine.send_message("loop", [])
    assert "unable to complete" in text
    assert len(artifacts) == chat_engine._MAX_ROUNDS


def test_streaming_yields_statuses_then_the_answer_with_artifacts(monkeypatch, tools):
    bedrock = ScriptedBedrock(_tool("query_semantic", {}), _text("Done."))
    monkeypatch.setattr(chat_engine, "_bedrock", bedrock)

    async def collect():
        return [e async for e in chat_engine.send_message_streaming("q", [])]

    events = asyncio.run(collect())
    assert [e["type"] for e in events[:-1]] and all(e["type"] == "status" for e in events[:-1])
    assert events[-1]["type"] == "raw_complete"
    assert events[-1]["text"] == "Done." and [a["id"] for a in events[-1]["artifacts"]] == ["a1"]


def test_engine_uses_the_semantic_prompt():
    assert "search_catalog" in chat_engine.SYSTEM_PROMPT
    assert "metric.active_students.v1" in chat_engine.SYSTEM_PROMPT


def test_an_empty_final_reply_becomes_a_short_message(monkeypatch, tools):
    bedrock = ScriptedBedrock(_tool("query_semantic", {}), _text(""))
    monkeypatch.setattr(chat_engine, "_bedrock", bedrock)
    text, _, artifacts = chat_engine.send_message("q", [])
    assert text.strip() and artifacts


def test_streaming_never_completes_with_empty_text(monkeypatch, tools):
    bedrock = ScriptedBedrock(_text("  "))
    monkeypatch.setattr(chat_engine, "_bedrock", bedrock)

    async def collect():
        return [e async for e in chat_engine.send_message_streaming("q", [])]

    assert asyncio.run(collect())[-1]["text"].strip()


def test_tools_learn_which_tools_ran_earlier_in_the_turn(monkeypatch, tools):
    bedrock = ScriptedBedrock(_tool("search_catalog", {}, "t1"), _tool("execute_sql", {}, "t2"), _text("ok"))
    monkeypatch.setattr(chat_engine, "_bedrock", bedrock)
    chat_engine.send_message("q", [])
    assert tools.called == ("search_catalog",)


def test_a_per_request_tool_set_replaces_the_default(monkeypatch, tools):
    mine = RecordingTools()
    monkeypatch.setattr(chat_engine, "_bedrock", ScriptedBedrock(_tool("query_semantic", {}), _text("ok")))
    chat_engine.send_message("q", [], tools=mine)
    assert len(mine.calls) == 1 and tools.calls == []


def _cut_off(text):
    return {"output": {"message": {"role": "assistant", "content": [{"text": text}]}}, "stopReason": "max_tokens"}


def test_an_answer_cut_off_at_the_token_limit_says_so(monkeypatch, tools):
    monkeypatch.setattr(chat_engine, "_bedrock", ScriptedBedrock(_cut_off("The first half")))
    text, _, _ = chat_engine.send_message("q", [])
    assert text.startswith("The first half") and "cut off" in text


def test_failed_tool_results_are_marked_as_errors(monkeypatch, tools):
    monkeypatch.setattr(tools, "dispatch", lambda name, tool_input, called=(): ToolResult({"error": "bad contract"}))
    monkeypatch.setattr(chat_engine, "_bedrock", ScriptedBedrock(_tool("query_semantic", {}), _text("ok")))
    _, messages, _ = chat_engine.send_message("q", [])
    assert messages[2]["content"][0]["toolResult"]["status"] == "error"


def test_a_per_request_system_prompt_replaces_the_default(monkeypatch, tools):
    bedrock = ScriptedBedrock(_text("ok"))
    monkeypatch.setattr(chat_engine, "_bedrock", bedrock)
    chat_engine.send_message("q", [], system_prompt="TENANT PROMPT")
    assert bedrock.calls[0]["system"][0] == {"text": "TENANT PROMPT"}
