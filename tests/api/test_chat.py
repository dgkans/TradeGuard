"""Trader accounts and their sessions (F10 to F10f): the real agent graph and the in-process
connector with a scripted model, in-memory stores, and (where noted) live prices from a fake
Yahoo."""

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from services.api.accounts import MemoryTraderStore, TraderTokens
from services.api.admin import AdminAuth
from services.api.app import Accounts, ApiDeps, create_app
from services.api.chat import ChatService, ChatSpec, GatewayStores, inprocess_connector
from services.api.limits import universe
from services.api.live import LiveFeed
from services.gateway.approvals import MemoryApprovalStore
from services.gateway.audit import MemoryAuditReader, MemoryAuditSink
from services.gateway.provenance.store import MemoryProvenanceStore
from tests.agent.conftest import ScriptedChatModel, answer, plan, tool_call, understand
from tests.api.fake_yahoo import OPEN_MOMENT, Clock, FakeYahoo

REPO_ROOT = Path(__file__).resolve().parents[2]


class Stack:
    """The dashboard API with trader accounts and in-process sessions (production code)."""

    def __init__(
        self,
        scripts: list[ScriptedChatModel],
        max_sessions: int = 8,
        feed: LiveFeed | None = None,
        store: MemoryTraderStore | None = None,
        **service: Any,
    ) -> None:
        self.audit = MemoryAuditSink()
        self.approvals = MemoryApprovalStore(audit=self.audit)
        self.scripts = list(scripts)
        self.store = store or MemoryTraderStore()
        stores = GatewayStores(self.audit, self.approvals, MemoryProvenanceStore())
        self.chat = ChatService(
            connect=inprocess_connector(REPO_ROOT, lambda: stores, 60, feed),
            models=self.models,
            approval_timeout_s=60,
            max_sessions=max_sessions,
            repo_root=REPO_ROOT,
            poll_seconds=0.05,
            feed=feed,
            store=self.store,
            **service,
        )
        self.admin = AdminAuth("test-admin")
        deps = ApiDeps(approvals=self.approvals, audit=MemoryAuditReader(self.audit),
                       chat=self.chat, repo_root=REPO_ROOT, admin=self.admin,
                       accounts=Accounts(self.store, TraderTokens("k" * 32)))  # fmt: skip
        self.app = create_app(deps)

    def models(self) -> tuple[Any, Any]:
        model = self.scripts.pop(0)
        return model, model

    @asynccontextmanager
    async def client(self) -> AsyncIterator[httpx.AsyncClient]:
        """A client; on exit every trader session is closed, like the API's shutdown."""
        transport = httpx.ASGITransport(app=self.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test",
                                     timeout=30) as client:  # fmt: skip
            try:
                yield client
            finally:
                await self.chat.close_all()

    def admin_headers(self) -> dict[str, str]:
        return {"authorization": f"Bearer {self.admin.login('test-admin').token}"}

    def market_of(self, user: str) -> Any:
        for session in self.chat.sessions.values():
            if session.spec.user.lower() == user.lower():
                return session.market
        raise KeyError(user)


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


async def signup(client: httpx.AsyncClient, user: str = "asha", **body: Any) -> dict[str, str]:
    payload = {"username": user, "password": "pw-" + user + "-123", **body}
    response = await client.post("/auth/signup", json=payload)
    assert response.status_code == 201, response.text
    return {"x-trader-token": response.json()["token"]}


async def send(client: httpx.AsyncClient, who: dict[str, str], text: str) -> dict[str, Any]:
    response = await client.post("/me/messages", json={"text": text}, headers=who)
    assert response.status_code == 200, response.text
    data: dict[str, Any] = response.json()
    return data


async def get(client: httpx.AsyncClient, who: dict[str, str], path: str) -> Any:
    response = await client.get(path, headers=who)
    assert response.status_code == 200, response.text
    return response.json()


def assert_no_json(transcript: list[dict[str, Any]]) -> None:
    """The trader view shows sentences, never raw JSON."""
    for entry in transcript:
        for key, value in entry.items():
            if key in ("args", "approval_id", "expires_at"):  # data for the card, not text
                continue
            text = str(value)
            assert "{" not in text and '":' not in text, (key, text)


# --- the trader's own session ------------------------------------------------------------


