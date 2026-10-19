"""Trader sessions (F10, F10b): the real agent graph and the in-process connector with a
scripted model and in-memory stores."""

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from services.api.app import ApiDeps, create_app
from services.api.chat import ChatService, ChatSpec, GatewayStores, inprocess_connector
from services.gateway.approvals import MemoryApprovalStore
from services.gateway.audit import MemoryAuditReader, MemoryAuditSink
from services.gateway.provenance.store import MemoryProvenanceStore
from tests.agent.conftest import ScriptedChatModel, answer, plan, tool_call, understand

REPO_ROOT = Path(__file__).resolve().parents[2]


class Stack:
    """The dashboard API with in-process trader sessions (the production connector)."""

    def __init__(self, scripts: list[ScriptedChatModel], max_sessions: int = 8) -> None:
        self.audit = MemoryAuditSink()
        self.approvals = MemoryApprovalStore(audit=self.audit)
        self.scripts = list(scripts)
        stores = GatewayStores(self.audit, self.approvals, MemoryProvenanceStore())
        self.chat = ChatService(
            connect=inprocess_connector(REPO_ROOT, lambda: stores, 60),
            models=self.models,
            approval_timeout_s=60,
            max_sessions=max_sessions,
            repo_root=REPO_ROOT,
            poll_seconds=0.05,
        )
        deps = ApiDeps(approvals=self.approvals, audit=MemoryAuditReader(self.audit),
                       chat=self.chat, repo_root=REPO_ROOT)  # fmt: skip
        self.app = create_app(deps)

    def models(self) -> tuple[Any, Any]:
        model = self.scripts.pop(0)
        return model, model

    def market(self, session_id: str) -> Any:
        return self.chat.get(session_id).market

    def client(self) -> httpx.AsyncClient:
        transport = httpx.ASGITransport(app=self.app)
        return httpx.AsyncClient(transport=transport, base_url="http://test", timeout=30)


def quote_script() -> ScriptedChatModel:
    return ScriptedChatModel([
        understand("question", "NVDA price"), plan(),
        tool_call("get_quote", {"symbol": "NVDA"}), answer("NVDA is at **$212.74**."),
    ])  # fmt: skip


def order_script(final: str, notional: float = 1500, symbol: str = "NVDA") -> ScriptedChatModel:
    return ScriptedChatModel([
        understand("trade", f"buy ${notional} of {symbol}"), plan(),
        tool_call("place_order", {"symbol": symbol, "side": "buy", "notional": notional}),
        answer(final),
    ])  # fmt: skip


async def start(client: httpx.AsyncClient, user: str = "Asha", **body: Any) -> str:
    response = await client.post("/chat/sessions", json={"user": user, **body})
    assert response.status_code == 201, response.text
    session_id: str = response.json()["session_id"]
    return session_id


async def send(client: httpx.AsyncClient, session_id: str, text: str) -> dict[str, Any]:
    response = await client.post(f"/chat/sessions/{session_id}/messages", json={"text": text})
    assert response.status_code == 200, response.text
    data: dict[str, Any] = response.json()
    return data


def assert_no_json(transcript: list[dict[str, Any]]) -> None:
    """The trader view shows sentences, never raw JSON."""
    for entry in transcript:
        for key, value in entry.items():
            if key in ("args", "approval_id", "expires_at"):  # data for the card, not text
                continue
            text = str(value)
            assert "{" not in text and '":' not in text, (key, text)


async def test_a_question_shows_plain_steps_and_the_answer() -> None:
    stack = Stack([quote_script()])
    async with stack.client() as client:
        session_id = await start(client)
        turn = await send(client, session_id, "What is NVDA at?")
        state = (await client.get(f"/chat/sessions/{session_id}")).json()
        await client.delete(f"/chat/sessions/{session_id}")
    assert turn["answer"] == "NVDA is at **$212.74**."
    assert turn["steps"] == [{"role": "step", "status": "ok", "tool": "get_quote",
                              "text": "Checked NVDA: $212.74"}]  # fmt: skip
    assert [entry["role"] for entry in state["transcript"]] == ["user", "step", "agent"]
    assert state["user"] == "Asha" and state["scenario"] == "desk"
    assert_no_json(state["transcript"])
    assert "get_quote" in [e["tool"] for e in stack.audit.events]  # through the gateway


async def test_blocked_order_is_a_friendly_notice() -> None:
    stack = Stack([order_script("That order was too large.", notional=3000, symbol="TSLA")])
    async with stack.client() as client:
        session_id = await start(client)
        turn = await send(client, session_id, "Buy $3000 of TSLA")
        state = (await client.get(f"/chat/sessions/{session_id}")).json()
        await client.delete(f"/chat/sessions/{session_id}")
    step = turn["steps"][0]
    assert step["status"] == "blocked"
    assert step["text"] == "Blocked: buy $3,000 of TSLA at market"
    assert step["title"] == "Order is over your per-order limit"
    assert step["reason"] == "order size $3,000 is over your per-order limit of $2,000"
    assert_no_json(state["transcript"])


