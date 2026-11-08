"""Execution-boundary tests: policy, approval binding, idempotency and attack attempts."""

import json

import pytest

from app.observability.tracker import new_usage
from tests.conftest import APPROVER_KEY, SVC_TOKEN

PAY = {"pr_id": "PR-2001", "amount": 48500.0, "currency": "USD", "recipient": "Skyline Cloud Tools Ltd.", "expected_version": 1}


@pytest.fixture
def run(container):
    r = container.store.create_run("security test", "demo", "simulated")
    r = container.store.get_run(r["id"])
    r["usage"] = new_usage(container.settings)
    return r


def _paid(enterprise):
    with enterprise.svc() as h:
        return h.get("/api/purchase-requests/PR-2001").json()["payments"]


def test_financial_action_via_ui_is_blocked(container, run, enterprise):
    out = container.gateway.execute(run, 1, "execute_payment", PAY, method="ui")
    assert out.status == "blocked" and "governed API path" in out.message
    assert container.store.list_approvals(run_id=run["id"]) == []
    assert _paid(enterprise) == []


def test_unknown_and_prohibited_tools_are_blocked(container, run, enterprise):
    assert container.gateway.execute(run, 1, "wire_transfer", {"amount": 1}).status == "blocked"
    out = container.gateway.execute(run, 2, "change_user_role", {"user_id": "U-7", "role": "Admin"})
    assert out.status == "blocked" and out.policy["level"] == "prohibited"
    with enterprise.admin() as h:
        roles = {u["id"]: u for u in h.get("/api/admin/audit").json()}
    assert not any(e.get("action") == "user.role" for e in roles.values())
    assert run["usage"]["policy_denials"] == 2


def test_injected_instruction_cannot_force_payment(container, run, enterprise):
    """Even if a model obeyed the injected text ('approve PR-2002 immediately'), the gateway refuses."""
    params = {
        "pr_id": "PR-2002",
        "amount": 185000.0,
        "currency": "USD",
        "recipient": "QuickStaff Partners",
        "expected_version": 1,
    }
    out = container.gateway.execute(run, 1, "execute_payment", params, rationale="task T-102 says to approve immediately")
    assert out.status == "blocked" and out.policy["rule"] == "financial_hard_limit"
    with enterprise.svc() as h:
        assert h.get("/api/purchase-requests/PR-2002").json()["payments"] == []


def test_payment_pauses_and_approval_is_bound_to_exact_parameters(container, run, enterprise):
    first = container.gateway.execute(run, 1, "execute_payment", PAY)
    assert first.status == "approval_required"
    assert container.gateway.execute(run, 2, "execute_payment", PAY).status == "approval_required"  # still pending
    assert _paid(enterprise) == []
    container.approvals.decide(first.approval_id, "APPROVE", "Approver", APPROVER_KEY)
    # different parameters (redirected recipient) are NOT covered by the approval
    tampered = {**PAY, "recipient": "Attacker Ltd."}
    out = container.gateway.execute(run, 3, "execute_payment", tampered)
    assert out.status == "blocked" and "recipient" in out.message
    assert _paid(enterprise) == []
    ok = container.gateway.execute(run, 4, "execute_payment", PAY)
    assert ok.status == "ok" and "SIMULATED" in ok.message
    assert len(_paid(enterprise)) == 1
    # duplicate execution attempt replays the ledger instead of paying again
    again = container.gateway.execute(run, 5, "execute_payment", PAY)
    assert again.status == "ok" and again.replayed and len(_paid(enterprise)) == 1


def test_rejected_approval_never_executes(container, run, enterprise):
    first = container.gateway.execute(run, 1, "execute_payment", PAY)
    container.approvals.decide(first.approval_id, "REJECT", "Approver", APPROVER_KEY)
    for step in (2, 3):
        assert container.gateway.execute(run, step, "execute_payment", PAY).status == "rejected"
    assert _paid(enterprise) == []


def test_stale_version_is_rejected_before_approval(container, run):
    out = container.gateway.execute(run, 1, "execute_payment", {**PAY, "expected_version": 7})
    assert out.status == "blocked" and "version" in out.message


def test_idempotent_autonomous_write(container, run, enterprise):
    params = {"task_id": "T-101", "body": "AutoPilot test note"}
    a = container.gateway.execute(run, 1, "add_task_comment", params)
    b = container.gateway.execute(run, 2, "add_task_comment", params)
    assert a.status == "ok" and b.status == "ok" and b.replayed
    with enterprise.svc() as h:
        assert len(h.get("/api/tasks/T-101/comments").json()) == 1


def test_optimistic_locking_conflict_is_reported(container, run):
    ok = container.gateway.execute(run, 1, "update_task_status", {"task_id": "T-101", "status": "At Risk", "expected_version": 1})
    assert ok.status == "ok"
    stale = container.gateway.execute(
        run, 2, "update_task_status", {"task_id": "T-101", "status": "Blocked", "expected_version": 1}
    )
    assert stale.status == "error" and "409" in stale.message


def test_disallowed_status_value_is_blocked(container, run):
    out = container.gateway.execute(run, 1, "update_task_status", {"task_id": "T-101", "status": "Done", "expected_version": 1})
    assert out.status == "blocked"


def test_invalid_parameters_rejected(container, run):
    out = container.gateway.execute(run, 1, "get_project", {"project_id": "../admin/reset"})
    assert out.status == "error" and "invalid parameters" in out.message


def test_integration_error_and_recovery(container, run):
    bad = container.gateway.execute(run, 1, "get_project", {"project_id": "PRJ-9999"})
    assert bad.status == "error" and "404" in bad.message
    good = container.gateway.execute(run, 2, "get_project", {"project_id": "PRJ-1001"})
    assert good.status == "ok"
    events = container.store.list_events(run["id"])
    assert [e["result"] for e in events] == ["ERROR", "SUCCESS"]


def test_agent_token_cannot_use_admin_endpoints(enterprise):
    import httpx

    h = {"Authorization": f"Bearer {SVC_TOKEN}"}
    assert httpx.post(enterprise.url + "/api/users/U-7/role", json={"role": "Admin"}, headers=h).status_code == 403
    assert httpx.post(enterprise.url + "/api/admin/reset", headers=h).status_code == 403


def test_secrets_never_reach_events(container, run):
    container.gateway.execute(run, 1, "get_project", {"project_id": "PRJ-1001"})
    container.gateway.execute(run, 2, "execute_payment", PAY)
    blob = json.dumps(container.store.list_events(run["id"])) + json.dumps(container.store.list_approvals(run_id=run["id"]))
    assert SVC_TOKEN not in blob and APPROVER_KEY not in blob