async def test_a_question_shows_plain_steps_and_the_answer() -> None:
    stack = Stack([quote_script()])
    async with stack.client() as client:
        asha = await signup(client, "asha", display_name="Asha")
        turn = await send(client, asha, "What is NVDA at?")
        state = await get(client, asha, "/me/session")
    assert turn["answer"] == "NVDA is at **$212.74**."
    assert turn["steps"][0]["status"] == "ok"
    assert turn["steps"][0]["text"].startswith("Checked NVDA: $")
    assert [entry["role"] for entry in state["transcript"]] == ["user", "step", "agent"]
    assert state["user"] == "Asha" and state["scenario"] == "desk"
    assert_no_json(state["transcript"])
    assert "get_quote" in [e["tool"] for e in stack.audit.events]  # through the gateway


async def test_blocked_order_is_a_friendly_notice() -> None:
    stack = Stack([order_script("That order was too large.", notional=3000, symbol="TSLA")])
    async with stack.client() as client:
        asha = await signup(client)
        turn = await send(client, asha, "Buy $3000 of TSLA")
        state = await get(client, asha, "/me/session")
    step = turn["steps"][0]
    assert step["status"] == "blocked"
    assert step["text"] == "Blocked: buy $3,000 of TSLA at market"
    assert step["title"] == "Order is over your per-order limit"
    assert step["reason"] == "order size $3,000 is over your per-order limit of $2,000"
    assert_no_json(state["transcript"])


async def test_escalated_order_waits_for_the_trader_then_executes_and_fills() -> None:
    stack = Stack([order_script("Bought $1500 of NVDA after approval.")])
    async with stack.client() as client:
        asha = await signup(client, "asha", display_name="Asha")
        first = await send(client, asha, "Buy $1500 of NVDA")
        pending = first["pending_approval"]
        assert first["answer"] is None and first["steps"] == []
        assert pending["title"] == "Large order: needs your approval"
        assert pending["args"] == {"symbol": "NVDA", "side": "buy", "notional": 1500}
        resume = asyncio.create_task(client.post("/me/resume", headers=asha))
        await asyncio.sleep(0.2)
        assert not resume.done()  # waiting on the trader
        decided = await client.post(f"/me/approvals/{pending['approval_id']}/approve",
                                    json={"note": "ok"}, headers=asha)  # fmt: skip
        assert decided.status_code == 200 and decided.json()["decided_by"] == "Asha"
        done = (await resume).json()
        book = await get(client, asha, "/me/portfolio")
    assert done["decision"]["text"].startswith("Approved. The order was sent.")
    assert book["orders"][0]["status"] == "filled"
    assert book["cash"] == 8500.0  # started empty with $10,000


async def test_a_new_trader_starts_empty_with_the_chosen_cash() -> None:
    stack = Stack([quote_script()])
    async with stack.client() as client:
        rich = await signup(client, "ben", starting_cash=250000)
        book = await get(client, rich, "/me/portfolio")
        too_much = await client.post(
            "/auth/signup",
            json={"username": "cy_1", "password": "longenough", "starting_cash": 2e6},
        )
    assert book["cash"] == 250000.0 and book["positions"] == []
    assert too_much.status_code == 422


async def test_portfolio_has_positions_totals_and_day_change() -> None:
    stack = Stack([order_script("Bought.", notional=500, symbol="AMD")])
    async with stack.client() as client:
        asha = await signup(client)
        await send(client, asha, "Buy $500 of AMD")
        book = await get(client, asha, "/me/portfolio")
    assert book["cash"] == 9500.0
    assert [p["symbol"] for p in book["positions"]] == ["AMD"]
    row = book["positions"][0]
    assert set(row) == {"symbol", "qty", "avg", "ltp", "value", "pnl", "pnl_pct",
                        "day_change_pct"}  # fmt: skip
    assert row["day_change_pct"] is not None
    assert book["total_pnl"] == pytest.approx(row["pnl"])


# --- accounts: sign up, log in, who can see what -----------------------------------------


async def test_sign_up_log_in_and_lockout() -> None:
    stack = Stack([quote_script()])
    async with stack.client() as client:
        await signup(client, "asha")
        taken = await client.post("/auth/signup", json={"username": "ASHA",
                                                         "password": "another-pw"})  # fmt: skip
        short = await client.post("/auth/signup", json={"username": "bo", "password": "x"})
        good = await client.post("/auth/login", json={"username": "Asha",
                                                       "password": "pw-asha-123"})  # fmt: skip
        wrong = [await client.post("/auth/login", json={"username": "asha", "password": "no"})
                 for _ in range(5)]  # fmt: skip
        locked = await client.post("/auth/login", json={"username": "asha",
                                                         "password": "pw-asha-123"})  # fmt: skip
        nobody = await client.get("/me")
        forged = await client.get("/me", headers={"x-trader-token": "1.9999999999999.abc"})
    assert taken.status_code == 409
    assert short.status_code == 422
    assert good.status_code == 200 and good.json()["trader"]["username"] == "asha"
    assert "password" not in json.dumps(good.json()["trader"])
    assert [r.status_code for r in wrong] == [401] * 5
    assert locked.status_code == 429  # even the right password waits out the lock
    assert nobody.status_code == 401 and forged.status_code == 401


