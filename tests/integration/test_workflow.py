"""End-to-end workflow tests against a real (local) DemoCorp server, real HTTP and real Chromium."""

import pytest

from app.config import Limits
from tests.conftest import APPROVER_KEY

pytestmark = pytest.mark.browser

ASSIGNMENT = (
    "Review the active project, identify overdue tasks and delivery risks, update permitted task statuses, prepare the "
    "next project actions, review the pending financial request, and complete all authorized work. Stop for human "
    "approval before any financial commitment or payment."
)


def _start(c):
    run = c.runtime.start_run(ASSIGNMENT, background=False)
    return c.store.get_run(run["id"])


def _decide(c, run, decision):
    c.approvals.decide(run["pending_approval_id"], decision, "Test Approver", APPROVER_KEY)
    c.runtime.resume_after_decision(run["id"], background=False)
    return c.store.get_run(run["id"])


def _pr(enterprise, pr_id="PR-2001"):
    with enterprise.svc() as h:
        return h.get(f"/api/purchase-requests/{pr_id}").json()


def _events(c, run_id, type_=None):
    return [e for e in c.store.list_events(run_id) if type_ is None or e["type"] == type_]


def test_runs_to_approval_checkpoint_with_hybrid_execution(container, enterprise):
    run = _start(container)
    assert run["state"] == "WAITING_APPROVAL"
    calls = _events(container, run["id"], "TOOL_CALL")
    methods = {e["method"] for e in calls if e["result"].startswith("SUCCESS")}
    assert {"API", "UI"} <= methods, "both execution methods must be exercised"
    ui_submit = [e for e in calls if e["action"] == "ui_submit_status_report"][0]
    assert ui_submit["result"] == "SUCCESS" and ui_submit["screenshot"]
    assert ui_submit["detail"]["browser_steps"]
    # financial action stopped before execution - nothing paid while pending
    pr = _pr(enterprise)
    assert pr["status"] == "Pending Approval" and pr["payments"] == []
    approval = container.store.get_approval(run["pending_approval_id"])
    ctx = approval["context"]
    assert approval["status"] == "PENDING" and ctx["amount"] == 48500.0 and ctx["vendor"] == "Skyline Cloud Tools Ltd."
    assert ctx["justification"] and ctx["policy_reason"] and ctx["project_id"] == "PRJ-1001"
    # Level 3: the over-limit request was never attempted
    assert _pr(enterprise, "PR-2002")["payments"] == []
    assert any(e["type"] == "SECURITY" for e in _events(container, run["id"]))
    # waiting again does not execute (re-driving the resume while PENDING is a no-op)
    container.runtime.resume_after_decision(run["id"], background=False)
    assert container.store.get_run(run["id"])["state"] == "WAITING_APPROVAL"
    assert _pr(enterprise)["payments"] == []


def test_approved_path_executes_once_and_verifies(container, enterprise):
    run = _decide(container, _start(container), "APPROVE")
    assert run["state"] == "COMPLETED", run["error"]
    assert run["result"]["verified"] is True
    assert all(ch["ok"] for ch in run["result"]["report"]["checks"])
    pr = _pr(enterprise)
    assert pr["status"] == "Paid (Simulated)" and len(pr["payments"]) == 1 and pr["payments"][0]["simulated"] == 1
    approval = container.store.list_approvals(run_id=run["id"])[0]
    assert approval["status"] == "CONSUMED"
    # re-running the same approved call cannot pay twice
    out = container.gateway.execute(run, 99, "execute_payment", approval["params"])
    assert out.status == "ok" and out.replayed
    assert len(_pr(enterprise)["payments"]) == 1
    states = [e["state"] for e in _events(container, run["id"], "STATE")]
    assert states == ["PLANNING", "RUNNING", "WAITING_APPROVAL", "RESUMING", "RUNNING", "VERIFYING", "COMPLETED"]


def test_rejected_path_leaves_action_unexecuted(container, enterprise):
    run = _decide(container, _start(container), "REJECT")
    assert run["state"] == "REJECTED"
    assert run["result"]["verified"] is True
    pr = _pr(enterprise)
    assert pr["status"] == "Pending Approval" and pr["payments"] == []
    assert container.store.list_approvals(run_id=run["id"])[0]["status"] == "REJECTED"


def test_changed_record_after_approval_invalidates(container, enterprise):
    run = _start(container)
    container.approvals.decide(run["pending_approval_id"], "APPROVE", "Test Approver", APPROVER_KEY)
    with enterprise.admin() as h:  # another user edits the amount after the approval
        h.patch("/api/admin/purchase-requests/PR-2001", json={"amount": 49999.0}).raise_for_status()
    container.runtime.resume_after_decision(run["id"], background=False)
    run = container.store.get_run(run["id"])
    assert run["state"] == "BLOCKED"
    assert container.store.list_approvals(run_id=run["id"])[0]["status"] == "INVALIDATED"
    assert _pr(enterprise)["payments"] == []


