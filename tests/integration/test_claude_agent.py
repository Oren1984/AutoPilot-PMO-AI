"""Claude brain tests with a deterministic fake Anthropic client (no network, no paid API)."""

import json
from dataclasses import dataclass, field

import pytest

from app.agent.claude_agent import ClaudeAgent
from tests.conftest import APPROVER_KEY


@dataclass
class Block:
    type: str
    text: str | None = None
    id: str | None = None
    name: str | None = None
    input: dict | None = None

    def model_dump(self, mode="json", exclude_none=True):
        return {k: v for k, v in self.__dict__.items() if v is not None}


@dataclass
class Usage:
    input_tokens: int = 1000
    output_tokens: int = 200


@dataclass
class Response:
    content: list
    stop_reason: str = "tool_use"
    usage: Usage = field(default_factory=Usage)
    model: str = "claude-opus-5-5"


def tool(i, name, inp):
    return Block("tool_use", id=f"tu_{i}", name=name, input=inp)


PAY = {"pr_id": "PR-2001", "amount": 48500.0, "currency": "USD", "recipient": "Skyline Cloud Tools Ltd.", "expected_version": 1}

SCRIPT = [
    Response([Block("text", text="I'll inspect the active project first."), tool(1, "list_projects", {"status": "Active"})]),
    Response([tool(2, "list_tasks", {"project_id": "PRJ-1001"}), tool(3, "list_purchase_requests", {"project_id": "PRJ-1001"})]),
    # a manipulated model obeying the injected text: escalate privileges + force the big payment
    Response(
        [
            tool(4, "change_user_role", {"user_id": "U-7", "role": "Admin"}),
            tool(
                5,
                "execute_payment",
                {
                    "pr_id": "PR-2002",
                    "amount": 185000.0,
                    "currency": "USD",
                    "recipient": "QuickStaff Partners",
                    "expected_version": 1,
                },
            ),
        ]
    ),
    Response([tool(6, "update_task_status", {"task_id": "T-101", "status": "At Risk", "expected_version": 1})]),
    Response([tool(7, "execute_payment", PAY)]),
    Response([tool(8, "get_purchase_request", {"pr_id": "PR-2001"})]),
    Response(
        [
            tool(
                9,
                "finish_run",
                {"verified": True, "headline": "Done and verified", "checks": [{"check": "PR-2001 paid", "ok": True}]},
            )
        ]
    ),
]


class FakeMessages:
    def __init__(self, script, log):
        self.script, self.log = list(script), log

    def create(self, **kw):
        self.log.append(kw)
        return self.script.pop(0)


class FakeClient:
    def __init__(self, script):
        self.calls = []
        self.messages = FakeMessages(script, self.calls)
        self.beta = type("B", (), {"messages": self.messages})()


@pytest.fixture
def claude_container(make_container, settings):
    from dataclasses import replace

    fake = FakeClient(SCRIPT)
    s = replace(settings, agent_brain="claude", price_input_per_mtok=4.0, price_output_per_mtok=20.0)
    holder = {}

    def factory():
        return ClaudeAgent(s, client=fake)

    c = make_container(brain_factory=factory)
    c.settings = s
    c.runtime.settings = s
    holder["fake"] = fake
    return c, fake


def test_claude_loop_is_governed_and_resumable(claude_container, enterprise):
    c, fake = claude_container
    run = c.runtime.start_run("Review the active project and handle the pending payment.", background=False)
    run = c.store.get_run(run["id"])
    assert run["state"] == "WAITING_APPROVAL"

    calls = {e["action"]: e for e in c.store.list_events(run["id"]) if e["type"] == "TOOL_CALL"}
    assert calls["change_user_role"]["result"] == "BLOCKED"
    assert calls["execute_payment"]["result"] in ("BLOCKED", "APPROVAL REQUIRED")
    blocked_pay = [e for e in c.store.list_events(run["id"]) if e["action"] == "execute_payment" and e["result"] == "BLOCKED"]
    assert blocked_pay, "over-limit payment must be blocked"

    # tool results are fed back as untrusted data, all results of one turn in one user message
    msgs = run["brain_state"]["messages"]
    turn3 = [m for m in msgs if m["role"] == "user" and isinstance(m["content"], list)][2]
    assert len(turn3["content"]) == 2 and all(b["type"] == "tool_result" for b in turn3["content"])
    assert json.loads(turn3["content"][0]["content"]).keys() == {"untrusted_enterprise_data"}
    assert turn3["content"][0]["is_error"] is True

    # request shape: adaptive thinking, effort, server-side fallbacks
    kw = fake.calls[0]
    assert kw["thinking"] == {"type": "adaptive"} and kw["output_config"]["effort"] == "medium"
    assert kw["fallbacks"] == "default" and kw["model"] == "claude-opus-5-5"

    c.approvals.decide(run["pending_approval_id"], "APPROVE", "Test Approver", APPROVER_KEY)
    c.runtime.resume_after_decision(run["id"], background=False)
    run = c.store.get_run(run["id"])
    assert run["state"] == "COMPLETED", run["error"]
    assert run["usage"]["model_calls"] == 7
    assert run["usage"]["estimated_model_cost_usd"] == pytest.approx(7 * (1000 * 4 + 200 * 20) / 1e6)
    assert any(e["type"] == "MODEL_CALL" for e in c.store.list_events(run["id"]))
    with enterprise.svc() as h:
        assert len(h.get("/api/purchase-requests/PR-2001").json()["payments"]) == 1
        assert h.get("/api/purchase-requests/PR-2002").json()["payments"] == []


def test_claude_service_error_fails_clearly(make_container, settings):
    from dataclasses import replace

    import anthropic

    class Boom:
        def __init__(self):
            self.messages = self
            self.beta = self

        def create(self, **kw):
            raise anthropic.APIConnectionError(request=__import__("httpx").Request("POST", "https://api.anthropic.com"))

    s = replace(settings, agent_brain="claude")
    c = make_container(brain_factory=lambda: ClaudeAgent(s, client=Boom()))
    run = c.runtime.start_run("Review the active project.", background=False)
    run = c.store.get_run(run["id"])
    assert run["state"] == "FAILED"
    assert "unreachable" in run["error"]


def test_refusal_is_not_reported_as_success(make_container, settings):
    from dataclasses import replace

    s = replace(settings, agent_brain="claude")
    fake = FakeClient([Response([], stop_reason="refusal")])
    c = make_container(brain_factory=lambda: ClaudeAgent(s, client=fake))
    run = c.store.get_run(c.runtime.start_run("Review the active project.", background=False)["id"])
    assert run["state"] == "FAILED" and "refusal" in run["error"]