async def test_a_trader_can_only_reach_their_own_session() -> None:
    stack = Stack([order_script("Bought after approval."), quote_script()])
    async with stack.client() as client:
        asha = await signup(client, "asha", display_name="Asha")
        ben = await signup(client, "ben")
        first = await send(client, asha, "Buy $1500 of NVDA")
        approval_id = first["pending_approval"]["approval_id"]
        resume = asyncio.create_task(client.post("/me/resume", headers=asha))
        stolen = await client.post(f"/me/approvals/{approval_id}/approve",
                                   json={"note": "mine now"}, headers=ben)  # fmt: skip
        ben_view = await get(client, ben, "/me/session")
        by_id = await client.get("/chat/sessions", headers=ben)  # the admin's view
        own = await client.post(f"/me/approvals/{approval_id}/approve",
                                json={"note": "ok"}, headers=asha)  # fmt: skip
        done = (await resume).json()
        listing = await client.get("/admin/traders", headers=stack.admin_headers())
    assert stolen.status_code == 404
    assert ben_view["transcript"] == [] and ben_view["user"] == "ben"
    assert by_id.status_code == 401
    assert own.status_code == 200
    assert done["decision"]["status"] == "executed"
    traders = listing.json()["traders"]
    assert [t["username"] for t in traders] == ["asha", "ben"]
    assert all("password_hash" not in t for t in traders)


async def test_two_traders_trade_side_by_side_with_separate_books() -> None:
    stack = Stack([order_script("Bought.", notional=500, symbol="AMD"), quote_script()])
    async with stack.client() as client:
        asha = await signup(client, "asha")
        ben = await signup(client, "ben")
        both = await asyncio.gather(send(client, asha, "Buy $500 of AMD"),
                                    send(client, ben, "What is NVDA at?"))  # fmt: skip
        asha_book = await get(client, asha, "/me/portfolio")
        ben_book = await get(client, ben, "/me/portfolio")
    assert both[0]["steps"][0]["text"] == "Order sent: buy $500 of AMD at market"
    assert "AMD" in {p["symbol"] for p in asha_book["positions"]}
    assert ben_book["positions"] == []


async def test_one_turn_at_a_time() -> None:
    gate = asyncio.Event()

    class SlowScript(ScriptedChatModel):
        async def _agenerate(self, *args: Any, **kwargs: Any) -> Any:
            await gate.wait()
            return self._generate(*args, **kwargs)

    slow = SlowScript(responses=[understand("question", "x"), plan(), answer("done")])
    stack = Stack([slow])
    async with stack.client() as client:
        asha = await signup(client)
        first = asyncio.create_task(client.post("/me/messages", json={"text": "one"},
                                                headers=asha))  # fmt: skip
        await asyncio.sleep(0.2)
        second = await client.post("/me/messages", json={"text": "two"}, headers=asha)
        gate.set()
        assert (await first).status_code == 200
    assert second.status_code == 409


# --- live prices (F10f) and coming back after a restart -----------------------------------


async def live_feed(at: datetime = OPEN_MOMENT) -> tuple[LiveFeed, FakeYahoo, Clock]:
    clock = Clock(at)
    yahoo = FakeYahoo(REPO_ROOT, clock)
    feed = LiveFeed(universe(), yahoo, clock=clock)
    await feed.refresh()
    return feed, yahoo, clock


async def test_a_live_market_order_fills_at_the_live_price() -> None:
    feed, yahoo, _clock = await live_feed()
    yahoo.prices["AMD"] = 125.0
    await feed.refresh()
    stack = Stack([order_script("Sent.", notional=500, symbol="AMD")], feed=feed)
    async with stack.client() as client:
        asha = await signup(client)
        await send(client, asha, "Buy $500 of AMD")
        book = await get(client, asha, "/me/portfolio")
    assert book["orders"][0]["status"] == "filled"
    assert book["positions"] == [{**book["positions"][0], "symbol": "AMD", "qty": 4.0,
                                  "avg": 125.0, "ltp": 125.0}]  # fmt: skip
    assert book["cash"] == 9500.0
    assert book["as_of"] == OPEN_MOMENT.isoformat()


