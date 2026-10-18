import asyncio
import json

import pytest

import chat_engine
from semantic_layer.chat_tools import ToolResult

PROMPT = "TENANT PROMPT"


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
def tools():
    return RecordingTools()


def test_tool_results_go_back_to_the_model_and_artifacts_to_the_caller(monkeypatch, tools):
    bedrock = ScriptedBedrock(_tool("query_semantic", {"metrics": ["metric.x.v1"]}), _text("There are 3."))
    monkeypatch.setattr(chat_engine, "_bedrock", bedrock)
    text, messages, artifacts = chat_engine.send_message("how many?", [], tools=tools, system_prompt=PROMPT)
    assert text == "There are 3."
    assert tools.calls == [("query_semantic", {"metrics": ["metric.x.v1"]})]
    # messages: user question, assistant tool call, tool result, final answer
    tool_result = messages[2]["content"][0]["toolResult"]
    assert json.loads(tool_result["content"][0]["text"]) == {"rows": [{"N": 3}]}
    assert [a["id"] for a in artifacts] == ["a1"]


def test_system_prompt_and_tools_are_cached(monkeypatch, tools):
    bedrock = ScriptedBedrock(_text("hi"))
    monkeypatch.setattr(chat_engine, "_bedrock", bedrock)
    chat_engine.send_message("hello", [], tools=tools, system_prompt=PROMPT)
    call = bedrock.calls[0]
    assert call["system"][-1] == {"cachePoint": {"type": "default"}}
    assert call["toolConfig"]["tools"][-1] == {"cachePoint": {"type": "default"}}
    assert call["toolConfig"]["tools"][0]["toolSpec"]["name"] == "query_semantic"


def test_running_out_of_rounds_returns_a_message_and_any_artifacts(monkeypatch, tools):
    bedrock = ScriptedBedrock(*[_tool("query_semantic", {}, f"t{i}") for i in range(chat_engine._MAX_ROUNDS)])
    monkeypatch.setattr(chat_engine, "_bedrock", bedrock)
    text, _, artifacts = chat_engine.send_message("loop", [], tools=tools, system_prompt=PROMPT)
    assert "unable to complete" in text
    assert len(artifacts) == chat_engine._MAX_ROUNDS


def test_streaming_yields_statuses_then_the_answer_with_artifacts(monkeypatch, tools):
    bedrock = ScriptedBedrock(_tool("query_semantic", {}), _text("Done."))
    monkeypatch.setattr(chat_engine, "_bedrock", bedrock)

    async def collect():
        return [e async for e in chat_engine.send_message_streaming("q", [], tools=tools, system_prompt=PROMPT)]

    events = asyncio.run(collect())
    assert [e["type"] for e in events[:-1]] and all(e["type"] == "status" for e in events[:-1])
    assert events[-1]["type"] == "raw_complete"
    assert events[-1]["text"] == "Done." and [a["id"] for a in events[-1]["artifacts"]] == ["a1"]


def test_an_empty_final_reply_becomes_a_short_message(monkeypatch, tools):
    bedrock = ScriptedBedrock(_tool("query_semantic", {}), _text(""))
    monkeypatch.setattr(chat_engine, "_bedrock", bedrock)
    text, _, artifacts = chat_engine.send_message("q", [], tools=tools, system_prompt=PROMPT)
    assert text.strip() and artifacts


def test_streaming_never_completes_with_empty_text(monkeypatch, tools):
    bedrock = ScriptedBedrock(_text("  "))
    monkeypatch.setattr(chat_engine, "_bedrock", bedrock)

    async def collect():
        return [e async for e in chat_engine.send_message_streaming("q", [], tools=tools, system_prompt=PROMPT)]

    assert asyncio.run(collect())[-1]["text"].strip()


def test_tools_learn_which_tools_ran_earlier_in_the_turn(monkeypatch, tools):
    bedrock = ScriptedBedrock(_tool("search_catalog", {}, "t1"), _tool("execute_sql", {}, "t2"), _text("ok"))
    monkeypatch.setattr(chat_engine, "_bedrock", bedrock)
    chat_engine.send_message("q", [], tools=tools, system_prompt=PROMPT)
    assert tools.called == ("search_catalog",)


