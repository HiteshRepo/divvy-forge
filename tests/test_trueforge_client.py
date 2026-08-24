"""Unit tests for TrueForgeClient.

Coverage areas (per task 2.4):
- Successful turn stream (model.message event)
- Tool approval event (tool.approval_required)
- Subagent thread events (thread.created / thread.done)
- HTTP error handling
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from divvy_forge.trueforge_client import (
    Agent,
    ModelMessageEvent,
    Session,
    ThreadCreatedEvent,
    ThreadDoneEvent,
    ToolApprovalRequiredEvent,
    ToolCallEvent,
    ToolResponseEvent,
    TrueForgeClient,
    Turn,
    _parse_sse_event,
)

BASE = "http://localhost:8790"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sse(*events: dict) -> bytes:
    """Encode dicts as SSE ``data:`` frames."""
    return b"".join(f"data: {json.dumps(e)}\n\n".encode() for e in events)


def _sse_headers() -> dict[str, str]:
    return {"content-type": "text/event-stream"}


@pytest.fixture
def client() -> TrueForgeClient:
    return TrueForgeClient(base_url=BASE)


# ---------------------------------------------------------------------------
# _parse_sse_event unit tests
# ---------------------------------------------------------------------------


class TestParseSseEvent:
    def test_model_message(self) -> None:
        event = _parse_sse_event({"type": "model.message", "content": "hello"})
        assert isinstance(event, ModelMessageEvent)
        assert event.content == "hello"

    def test_tool_call(self) -> None:
        event = _parse_sse_event(
            {"type": "tool.call", "toolName": "divvy-reader", "args": {"ticker": "INFY"}, "callId": "c1"}
        )
        assert isinstance(event, ToolCallEvent)
        assert event.tool_name == "divvy-reader"
        assert event.args == {"ticker": "INFY"}
        assert event.call_id == "c1"

    def test_tool_response(self) -> None:
        event = _parse_sse_event(
            {"type": "tool.response", "toolName": "divvy-reader", "result": {"yield": 2.5}, "callId": "c1"}
        )
        assert isinstance(event, ToolResponseEvent)
        assert event.tool_name == "divvy-reader"
        assert event.result == {"yield": 2.5}

    def test_tool_approval_required(self) -> None:
        event = _parse_sse_event(
            {
                "type": "tool.approval_required",
                "toolName": "github-pr-opener",
                "args": {"ticker": "INFY", "date": "2026-08-24"},
            }
        )
        assert isinstance(event, ToolApprovalRequiredEvent)
        assert event.tool_name == "github-pr-opener"
        assert event.args["ticker"] == "INFY"

    def test_thread_created(self) -> None:
        event = _parse_sse_event({"type": "thread.created", "threadId": "t-1"})
        assert isinstance(event, ThreadCreatedEvent)
        assert event.thread_id == "t-1"

    def test_thread_done(self) -> None:
        event = _parse_sse_event(
            {"type": "thread.done", "threadId": "t-1", "summary": "Yield trend stable"}
        )
        assert isinstance(event, ThreadDoneEvent)
        assert event.thread_id == "t-1"
        assert event.summary == "Yield trend stable"

    def test_thread_done_no_summary(self) -> None:
        event = _parse_sse_event({"type": "thread.done", "threadId": "t-2"})
        assert isinstance(event, ThreadDoneEvent)
        assert event.summary is None

    def test_unknown_type_returns_none(self) -> None:
        assert _parse_sse_event({"type": "model.message.delta", "delta": "he"}) is None

    def test_mcp_auth_required_returns_none(self) -> None:
        assert _parse_sse_event({"type": "mcp.auth_required"}) is None

    def test_empty_type_returns_none(self) -> None:
        assert _parse_sse_event({}) is None


# ---------------------------------------------------------------------------
# Agent management
# ---------------------------------------------------------------------------


class TestCreateAgent:
    def test_success(self, client: TrueForgeClient) -> None:
        manifest = {"model": "anthropic/claude-3-5-sonnet", "instructions": "You are helpful."}
        response_body = {"id": "agent-1", "name": "coordinator", "manifest": manifest}

        with respx.mock:
            respx.post(f"{BASE}/api/v1/agents").mock(
                return_value=httpx.Response(201, json=response_body)
            )
            agent = client.create_agent("coordinator", manifest)

        assert isinstance(agent, Agent)
        assert agent.id == "agent-1"
        assert agent.name == "coordinator"
        assert agent.manifest == manifest

    def test_http_error_propagates(self, client: TrueForgeClient) -> None:
        with respx.mock:
            respx.post(f"{BASE}/api/v1/agents").mock(
                return_value=httpx.Response(500, json={"error": "internal"})
            )
            with pytest.raises(httpx.HTTPStatusError):
                client.create_agent("coordinator", {})


class TestGetAgent:
    def test_success(self, client: TrueForgeClient) -> None:
        manifest = {"model": "anthropic/claude-3-5-sonnet"}
        with respx.mock:
            respx.get(f"{BASE}/api/v1/agents/agent-1").mock(
                return_value=httpx.Response(200, json={"id": "agent-1", "name": "coordinator", "manifest": manifest})
            )
            agent = client.get_agent("agent-1")

        assert agent.id == "agent-1"


# ---------------------------------------------------------------------------
# Session management
# ---------------------------------------------------------------------------


class TestCreateSession:
    def test_success(self, client: TrueForgeClient) -> None:
        with respx.mock:
            respx.post(f"{BASE}/api/v1/sessions").mock(
                return_value=httpx.Response(201, json={"id": "sess-1", "agentId": "agent-1"})
            )
            session = client.create_session("agent-1")

        assert isinstance(session, Session)
        assert session.id == "sess-1"
        assert session.agent_id == "agent-1"


# ---------------------------------------------------------------------------
# Turn management
# ---------------------------------------------------------------------------


class TestCreateTurn:
    def test_success(self, client: TrueForgeClient) -> None:
        with respx.mock:
            respx.post(f"{BASE}/api/v1/sessions/sess-1/turns").mock(
                return_value=httpx.Response(
                    201,
                    json={
                        "id": "turn-1",
                        "sessionId": "sess-1",
                        "userMessage": "Review INFY",
                        "status": "running",
                        "assistantMessage": None,
                    },
                )
            )
            turn = client.create_turn("sess-1", "Review INFY")

        assert isinstance(turn, Turn)
        assert turn.id == "turn-1"
        assert turn.status == "running"
        assert turn.assistant_message is None


class TestStreamTurn:
    """Core SSE streaming tests (task 2.4 requirements)."""

    def test_successful_turn_stream_model_message(self, client: TrueForgeClient) -> None:
        """Successful stream yields ModelMessageEvent with correct content."""
        content = _sse({"type": "model.message", "content": "Analysis complete for INFY."})

        with respx.mock:
            respx.get(f"{BASE}/api/v1/sessions/sess-1/turns/turn-1/stream").mock(
                return_value=httpx.Response(200, content=content, headers=_sse_headers())
            )
            events = list(client.stream_turn("sess-1", "turn-1"))

        assert len(events) == 1
        assert isinstance(events[0], ModelMessageEvent)
        assert events[0].content == "Analysis complete for INFY."

    def test_tool_approval_event(self, client: TrueForgeClient) -> None:
        """tool.approval_required event is parsed and yielded correctly."""
        content = _sse(
            {
                "type": "tool.approval_required",
                "toolName": "github-pr-opener",
                "args": {"ticker": "INFY", "date": "2026-08-24"},
            }
        )

        with respx.mock:
            respx.get(f"{BASE}/api/v1/sessions/sess-1/turns/turn-1/stream").mock(
                return_value=httpx.Response(200, content=content, headers=_sse_headers())
            )
            events = list(client.stream_turn("sess-1", "turn-1"))

        assert len(events) == 1
        assert isinstance(events[0], ToolApprovalRequiredEvent)
        assert events[0].tool_name == "github-pr-opener"
        assert events[0].args == {"ticker": "INFY", "date": "2026-08-24"}

    def test_subagent_thread_events(self, client: TrueForgeClient) -> None:
        """thread.created and thread.done are emitted for parallel subagents."""
        content = _sse(
            {"type": "thread.created", "threadId": "fundamentals-thread"},
            {"type": "thread.created", "threadId": "risk-thread"},
            {"type": "thread.done", "threadId": "fundamentals-thread", "summary": "Yield stable."},
            {"type": "thread.done", "threadId": "risk-thread", "summary": "No cut signals."},
            {"type": "model.message", "content": "Merged findings: hold."},
        )

        with respx.mock:
            respx.get(f"{BASE}/api/v1/sessions/sess-1/turns/turn-1/stream").mock(
                return_value=httpx.Response(200, content=content, headers=_sse_headers())
            )
            events = list(client.stream_turn("sess-1", "turn-1"))

        assert len(events) == 5
        assert isinstance(events[0], ThreadCreatedEvent)
        assert events[0].thread_id == "fundamentals-thread"
        assert isinstance(events[1], ThreadCreatedEvent)
        assert events[1].thread_id == "risk-thread"
        assert isinstance(events[2], ThreadDoneEvent)
        assert events[2].summary == "Yield stable."
        assert isinstance(events[3], ThreadDoneEvent)
        assert events[3].summary == "No cut signals."
        assert isinstance(events[4], ModelMessageEvent)

    def test_http_error_handling(self, client: TrueForgeClient) -> None:
        """Non-2xx response raises HTTPStatusError."""
        with respx.mock:
            respx.get(f"{BASE}/api/v1/sessions/sess-1/turns/turn-1/stream").mock(
                return_value=httpx.Response(503, json={"error": "service unavailable"})
            )
            with pytest.raises(httpx.HTTPStatusError) as exc_info:
                list(client.stream_turn("sess-1", "turn-1"))

        assert exc_info.value.response.status_code == 503

    def test_unknown_event_types_are_skipped(self, client: TrueForgeClient) -> None:
        """model.message.delta and other non-actionable events are silently skipped."""
        content = _sse(
            {"type": "model.message.delta", "delta": "Analy"},
            {"type": "model.message.delta", "delta": "sis done."},
            {"type": "model.message", "content": "Analysis done."},
        )

        with respx.mock:
            respx.get(f"{BASE}/api/v1/sessions/sess-1/turns/turn-1/stream").mock(
                return_value=httpx.Response(200, content=content, headers=_sse_headers())
            )
            events = list(client.stream_turn("sess-1", "turn-1"))

        assert len(events) == 1
        assert isinstance(events[0], ModelMessageEvent)

    def test_mixed_event_types(self, client: TrueForgeClient) -> None:
        """Full realistic stream with tool calls, thread events, and message."""
        content = _sse(
            {"type": "tool.call", "toolName": "divvy-reader", "args": {"ticker": "ITC"}, "callId": "c1"},
            {"type": "tool.response", "toolName": "divvy-reader", "result": {"yield_pct": 3.1}, "callId": "c1"},
            {"type": "thread.created", "threadId": "fundamentals"},
            {"type": "thread.done", "threadId": "fundamentals", "summary": "Payout ratio safe."},
            {"type": "model.message", "content": "ITC looks healthy."},
        )

        with respx.mock:
            respx.get(f"{BASE}/api/v1/sessions/sess-2/turns/turn-2/stream").mock(
                return_value=httpx.Response(200, content=content, headers=_sse_headers())
            )
            events = list(client.stream_turn("sess-2", "turn-2"))

        assert len(events) == 5
        assert isinstance(events[0], ToolCallEvent)
        assert isinstance(events[1], ToolResponseEvent)
        assert isinstance(events[2], ThreadCreatedEvent)
        assert isinstance(events[3], ThreadDoneEvent)
        assert isinstance(events[4], ModelMessageEvent)

    def test_empty_stream(self, client: TrueForgeClient) -> None:
        """Empty SSE stream yields no events."""
        with respx.mock:
            respx.get(f"{BASE}/api/v1/sessions/sess-1/turns/turn-1/stream").mock(
                return_value=httpx.Response(200, content=b"", headers=_sse_headers())
            )
            events = list(client.stream_turn("sess-1", "turn-1"))

        assert events == []

    def test_done_sentinel_skipped(self, client: TrueForgeClient) -> None:
        """The ``[DONE]`` SSE sentinel is silently ignored."""
        content = b"data: [DONE]\n\n"

        with respx.mock:
            respx.get(f"{BASE}/api/v1/sessions/sess-1/turns/turn-1/stream").mock(
                return_value=httpx.Response(200, content=content, headers=_sse_headers())
            )
            events = list(client.stream_turn("sess-1", "turn-1"))

        assert events == []


class TestGetTurn:
    def test_success(self, client: TrueForgeClient) -> None:
        with respx.mock:
            respx.get(f"{BASE}/api/v1/sessions/sess-1/turns/turn-1").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "id": "turn-1",
                        "sessionId": "sess-1",
                        "userMessage": "Review INFY",
                        "status": "done",
                        "assistantMessage": "Analysis complete.",
                    },
                )
            )
            turn = client.get_turn("sess-1", "turn-1")

        assert turn.status == "done"
        assert turn.assistant_message == "Analysis complete."

    def test_http_error_propagates(self, client: TrueForgeClient) -> None:
        with respx.mock:
            respx.get(f"{BASE}/api/v1/sessions/sess-1/turns/turn-1").mock(
                return_value=httpx.Response(404)
            )
            with pytest.raises(httpx.HTTPStatusError):
                client.get_turn("sess-1", "turn-1")


class TestCancelTurn:
    def test_success(self, client: TrueForgeClient) -> None:
        with respx.mock:
            respx.delete(f"{BASE}/api/v1/sessions/sess-1/turns/turn-1").mock(
                return_value=httpx.Response(204)
            )
            client.cancel_turn("sess-1", "turn-1")  # no exception → pass

    def test_http_error_propagates(self, client: TrueForgeClient) -> None:
        with respx.mock:
            respx.delete(f"{BASE}/api/v1/sessions/sess-1/turns/turn-1").mock(
                return_value=httpx.Response(404)
            )
            with pytest.raises(httpx.HTTPStatusError):
                client.cancel_turn("sess-1", "turn-1")


# ---------------------------------------------------------------------------
# Deploy-time helpers
# ---------------------------------------------------------------------------


class TestRegisterMcpServer:
    def test_success(self, client: TrueForgeClient) -> None:
        response_body = {"id": "mcp-1", "name": "divvy-reader", "url": "mcp+stdio:///path/to/server.py"}

        with respx.mock:
            respx.post(f"{BASE}/api/v1/settings/mcp-servers").mock(
                return_value=httpx.Response(201, json=response_body)
            )
            result = client.register_mcp_server("divvy-reader", "mcp+stdio:///path/to/server.py")

        assert result["name"] == "divvy-reader"

    def test_409_returns_existing(self, client: TrueForgeClient) -> None:
        existing = {"id": "mcp-1", "name": "divvy-reader", "url": "mcp+stdio:///path/to/server.py"}

        with respx.mock:
            respx.post(f"{BASE}/api/v1/settings/mcp-servers").mock(
                return_value=httpx.Response(409, json=existing)
            )
            result = client.register_mcp_server("divvy-reader", "mcp+stdio:///path/to/server.py")

        assert result["id"] == "mcp-1"

    def test_with_auth(self, client: TrueForgeClient) -> None:
        auth = {"type": "header", "header": {"name": "X-Token", "value": "secret"}}
        response_body = {"id": "mcp-2", "name": "market-fetcher", "url": "http://mcp.local", "auth": auth}

        with respx.mock:
            mock = respx.post(f"{BASE}/api/v1/settings/mcp-servers").mock(
                return_value=httpx.Response(201, json=response_body)
            )
            client.register_mcp_server("market-fetcher", "http://mcp.local", auth=auth)

        assert mock.called
        sent = json.loads(mock.calls[0].request.content)
        assert sent["auth"] == auth

    def test_non_409_error_propagates(self, client: TrueForgeClient) -> None:
        with respx.mock:
            respx.post(f"{BASE}/api/v1/settings/mcp-servers").mock(
                return_value=httpx.Response(500)
            )
            with pytest.raises(httpx.HTTPStatusError):
                client.register_mcp_server("divvy-reader", "mcp+stdio:///path")


class TestRegisterAgent:
    def test_creates_new_agent(self, client: TrueForgeClient) -> None:
        manifest = {"model": "anthropic/claude-3-5-sonnet"}
        response_body = {"id": "agent-1", "name": "coordinator", "manifest": manifest}

        with respx.mock:
            respx.post(f"{BASE}/api/v1/agents").mock(
                return_value=httpx.Response(201, json=response_body)
            )
            agent = client.register_agent("coordinator", manifest)

        assert agent.id == "agent-1"

    def test_409_returns_existing_agent(self, client: TrueForgeClient) -> None:
        manifest = {"model": "anthropic/claude-3-5-sonnet"}
        existing = {"id": "agent-42", "name": "coordinator", "manifest": manifest}

        with respx.mock:
            respx.post(f"{BASE}/api/v1/agents").mock(
                return_value=httpx.Response(409, json=existing)
            )
            agent = client.register_agent("coordinator", manifest)

        assert agent.id == "agent-42"

    def test_non_409_error_propagates(self, client: TrueForgeClient) -> None:
        with respx.mock:
            respx.post(f"{BASE}/api/v1/agents").mock(
                return_value=httpx.Response(503)
            )
            with pytest.raises(httpx.HTTPStatusError):
                client.register_agent("coordinator", {})