async def test_a_limit_order_fills_when_the_live_price_gets_there() -> None:
    feed, yahoo, clock = await live_feed()
    yahoo.prices["NVDA"] = 200.0
    await feed.refresh()
    script = ScriptedChatModel([
        understand("trade", "buy 2 shares of NVDA at a 190 limit"), plan(),
        tool_call("place_order", {"symbol": "NVDA", "side": "buy", "qty": 2,
                                  "order_type": "limit", "limit_price": 190}),
        answer("Sent."),
    ])  # fmt: skip
    stack = Stack([script], feed=feed)
    async with stack.client() as client:
        asha = await signup(client)
        await send(client, asha, "Buy 2 shares of NVDA at a 190 limit")
        before = await get(client, asha, "/me/portfolio")
        yahoo.prices["NVDA"] = 189.0
        clock.move(minutes=1)
        await feed.refresh()
        changed = stack.chat.settle()  # what the API's poller does after each refresh
        after = await get(client, asha, "/me/portfolio")
    assert before["orders"][0]["status"] == "new"
    assert [s.spec.user for s in changed] == ["asha"]
    assert after["orders"][0]["status"] == "filled"
    assert after["positions"][0]["avg"] == 189.0


async def test_an_account_comes_back_identical_after_a_restart() -> None:
    store = MemoryTraderStore()
    feed, _yahoo, clock = await live_feed()
    first = Stack([order_script("Sent.", notional=500, symbol="AMD")], feed=feed, store=store)
    async with first.client() as client:
        asha = await signup(client, "asha", starting_cash=50000)
        limits = (await get(client, asha, "/me/limits"))["limits"]
        changed = await client.put(
            "/me/limits",
            headers=asha,
            json={**limits, "max_order_usd": 900, "approval_over_usd": 800},
        )
        assert changed.status_code == 200
        await send(client, asha, "Buy $500 of AMD")
        book = await get(client, asha, "/me/portfolio")
        transcript = (await get(client, asha, "/me/session"))["transcript"]
    await first.chat.close_all()  # the API stops

    clock.move(minutes=30)  # the market moves while the API is down
    await feed.refresh()
    second = Stack([quote_script()], feed=feed, store=store)  # a new process, same database
    async with second.client() as client:
        login = await client.post("/auth/login", json={"username": "asha",
                                                        "password": "pw-asha-123"})  # fmt: skip
        again = {"x-trader-token": login.json()["token"]}
        back = await get(client, again, "/me/portfolio")
        back_limits = (await get(client, again, "/me/limits"))["limits"]
        back_transcript = (await get(client, again, "/me/session"))["transcript"]
    assert back["orders"] == book["orders"]
    assert back["cash"] == book["cash"] == 49500.0
    assert [(p["symbol"], p["qty"], p["avg"]) for p in back["positions"]] == [
        (p["symbol"], p["qty"], p["avg"]) for p in book["positions"]
    ]
    assert back["as_of"] > book["as_of"]  # priced at the latest live price
    assert back_limits["max_order_usd"] == 900
    assert back_transcript == transcript


async def test_admin_logs_in_on_the_trader_form() -> None:
    stack = Stack([])
    async with stack.client() as client:
        signed = await client.post("/auth/login", json={"username": "admin",
                                                         "password": "test-admin"})  # fmt: skip
        token = signed.json()["token"]
        listing = await client.get("/admin/traders",
                                   headers={"authorization": f"Bearer {token}"})  # fmt: skip
        taken = await client.post("/auth/signup", json={"username": "ADMIN",
                                                         "password": "long-enough-1"})  # fmt: skip
        as_trader = await client.get("/me", headers={"x-trader-token": token})
    assert signed.json()["role"] == "admin" and listing.status_code == 200
    assert taken.status_code == 409  # nobody can register as admin
    assert as_trader.status_code == 401  # an admin token is not a trader login


# --- limits (F10d), through /me ----------------------------------------------------------


