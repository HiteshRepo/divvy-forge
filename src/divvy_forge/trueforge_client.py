"""TrueForge REST API client for divvy-forge.

Thin httpx wrapper over the TrueForge HTTP API. Covers all operations
needed by divvy-forge: agent and session lifecycle, turn streaming (SSE),
and deploy-time registration helpers.

API base: ``{TRUEFORGE_BASE_URL}/api/v1``

Key endpoints used::

    POST   /api/v1/agents
    GET    /api/v1/agents/{agentId}
    POST   /api/v1/sessions
    POST   /api/v1/sessions/{sessionId}/turns
    GET    /api/v1/sessions/{sessionId}/turns/{turnId}
    DELETE /api/v1/sessions/{sessionId}/turns/{turnId}
    GET    /api/v1/sessions/{sessionId}/turns/{turnId}/stream   (SSE)
    POST   /api/v1/settings/mcp-servers
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import httpx


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Agent:
    """A registered TrueForge agent."""

    id: str
    name: str
    manifest: dict[str, Any]


@dataclass(frozen=True)
class Session:
    """A conversation session bound to an agent."""

    id: str
    agent_id: str


@dataclass(frozen=True)
class Turn:
    """One request/response cycle inside a session."""

    id: str
    session_id: str
    user_message: str
    status: str  # "running" | "done" | "paused"
    assistant_message: str | None = None


# ---------------------------------------------------------------------------
# SSE event models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelMessageEvent:
    """Full assistant message emitted at the end of a model response."""

    content: str


@dataclass(frozen=True)
class ToolCallEvent:
    """Agent has invoked an MCP tool."""

    tool_name: str
    args: dict[str, Any]
    call_id: str | None = None


@dataclass(frozen=True)
class ToolResponseEvent:
    """MCP tool has returned its result."""

    tool_name: str
    result: Any
    call_id: str | None = None


@dataclass(frozen=True)
class ToolApprovalRequiredEvent:
    """Turn is paused; a human must approve or reject this tool call."""

    tool_name: str
    args: dict[str, Any]


@dataclass(frozen=True)
class ThreadCreatedEvent:
    """A subagent thread has been spawned."""

    thread_id: str


@dataclass(frozen=True)
class ThreadDoneEvent:
    """A subagent thread has finished and returned its summary."""

    thread_id: str
    summary: str | None = None


#: Union of all typed SSE event objects yielded by :meth:`TrueForgeClient.stream_turn`.
SSEEvent = (
    ModelMessageEvent
    | ToolCallEvent
    | ToolResponseEvent
    | ToolApprovalRequiredEvent
    | ThreadCreatedEvent
    | ThreadDoneEvent
)


# ---------------------------------------------------------------------------
# SSE parsing
# ---------------------------------------------------------------------------


def _parse_sse_event(data: dict[str, Any]) -> SSEEvent | None:
    """Convert a decoded SSE JSON payload into a typed event object.

    Returns ``None`` for non-actionable or unknown event types (e.g.
    ``model.message.delta``, ``mcp.auth_required``, ``tool.response_required``).
    These are silently skipped by :meth:`TrueForgeClient.stream_turn`.
    """
    event_type = data.get("type", "")

    if event_type == "model.message":
        return ModelMessageEvent(content=data.get("content", ""))

    if event_type == "tool.call":
        return ToolCallEvent(
            tool_name=data.get("toolName", ""),
            args=data.get("args", {}),
            call_id=data.get("callId"),
        )

    if event_type == "tool.response":
        return ToolResponseEvent(
            tool_name=data.get("toolName", ""),
            result=data.get("result"),
            call_id=data.get("callId"),
        )

    if event_type == "tool.approval_required":
        return ToolApprovalRequiredEvent(
            tool_name=data.get("toolName", ""),
            args=data.get("args", {}),
        )

    if event_type == "thread.created":
        return ThreadCreatedEvent(thread_id=data.get("threadId", ""))

    if event_type == "thread.done":
        return ThreadDoneEvent(
            thread_id=data.get("threadId", ""),
            summary=data.get("summary"),
        )

    return None


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class TrueForgeClient:
    """HTTP client for the TrueForge REST API.

    Example::

        client = TrueForgeClient(base_url="http://localhost:8790")
        agent = client.create_agent("coordinator", manifest)
        session = client.create_session(agent.id)
        turn = client.create_turn(session.id, "Review INFY")
        for event in client.stream_turn(session.id, turn.id):
            if isinstance(event, ModelMessageEvent):
                print(event.content)
    """

    def __init__(
        self,
        base_url: str,
        api_key: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        headers: dict[str, str] = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers=headers,
            timeout=httpx.Timeout(timeout),
        )

    # ------------------------------------------------------------------
    # Low-level helpers
    # ------------------------------------------------------------------

    def _get(self, path: str) -> Any:
        response = self._http.get(path)
        response.raise_for_status()
        return response.json()

    def _post(self, path: str, body: dict[str, Any]) -> Any:
        response = self._http.post(path, json=body)
        response.raise_for_status()
        return response.json()

    def _delete(self, path: str) -> None:
        response = self._http.delete(path)
        response.raise_for_status()

    # ------------------------------------------------------------------
    # Agent management
    # ------------------------------------------------------------------

    def create_agent(self, name: str, manifest: dict[str, Any]) -> Agent:
        """Create a new agent with the given manifest.

        Args:
            name:     Unique agent name.
            manifest: Agent manifest dict (model, instructions, mcp_servers, config).

        Returns:
            The created :class:`Agent`.

        Raises:
            httpx.HTTPStatusError: On API error (including 409 name collision).
        """
        data = self._post("/api/v1/agents", {"name": name, "manifest": manifest})
        return Agent(id=data["id"], name=data["name"], manifest=data["manifest"])

    def get_agent(self, agent_id: str) -> Agent:
        """Fetch an existing agent by ID."""
        data = self._get(f"/api/v1/agents/{agent_id}")
        return Agent(id=data["id"], name=data["name"], manifest=data["manifest"])

    # ------------------------------------------------------------------
    # Session management
    # ------------------------------------------------------------------

    def create_session(self, agent_id: str) -> Session:
        """Create a new conversation session for the given agent."""
        data = self._post("/api/v1/sessions", {"agentId": agent_id})
        return Session(id=data["id"], agent_id=data["agentId"])

    # ------------------------------------------------------------------
    # Turn management
    # ------------------------------------------------------------------

    def create_turn(self, session_id: str, user_message: str) -> Turn:
        """Submit a user message and start a new turn.

        The turn begins in ``"running"`` status. Use :meth:`stream_turn` to
        consume its SSE event stream, or :meth:`get_turn` to poll for status.
        """
        data = self._post(
            f"/api/v1/sessions/{session_id}/turns",
            {"userMessage": user_message},
        )
        return Turn(
            id=data["id"],
            session_id=data["sessionId"],
            user_message=data["userMessage"],
            status=data["status"],
            assistant_message=data.get("assistantMessage"),
        )

    def stream_turn(self, session_id: str, turn_id: str) -> Iterator[SSEEvent]:
        """Stream SSE events for a running turn.

        Yields typed :data:`SSEEvent` objects as they arrive. Unknown or
        informational event types (e.g. ``model.message.delta``) are silently
        skipped. The stream closes when the turn completes (``thread.done``
        or server closes the connection).

        Args:
            session_id: Session that owns the turn.
            turn_id:    Turn whose event stream to consume.

        Yields:
            One typed event object per actionable SSE frame.

        Raises:
            httpx.HTTPStatusError: If the server returns a non-2xx status.
        """
        path = f"/api/v1/sessions/{session_id}/turns/{turn_id}/stream"
        with self._http.stream("GET", path) as response:
            response.raise_for_status()
            for line in response.iter_lines():
                if not line.startswith("data:"):
                    continue
                raw = line[5:].strip()
                if not raw or raw == "[DONE]":
                    continue
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                event = _parse_sse_event(payload)
                if event is not None:
                    yield event

    def get_turn(self, session_id: str, turn_id: str) -> Turn:
        """Fetch the current state of a turn (for polling instead of streaming)."""
        data = self._get(f"/api/v1/sessions/{session_id}/turns/{turn_id}")
        return Turn(
            id=data["id"],
            session_id=data["sessionId"],
            user_message=data["userMessage"],
            status=data["status"],
            assistant_message=data.get("assistantMessage"),
        )

    def cancel_turn(self, session_id: str, turn_id: str) -> None:
        """Cancel a running turn. No-op if the turn is already done."""
        self._delete(f"/api/v1/sessions/{session_id}/turns/{turn_id}")

    # ------------------------------------------------------------------
    # Deploy-time helpers  (task 2.3)
    # ------------------------------------------------------------------

    def register_mcp_server(
        self,
        name: str,
        url: str,
        auth: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Register an MCP server connector in TrueForge (Settings → Connectors).

        Idempotent: if a connector with this name already exists (HTTP 409)
        the existing entry is returned unchanged.

        Args:
            name: Connector name — must match the name used in agent manifests.
            url:  ``mcp+stdio:///path/to/server.py`` or an HTTP URL.
            auth: Optional auth config, e.g.::

                    {"type": "header", "header": {"name": "X-Token", "value": "secret"}}

        Returns:
            The connector object as returned by the API.

        Raises:
            httpx.HTTPStatusError: On unexpected API errors (non-409).
        """
        body: dict[str, Any] = {"name": name, "url": url}
        if auth is not None:
            body["auth"] = auth
        try:
            return self._post("/api/v1/settings/mcp-servers", body)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 409:
                return exc.response.json()
            raise

    def register_agent(self, name: str, manifest: dict[str, Any]) -> Agent:
        """Create an agent, or return the existing one if a name collision occurs.

        Idempotent: safe to call on every ``deploy.py`` run.

        Args:
            name:     Unique agent name.
            manifest: Agent manifest dict.

        Returns:
            The created or pre-existing :class:`Agent`.

        Raises:
            httpx.HTTPStatusError: On unexpected API errors (non-409).
        """
        try:
            return self.create_agent(name, manifest)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 409:
                data = exc.response.json()
                return Agent(id=data["id"], name=data["name"], manifest=data["manifest"])
            raise