async def test_escalated_order_waits_for_the_human_then_executes_and_fills() -> None:
    stack = Stack([order_script("Bought $1500 of NVDA after approval.")])
    async with stack.client() as client:
        session_id = await start(client)
        first = await send(client, session_id, "Buy $1500 of NVDA")
        pending = first["pending_approval"]
        assert first["answer"] is None and first["steps"] == []  # the card replaces the step
        assert pending["rule_id"] == "approval_notional"
        assert pending["title"] == "Large order: needs your approval"
        assert pending["args"] == {"symbol": "NVDA", "side": "buy", "notional": 1500}
        assert stack.market(session_id).list_orders("all")["orders"] == []

        resume = asyncio.create_task(client.post(f"/chat/sessions/{session_id}/resume"))
        await asyncio.sleep(0.2)
        assert not resume.done()  # waiting on the human
        decided = await client.post(f"/approvals/{pending['approval_id']}/approve",
                                    json={"decided_by": "Asha", "note": "ok"})  # fmt: skip
        assert decided.status_code == 200
        done = (await resume).json()
        state = (await client.get(f"/chat/sessions/{session_id}")).json()
        book = (await client.get(f"/chat/sessions/{session_id}/portfolio")).json()
        await client.delete(f"/chat/sessions/{session_id}")
    assert done["decision"]["text"].startswith("Approved. The order was sent.")
    assert done["answer"] == "Bought $1500 of NVDA after approval."
    assert [e["role"] for e in state["transcript"]] == ["user", "approval", "decision", "agent"]
    assert_no_json(state["transcript"])
    # Filled without any clock button: the session's market moved on after the turn.
    assert book["orders"][0]["status"] == "filled"
    nvda = next(p for p in book["positions"] if p["symbol"] == "NVDA")
    assert nvda["qty"] > 0 and book["cash"] == 8500.0  # started empty with $10,000


async def test_portfolio_has_positions_totals_and_day_change() -> None:
    stack = Stack([order_script("Bought.", notional=500, symbol="AMD")])
    async with stack.client() as client:
        session_id = await start(client)
        await send(client, session_id, "Buy $500 of AMD")
        book = (await client.get(f"/chat/sessions/{session_id}/portfolio")).json()
        await client.delete(f"/chat/sessions/{session_id}")
    assert book["cash"] == 9500.0
    assert [p["symbol"] for p in book["positions"]] == ["AMD"]  # only what was bought
    row = book["positions"][0]
    assert set(row) == {"symbol", "qty", "avg", "ltp", "value", "pnl", "pnl_pct",
                        "day_change_pct"}  # fmt: skip
    assert row["day_change_pct"] is not None
    assert book["total_pnl"] == pytest.approx(row["pnl"])
    assert book["market_open"] is True and book["orders"][0]["status"] == "filled"


async def test_two_traders_run_side_by_side_with_separate_books() -> None:
    stack = Stack([order_script("Bought.", notional=500, symbol="AMD"), quote_script()])
    async with stack.client() as client:
        asha = await start(client, "Asha")
        ben = await start(client, "Ben")
        both = await asyncio.gather(send(client, asha, "Buy $500 of AMD"),
                                    send(client, ben, "What is NVDA at?"))  # fmt: skip
        listed = (await client.get("/chat/sessions")).json()["sessions"]
        asha_book = (await client.get(f"/chat/sessions/{asha}/portfolio")).json()
        ben_book = (await client.get(f"/chat/sessions/{ben}/portfolio")).json()
        await client.delete(f"/chat/sessions/{asha}")
        remaining = (await client.get("/chat/sessions")).json()["sessions"]
        await client.delete(f"/chat/sessions/{ben}")
    assert both[0]["steps"][0]["text"] == "Order sent: buy $500 of AMD at market"
    assert {s["user"] for s in listed} == {"Asha", "Ben"}
    assert "AMD" in {p["symbol"] for p in asha_book["positions"]}
    assert "AMD" not in {p["symbol"] for p in ben_book["positions"]}
    assert [s["user"] for s in remaining] == ["Ben"]  # ending one leaves the other


async def test_bad_requests_are_rejected() -> None:
    stack = Stack([quote_script()], max_sessions=2)
    async with stack.client() as client:
        unknown_scenario = await client.post("/chat/sessions", json={"scenario": "nope"})
        held_out = await client.post("/chat/sessions", json={"scenario": "ben_025"})
        bad_policy = await client.post("/chat/sessions", json={"policy": "../.env"})
        no_name = await client.post("/chat/sessions", json={"user": "   "})
        session_id = await start(client, "Asha")
        same_user = await client.post("/chat/sessions", json={"user": "asha"})
        empty = await client.post(f"/chat/sessions/{session_id}/messages", json={"text": ""})
        no_session = await client.post("/chat/sessions/nope/messages", json={"text": "hi"})
        nothing_pending = await client.post(f"/chat/sessions/{session_id}/resume")
        await client.delete(f"/chat/sessions/{session_id}")
        gone = await client.get(f"/chat/sessions/{session_id}")
    assert unknown_scenario.status_code == 404
    assert held_out.status_code == 404  # held out scenarios are not offered
    assert bad_policy.status_code == 404
    assert no_name.status_code == 400
    assert same_user.status_code == 409  # one session per trader
    assert empty.status_code == 422
    assert no_session.status_code == 404
    assert nothing_pending.status_code == 409
    assert gone.status_code == 404