async def test_a_trader_edits_limits_and_the_next_order_is_checked_against_them() -> None:
    stack = Stack([order_script("Too big now.", notional=800, symbol="AMD")])
    async with stack.client() as client:
        asha = await signup(client)
        before = await get(client, asha, "/me/limits")
        changed = await client.put(
            "/me/limits",
            headers=asha,
            json={**before["limits"], "max_order_usd": 500, "approval_over_usd": 300},
        )
        turn = await send(client, asha, "Buy $800 of AMD")
        stopped = (await get(client, asha, "/me/stopped"))["rules"]
    assert before["limits"]["max_order_usd"] == 2000
    assert before["locked"] == ["No short selling", "Only while the market is open",
                                "Orders you didn't ask for are blocked",
                                "Each order must match what you asked"]  # fmt: skip
    assert changed.status_code == 200
    step = turn["steps"][0]
    assert step["status"] == "blocked" and "$500" in step["reason"]
    assert stopped == [{"rule_id": "max_order_notional", "decision": "DENY", "count": 1,
                        "title": "Order is over your per-order limit"}]  # fmt: skip
    assert len([e for e in stack.audit.events if e["tool"] == "update_limits"]) == 1


@pytest.mark.parametrize(
    ("change", "field"),
    [
        ({"max_order_usd": 50}, "max_order_usd"),
        ({"max_order_usd": 2_000_000}, "max_order_usd"),
        ({"max_position_usd": 100, "max_order_usd": 500}, "max_position_usd"),
        ({"max_exposure_pct": 150}, "max_exposure_pct"),
        ({"max_orders_per_day": 0}, "max_orders_per_day"),
        ({"approval_over_usd": 5000}, "approval_over_usd"),
        ({"allowed_symbols": []}, "allowed_symbols"),
        ({"allowed_symbols": ["NVDA", "DOGECOIN"]}, "allowed_symbols"),
        ({"allow_short": True}, "allow_short"),
        ({"taint": False}, "taint"),
    ],
)
async def test_limits_are_bounded_and_protections_stay_on(
    change: dict[str, Any], field: str
) -> None:
    stack = Stack([quote_script()])
    async with stack.client() as client:
        asha = await signup(client)
        current = (await get(client, asha, "/me/limits"))["limits"]
        response = await client.put("/me/limits", json={**current, **change}, headers=asha)
    assert response.status_code == 422, response.text
    assert field in response.text


async def test_limits_can_start_from_a_profile_template() -> None:
    stack = Stack([quote_script()])
    async with stack.client() as client:
        asha = await signup(client)
        strict = await get(client, asha, "/me/limits?template=strict")
    assert strict["limits"]["max_order_usd"] == 500
    assert strict["limits"]["allowed_symbols"] == ["SPY", "QQQ", "AAPL", "MSFT"]


# --- admin's session tools, direct mode, scenario hygiene, paper sessions ----------------


async def test_admin_can_run_a_direct_session_without_the_guard() -> None:
    stack = Stack([order_script("Bought.")])
    async with stack.client() as client:
        admin = stack.admin_headers()
        anonymous = await client.post("/chat/sessions", json={"user": "Zed", "guarded": False})
        made = await client.post("/chat/sessions", json={"user": "Zed", "guarded": False},
                                 headers=admin)  # fmt: skip
        sid = made.json()["session_id"]
        turn = await client.post(f"/chat/sessions/{sid}/messages", json={"text": "Buy"},
                                 headers=admin)  # fmt: skip
        orders = stack.market_of("Zed").list_orders("all")["orders"]
    assert anonymous.status_code == 401
    assert turn.json()["answer"] == "Bought."
    assert len(orders) == 1
    assert stack.audit.events == []  # no gateway, no audit


def test_the_desk_scenario_has_no_planted_injection() -> None:
    from services.replay_server.scenario import load_scenario

    desk = load_scenario("desk", REPO_ROOT / "scenarios")
    for news_id in desk.news_fixtures:
        item = json.loads((REPO_ROOT / "fixtures/news" / f"{news_id}.json").read_text())
        assert item["injection"] is False, news_id


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
        assert ("alpaca_paper" in ids) is offered


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
    assert connection.market is None


# --- the free AI budget (F12) -------------------------------------------------------------


async def test_a_trader_gets_a_friendly_message_after_their_daily_ai_turns() -> None:
    stack = Stack([quote_script()], trader_daily_turn_cap=1)
    async with stack.client() as client:
        asha = await signup(client)
        assert (await get(client, asha, "/me"))["session"]["ai_turns_left"] == 1
        await send(client, asha, "What is NVDA at?")
        refused = await client.post("/me/messages", json={"text": "And AMD?"}, headers=asha)
        left = (await get(client, asha, "/me"))["session"]["ai_turns_left"]
    assert refused.status_code == 429
    assert "used your 1 AI requests for today" in refused.json()["detail"]
    assert left == 0


