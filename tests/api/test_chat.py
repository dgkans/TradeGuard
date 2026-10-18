"""Dashboard chat sessions: the real agent graph and gateway, with a scripted model."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest

from services.api.app import ApiDeps, create_app
from services.api.chat import ChatService, ChatSpec
from services.gateway.approvals import MemoryApprovalStore
from services.gateway.audit import MemoryAuditReader, MemoryAuditSink
from services.gateway.mcp_proxy.proxy import Gateway, Upstream
from services.gateway.policy.model import load_policy
from services.gateway.provenance.store import MemoryProvenanceStore
from services.replay_server.scenario import load_scenario
from services.replay_server.server import build_server
from services.replay_server.session import ReplaySession
from tests.agent.conftest import ScriptedChatModel, answer, plan, tool_call, understand

REPO_ROOT = Path(__file__).resolve().parents[2]


class Stack:
    """An API whose chat sessions run the agent against an in-process gateway."""

    def __init__(self, scripts: list[ScriptedChatModel], max_sessions: int = 5) -> None:
        self.audit = MemoryAuditSink()
        self.approvals = MemoryApprovalStore(audit=self.audit)
        self.scripts = list(scripts)
        self.sessions: list[ReplaySession] = []
        self.chat = ChatService(
            connect=self.connect,
            models=self.models,
            approval_timeout_s=60,
            max_sessions=max_sessions,
            repo_root=REPO_ROOT,
        )
        deps = ApiDeps(
            approvals=self.approvals,
            audit=MemoryAuditReader(self.audit),
            chat=self.chat,
            repo_root=REPO_ROOT,
        )
        self.app = create_app(deps)

    def models(self) -> tuple[Any, Any]:
        model = self.scripts.pop(0)
        return model, model

    @asynccontextmanager
    async def connect(self, spec: ChatSpec, session_id: str, token: str) -> AsyncIterator[Any]:
        replay = ReplaySession(
            load_scenario(spec.scenario, REPO_ROOT / "scenarios"),
            bars_root=REPO_ROOT / "fixtures/bars",
            news_root=REPO_ROOT / "fixtures/news",
        )
        self.sessions.append(replay)
        upstream = build_server(replay, dev_tools=True)
        if not spec.guarded:
            yield upstream
            return
        gateway = Gateway(
            [Upstream("replay", upstream)], audit=self.audit, session_id=session_id,
            policy=load_policy(spec.policy_path), operator_token=token,
            approvals=self.approvals, approval_timeout_s=60,
            provenance=MemoryProvenanceStore(),
        )  # fmt: skip
        async with gateway:
            yield gateway.server

    def client(self) -> httpx.AsyncClient:
        transport = httpx.ASGITransport(app=self.app)
        return httpx.AsyncClient(transport=transport, base_url="http://test", timeout=30)


def quote_script() -> ScriptedChatModel:
    return ScriptedChatModel([
        understand("question", "NVDA price"), plan(),
        tool_call("get_quote", {"symbol": "NVDA"}), answer("NVDA is at the quoted price."),
    ])  # fmt: skip


def order_script(final: str) -> ScriptedChatModel:
    return ScriptedChatModel([
        understand("trade", "buy $1500 of NVDA"), plan(),
        tool_call("place_order", {"symbol": "NVDA", "side": "buy", "notional": 1500}),
        answer(final),
    ])  # fmt: skip


async def start(client: httpx.AsyncClient, **body: Any) -> str:
    response = await client.post("/chat/sessions", json={"scenario": "demo", **body})
    assert response.status_code == 201, response.text
    session_id: str = response.json()["session_id"]
    return session_id


async def test_a_question_runs_tools_and_answers() -> None:
    stack = Stack([quote_script()])
    async with stack.client() as client:
        session_id = await start(client)
        turn = (await client.post(f"/chat/sessions/{session_id}/messages",
                                  json={"text": "What is NVDA at?"})).json()  # fmt: skip
        state = (await client.get(f"/chat/sessions/{session_id}")).json()
        await client.delete(f"/chat/sessions/{session_id}")
    assert turn["answer"] == "NVDA is at the quoted price."
    assert turn["pending_approval"] is None
    assert [c["tool"] for c in turn["tool_calls"]] == ["get_quote"]
    assert turn["tool_calls"][0]["error"] is None
    roles = [entry["role"] for entry in state["transcript"]]
    assert roles == ["user", "tool", "agent"]
    assert state["guarded"] is True
    assert state["policy"] == "default"
    # Guarded sessions go through the gateway: the call is in the audit log.
    assert "get_quote" in [e["tool"] for e in stack.audit.events]


async def test_escalated_order_waits_for_the_human_then_executes() -> None:
    stack = Stack([order_script("Bought $1500 of NVDA after approval.")])
    async with stack.client() as client:
        session_id = await start(client)
        first = (await client.post(f"/chat/sessions/{session_id}/messages",
                                   json={"text": "Buy $1500 of NVDA"})).json()  # fmt: skip
        pending = first["pending_approval"]
        assert first["answer"] is None
        assert pending["rule_id"] == "approval_notional"
        assert pending["args"] == {"symbol": "NVDA", "side": "buy", "notional": 1500}
        assert stack.sessions[0].list_orders()["orders"] == []

        resume = asyncio.create_task(client.post(f"/chat/sessions/{session_id}/resume"))
        await asyncio.sleep(0.2)
        assert not resume.done()  # waiting on the human
        decided = await client.post(f"/approvals/{pending['approval_id']}/approve",
                                    json={"decided_by": "tester", "note": "ok"})  # fmt: skip
        assert decided.status_code == 200
        done = (await resume).json()
        state = (await client.get(f"/chat/sessions/{session_id}")).json()
        await client.delete(f"/chat/sessions/{session_id}")
    assert done["outcome"]["status"] == "executed"
    assert done["answer"] == "Bought $1500 of NVDA after approval."
    assert len(stack.sessions[0].list_orders()["orders"]) == 1
    roles = [entry["role"] for entry in state["transcript"]]
    assert roles == ["user", "tool", "approval", "decision", "agent"]


async def test_direct_mode_skips_the_gateway() -> None:
    stack = Stack([order_script("Bought.")])
    async with stack.client() as client:
        session_id = await start(client, guarded=False)
        turn = (await client.post(f"/chat/sessions/{session_id}/messages",
                                  json={"text": "Buy $1500 of NVDA"})).json()  # fmt: skip
        await client.delete(f"/chat/sessions/{session_id}")
    assert turn["answer"] == "Bought."
    assert len(stack.sessions[0].list_orders()["orders"]) == 1  # no guard, no approval
    assert stack.audit.events == []


async def test_bad_requests_are_rejected() -> None:
    stack = Stack([quote_script()], max_sessions=1)
    async with stack.client() as client:
        unknown_scenario = await client.post("/chat/sessions", json={"scenario": "nope"})
        held_out = await client.post("/chat/sessions", json={"scenario": "ben_025"})
        bad_policy = await client.post(
            "/chat/sessions", json={"scenario": "demo", "policy": "../.env"}
        )
        session_id = await start(client)
        too_many = await client.post("/chat/sessions", json={"scenario": "demo"})
        empty = await client.post(f"/chat/sessions/{session_id}/messages", json={"text": ""})
        no_session = await client.post("/chat/sessions/nope/messages", json={"text": "hi"})
        nothing_pending = await client.post(f"/chat/sessions/{session_id}/resume")
        await client.delete(f"/chat/sessions/{session_id}")
        gone = await client.get(f"/chat/sessions/{session_id}")
    assert unknown_scenario.status_code == 404
    assert held_out.status_code == 404  # held out scenarios are not offered
    assert bad_policy.status_code == 404
    assert too_many.status_code == 429
    assert empty.status_code == 422
    assert no_session.status_code == 404
    assert nothing_pending.status_code == 409
    assert gone.status_code == 404


async def test_one_turn_at_a_time_per_session() -> None:
    gate = asyncio.Event()

    class SlowScript(ScriptedChatModel):
        async def _agenerate(self, *args: Any, **kwargs: Any) -> Any:
            await gate.wait()
            return self._generate(*args, **kwargs)

    slow = SlowScript(responses=[understand("question", "x"), plan(), answer("done")])
    stack = Stack([slow])
    async with stack.client() as client:
        session_id = await start(client)
        first = asyncio.create_task(
            client.post(f"/chat/sessions/{session_id}/messages", json={"text": "one"})
        )
        await asyncio.sleep(0.2)
        second = await client.post(f"/chat/sessions/{session_id}/messages", json={"text": "two"})
        gate.set()
        assert (await first).status_code == 200
        await client.delete(f"/chat/sessions/{session_id}")
    assert second.status_code == 409


@pytest.mark.parametrize("minutes", [0, 1000])
async def test_advance_clock_bounds(minutes: int) -> None:
    stack = Stack([quote_script()])
    async with stack.client() as client:
        session_id = await start(client)
        response = await client.post(f"/chat/sessions/{session_id}/advance",
                                     json={"minutes": minutes})  # fmt: skip
        ok = await client.post(f"/chat/sessions/{session_id}/advance", json={"minutes": 5})
        await client.delete(f"/chat/sessions/{session_id}")
    assert response.status_code == 422
    assert ok.status_code == 200
    assert "now" in ok.json()["clock"]