def _cut_off(text):
    return {"output": {"message": {"role": "assistant", "content": [{"text": text}]}}, "stopReason": "max_tokens"}


def test_an_answer_cut_off_at_the_token_limit_says_so(monkeypatch, tools):
    monkeypatch.setattr(chat_engine, "_bedrock", ScriptedBedrock(_cut_off("The first half")))
    text, _, _ = chat_engine.send_message("q", [], tools=tools, system_prompt=PROMPT)
    assert text.startswith("The first half") and "cut off" in text


def test_failed_tool_results_are_marked_as_errors(monkeypatch, tools):
    monkeypatch.setattr(tools, "dispatch", lambda name, tool_input, called=(): ToolResult({"error": "bad contract"}))
    monkeypatch.setattr(chat_engine, "_bedrock", ScriptedBedrock(_tool("query_semantic", {}), _text("ok")))
    _, messages, _ = chat_engine.send_message("q", [], tools=tools, system_prompt=PROMPT)
    assert messages[2]["content"][0]["toolResult"]["status"] == "error"


def test_the_callers_system_prompt_is_sent(monkeypatch, tools):
    bedrock = ScriptedBedrock(_text("ok"))
    monkeypatch.setattr(chat_engine, "_bedrock", bedrock)
    chat_engine.send_message("q", [], tools=tools, system_prompt=PROMPT)
    assert bedrock.calls[0]["system"][0] == {"text": PROMPT}


def test_a_partly_governed_turn_is_replayed_so_a_follow_up_can_resend_its_parts(monkeypatch):
    import lambda_handler
    from semantic_layer.catalog import load_catalog
    from semantic_layer.chat_tools import ChatTools
    from tests.tenant_fakes import TENANT_A
    from tests.test_chat_tools import FakeWarehouse

    parts = {"governed_1": {"metrics": ["metric.reportable_courses.v1"], "dimensions": ["term_name"]}}
    sql = ("SELECT g.term_name, g.reportable_courses, c.N FROM governed_1 g"
           " CROSS JOIN (SELECT COUNT(*) AS N FROM DB.CDM_LMS.COURSE) c")
    request = {"sql": sql, "reason": "No governed metric counts every course row", "governed": parts}
    wh = FakeWarehouse([{"TERM_NAME": "Fall", "REPORTABLE_COURSES": 3, "N": 7}])
    tools = ChatTools(load_catalog(), tenant=TENANT_A, execute=wh)

    bedrock = ScriptedBedrock(_tool("search_catalog", {"question": "courses"}, "t1"),
                              _tool("execute_sql", request, "t2"), _text("Governed: term_name, reportable_courses."))
    monkeypatch.setattr(chat_engine, "_bedrock", bedrock)
    text, messages, artifacts = chat_engine.send_message("courses vs all", [], tools=tools, system_prompt=PROMPT)
    result = json.loads(messages[4]["content"][0]["toolResult"]["content"][0]["text"])
    assert result["provenance"]["governance"] == "partial"
    assert result["provenance"]["columns"]["N"]["governed"] is False

    queries = lambda_handler._queries_from(artifacts)
    assert queries == [{"kind": "sql", "title": "Partly governed query result", "governed": False, "governance": "partial",
                        "sql": sql, "governed_parts": parts}]

    history = [{"role": "user", "content": "courses vs all"}, {"role": "assistant", "content": text, "queries": queries}]
    turns, follow_up = lambda_handler._model_turns(history, "only Fall")
    replayed = json.loads(follow_up.split("<previous_queries>")[1].split("</previous_queries>")[0])
    assert replayed[0]["governed_parts"] == parts

    again = {**request, "sql": sql + " WHERE g.term_name = 'Fall'", "governed": replayed[0]["governed_parts"]}
    bedrock = ScriptedBedrock(_tool("search_catalog", {"question": "fall"}, "t1"), _tool("execute_sql", again, "t2"),
                              _text("Fall only."))
    monkeypatch.setattr(chat_engine, "_bedrock", bedrock)
    _, _, artifacts = chat_engine.send_message(follow_up, turns, tools=tools, system_prompt=PROMPT)
    assert len(wh.sql) == 2 and "governed_1 AS (" in wh.sql[1] and "'Fall'" in wh.sql[1]
    assert artifacts[0]["governed_parts"] == parts