def test_expired_approval_is_not_executed(container, enterprise):
    run = _start(container)
    container.store._exec("UPDATE approvals SET expires_at='2000-01-01T00:00:00+00:00' WHERE id=?", (run["pending_approval_id"],))
    with pytest.raises(Exception):  # noqa: B017
        container.approvals.decide(run["pending_approval_id"], "APPROVE", "Late Approver", APPROVER_KEY)
    container.runtime.resume_after_decision(run["id"], background=False)
    run = container.store.get_run(run["id"])
    assert run["state"] == "BLOCKED"
    assert _pr(enterprise)["payments"] == []


def test_cancel_while_waiting(container, enterprise):
    run = _start(container)
    container.runtime.request_cancel(run["id"])
    run = container.store.get_run(run["id"])
    assert run["state"] == "CANCELLED"
    assert container.store.list_approvals(run_id=run["id"])[0]["status"] == "INVALIDATED"
    with pytest.raises(Exception):  # noqa: B017
        container.approvals.decide(run["pending_approval_id"], "APPROVE", "Test Approver", APPROVER_KEY)
    assert _pr(enterprise)["payments"] == []


def test_pause_and_resume(container, run_until):
    run = container.runtime.start_run(ASSIGNMENT, background=True)
    container.runtime.request_pause(run["id"])
    paused = run_until(container.store, run["id"], {"PAUSED", "WAITING_APPROVAL"}, timeout=60)
    if paused["state"] == "PAUSED":
        container.runtime.request_resume(run["id"], background=False)
    assert container.store.get_run(run["id"])["state"] == "WAITING_APPROVAL"


def test_max_steps_limit_blocks_run(make_container):
    c = make_container(limits=Limits(max_steps=4))
    run = _start(c)
    assert run["state"] == "BLOCKED"
    assert "maximum steps" in run["error"]


def test_restart_resumes_pending_approval_from_checkpoint(make_container, enterprise):
    c1 = make_container()
    run = _start(c1)
    step_before = run["step_count"]
    c1.store.close()
    # "restart": brand-new object graph over the same SQLite file
    c2 = make_container()
    assert c2.runtime.recover() == []  # waiting runs stay waiting; nothing auto-executes
    run2 = c2.store.get_run(run["id"])
    assert run2["state"] == "WAITING_APPROVAL" and run2["step_count"] == step_before
    run2 = _decide(c2, run2, "APPROVE")
    assert run2["state"] == "COMPLETED"
    assert len(_pr(enterprise)["payments"]) == 1


def test_restart_mid_run_resumes_from_checkpoint(make_container, enterprise):
    c1 = make_container()
    c1.runtime.start_run(ASSIGNMENT, background=True)
    from app.agent.runtime import wait_for_state

    run_id = c1.store.list_runs(1)[0]["id"]
    # simulate a crash: freeze the run in RUNNING state at its last checkpoint
    wait_for_state(c1.store, run_id, {"WAITING_APPROVAL"}, 60)
    c1.store.update_run(run_id, state="RUNNING", pending_approval_id=None, pending_call=None)
    approvals = c1.store.list_approvals(run_id=run_id)
    c1.store.transition_approval(approvals[0]["id"], "PENDING", "INVALIDATED", status_reason="test reset")
    c1.store._exec("DELETE FROM approvals WHERE run_id=?", (run_id,))
    # roll the brain checkpoint back so the payment call is re-planned
    st = c1.store.get_run(run_id)["brain_state"]
    st["queue"] = []
    c1.store.update_run(run_id, brain_state=st)
    c1.store.close()

    c2 = make_container()
    assert run_id in c2.runtime.recover()
    c2.runtime.wait(run_id, 60)
    run = c2.store.get_run(run_id)
    assert any(e["type"] == "RECOVERY" for e in c2.store.list_events(run_id))
    assert run["state"] in {"VERIFYING", "COMPLETED", "FAILED", "WAITING_APPROVAL"}
    # completed writes were not duplicated: one comment per overdue task, not two
    with enterprise.svc() as h:
        comments = h.get("/api/tasks/T-101/comments").json()
    assert len(comments) == 1


def test_uncertain_financial_execution_is_never_retried(make_container, enterprise):
    c1 = make_container()
    run = _start(c1)
    c1.approvals.decide(run["pending_approval_id"], "APPROVE", "Test Approver", APPROVER_KEY)
    # crash right after the payment was marked in-flight, before its result was recorded
    idem = f"pay-{run['pending_approval_id']}"
    c1.store.begin_execution(idem, run["id"], "execute_payment", "api", {}, True)
    c1.store.update_run(run["id"], state="RESUMING")
    c1.store.close()
    c2 = make_container()
    c2.runtime.recover()
    run = c2.store.get_run(run["id"])
    assert run["state"] == "BLOCKED"
    assert c2.store.get_execution(idem)["status"] == "UNCERTAIN"
    assert _pr(enterprise)["payments"] == []