async def test_daily_ai_turns_survive_the_api_sleeping_and_waking() -> None:
    """Render's free plan restarts the API on wake; the counts are in the store (F12)."""
    store = MemoryTraderStore()
    first = Stack([quote_script()], store=store, trader_daily_turn_cap=1)
    async with first.client() as client:
        asha = await signup(client)
        await send(client, asha, "What is NVDA at?")
    woken = Stack([quote_script()], store=store, trader_daily_turn_cap=1)  # a new process
    async with woken.client() as client:
        login = await client.post("/auth/login", json={"username": "asha",
                                                        "password": "pw-asha-123"})  # fmt: skip
        again = {"x-trader-token": login.json()["token"]}
        left = (await get(client, again, "/me"))["session"]["ai_turns_left"]
        refused = await client.post("/me/messages", json={"text": "hi"}, headers=again)
    assert left == 0
    assert refused.status_code == 429


async def test_the_site_wide_daily_cap_stops_every_trader() -> None:
    stack = Stack([quote_script(), quote_script()], daily_turn_cap=1)
    async with stack.client() as client:
        asha = await signup(client, "asha")
        ben = await signup(client, "ben")
        await send(client, asha, "What is NVDA at?")
        refused = await client.post("/me/messages", json={"text": "hi"}, headers=ben)
    assert refused.status_code == 429
    assert "today's free budget" in refused.json()["detail"]


class RateLimitError(Exception):
    """Stands in for groq.RateLimitError."""

    status_code = 429


class LimitedModel(ScriptedChatModel):
    def _generate(self, *args: Any, **kwargs: Any) -> Any:
        raise RateLimitError("rate limit reached for model openai/gpt-oss-120b")


async def test_a_provider_rate_limit_is_a_friendly_429() -> None:
    stack = Stack([LimitedModel(responses=[])])
    async with stack.client() as client:
        asha = await signup(client)
        refused = await client.post("/me/messages", json={"text": "What is NVDA at?"},
                                    headers=asha)  # fmt: skip
        again = await get(client, asha, "/me/session")
    assert refused.status_code == 429
    assert refused.json()["detail"].startswith("The AI is at its free usage limit")
    assert again["busy"] is False  # the session is still usable


class AuthenticationError(Exception):
    """Stands in for groq.AuthenticationError (a bad or revoked key)."""

    status_code = 401


class BrokenKeyModel(ScriptedChatModel):
    def _generate(self, *args: Any, **kwargs: Any) -> Any:
        raise AuthenticationError("Error code: 401 - Invalid API Key")


async def test_a_provider_failure_is_a_friendly_503_and_hides_the_cause() -> None:
    stack = Stack([BrokenKeyModel(responses=[])], trader_daily_turn_cap=5)
    async with stack.client() as client:
        asha = await signup(client)
        failed = await client.post("/me/messages", json={"text": "What is NVDA at?"},
                                   headers=asha)  # fmt: skip
        still = await client.get("/me/session", headers=asha)
    assert still.json()["ai_turns_left"] == 5  # a failed turn isn't charged
    assert failed.status_code == 503
    assert (
        failed.json()["detail"]
        == "The AI service isn't available right now. Please try again later."
    )
    assert "API Key" not in failed.text
    assert still.status_code == 200


async def test_a_replay_account_also_comes_back_after_a_restart() -> None:
    """LIVE_PRICES=false (F12): the sleeping free server restarts on wake; the book stays."""
    store = MemoryTraderStore()
    first = Stack([order_script("Sent.", notional=500, symbol="AMD")], store=store)
    async with first.client() as client:
        asha = await signup(client, "asha", starting_cash=20000)
        await send(client, asha, "Buy $500 of AMD")
        book = await get(client, asha, "/me/portfolio")
    second = Stack([quote_script()], store=store)  # a new process, same database
    async with second.client() as client:
        login = await client.post("/auth/login", json={"username": "asha",
                                                        "password": "pw-asha-123"})  # fmt: skip
        again = {"x-trader-token": login.json()["token"]}
        back = await get(client, again, "/me/portfolio")
        await send(client, again, "What is NVDA at?")
        later = await get(client, again, "/me/portfolio")
    assert book["orders"][0]["status"] == "filled"
    assert back["orders"] == book["orders"]
    assert back["cash"] == book["cash"] == 19500.0
    assert back["positions"] == book["positions"]
    assert back["as_of"] >= book["orders"][0]["submitted_at"]  # clock caught up to the book
    assert later["orders"] == book["orders"]