async def test_session_limit() -> None:
    stack = Stack([quote_script(), quote_script()], max_sessions=1)
    async with stack.client() as client:
        first = await start(client, "Asha")
        too_many = await client.post("/chat/sessions", json={"user": "Ben"})
        await client.delete(f"/chat/sessions/{first}")
    assert too_many.status_code == 429


async def test_direct_mode_skips_the_gateway() -> None:
    stack = Stack([order_script("Bought.")])
    async with stack.client() as client:
        session_id = await start(client, guarded=False)
        turn = await send(client, session_id, "Buy $1500 of NVDA")
        orders = stack.market(session_id).list_orders("all")["orders"]
        await client.delete(f"/chat/sessions/{session_id}")
    assert turn["answer"] == "Bought."
    assert len(orders) == 1  # no guard, no approval
    assert stack.audit.events == []


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


def test_the_desk_scenario_has_no_planted_injection() -> None:
    from services.replay_server.scenario import load_scenario

    desk = load_scenario("desk", REPO_ROOT / "scenarios")
    for news_id in desk.news_fixtures:
        item = json.loads((REPO_ROOT / "fixtures/news" / f"{news_id}.json").read_text())
        assert item["injection"] is False, news_id


# --- Alpaca paper sessions (F11) ---------------------------------------------------------


def paper_service(watchlist: list[str] | None) -> ChatService:
    unused: Any = None
    return ChatService(connect=unused, models=unused, approval_timeout_s=60,
                       repo_root=REPO_ROOT, paper_watchlist=watchlist)  # fmt: skip


async def test_paper_session_is_offered_only_when_paper_mode_is_configured() -> None:
    for watchlist, offered in ((None, False), (["NVDA", "AMD"], True)):
        deps = ApiDeps(approvals=MemoryApprovalStore(audit=MemoryAuditSink()),
                       audit=MemoryAuditReader(MemoryAuditSink()), chat=paper_service(watchlist),
                       repo_root=REPO_ROOT)  # fmt: skip
        transport = httpx.ASGITransport(app=create_app(deps))
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            ids = [s["id"] for s in (await client.get("/scenarios")).json()["scenarios"]]
            if not offered:
                missing = await client.post("/chat/sessions", json={"scenario": "alpaca_paper"})
                assert missing.status_code == 404
        assert ("alpaca_paper" in ids) is offered
        if offered:
            assert ids[0] == "alpaca_paper"


async def test_paper_session_must_go_through_the_gateway() -> None:
    service = paper_service(["NVDA"])
    with pytest.raises(Exception, match="gateway"):
        await service.create("alpaca_paper", guarded=False, policy="default", user="Asha")


async def test_paper_connector_starts_the_gateway_on_the_alpaca_upstream() -> None:
    from services.api.chat import stdio_connector

    spec = ChatSpec("alpaca_paper", True, "default", REPO_ROOT / "policies/default.yaml")
    async with stdio_connector(REPO_ROOT)(spec, "web-alpaca_paper-1", "tok") as connection:
        args = list(connection.target.args)
    assert args[:2] == ["-m", "services.gateway"]
    assert args[args.index("--upstream") + 1] == "alpaca"
    assert "--scenario" not in args and "--dev-tools" not in args
    assert connection.market is None


async def test_a_new_trader_starts_empty_with_the_chosen_cash() -> None:
    stack = Stack([quote_script(), quote_script(), quote_script()])
    async with stack.client() as client:
        default = await start(client, "Asha")
        rich = await start(client, "Ben", starting_cash=250000)
        books = [(await client.get(f"/chat/sessions/{sid}/portfolio")).json()
                 for sid in (default, rich)]  # fmt: skip
        too_much = await client.post("/chat/sessions", json={"user": "Cy", "starting_cash": 2e6})
        too_little = await client.post("/chat/sessions", json={"user": "Di", "starting_cash": 10})
        info = (await client.get(f"/chat/sessions/{rich}")).json()
        for sid in (default, rich):
            await client.delete(f"/chat/sessions/{sid}")
    assert books[0]["cash"] == 10000.0 and books[0]["positions"] == []
    assert books[1]["cash"] == 250000.0 and books[1]["positions"] == []
    assert info["starting_cash"] == 250000.0
    assert too_much.status_code == 422 and too_little.status_code == 422
