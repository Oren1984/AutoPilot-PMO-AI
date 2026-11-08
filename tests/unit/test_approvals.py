from datetime import UTC, datetime, timedelta

import pytest

from app.core.store import Store
from app.workflows.approvals import ApprovalError, ApprovalManager, params_hash

KEY = "unit-approver-key"


@pytest.fixture
def mgr(tmp_path):
    store = Store(tmp_path / "a.db")
    yield ApprovalManager(store, KEY)
    store.close()


def _approval(mgr, ttl=30):
    run = mgr.store.create_run("x", "demo", "simulated")
    p = {"pr_id": "PR-1", "amount": 10.0}
    return mgr.store.create_approval(
        run["id"], "execute_payment", "api", p, params_hash("execute_payment", "api", p), {}, {}, ttl
    )


def test_params_hash_binds_exact_parameters():
    a = params_hash("execute_payment", "api", {"amount": 10.0, "pr_id": "PR-1"})
    b = params_hash("execute_payment", "api", {"pr_id": "PR-1", "amount": 10.0})
    c = params_hash("execute_payment", "api", {"pr_id": "PR-1", "amount": 10.01})
    d = params_hash("execute_payment", "ui", {"pr_id": "PR-1", "amount": 10.0})
    assert a == b and a != c and a != d


@pytest.mark.parametrize("key", [None, "", "wrong-key"])
def test_unauthorized_approval_rejected(mgr, key):
    a = _approval(mgr)
    with pytest.raises(ApprovalError) as e:
        mgr.decide(a["id"], "APPROVE", "Oren", key)
    assert e.value.status == 401
    assert mgr.store.get_approval(a["id"])["status"] == "PENDING"


@pytest.mark.parametrize("who", ["agent", "AutoPilot", "Claude", "system"])
def test_agent_cannot_self_approve(mgr, who):
    a = _approval(mgr)
    with pytest.raises(ApprovalError) as e:
        mgr.decide(a["id"], "APPROVE", who, KEY)
    assert e.value.status == 403


def test_duplicate_decision_rejected(mgr):
    a = _approval(mgr)
    assert mgr.decide(a["id"], "APPROVE", "Oren", KEY)["status"] == "APPROVED"
    with pytest.raises(ApprovalError) as e:
        mgr.decide(a["id"], "REJECT", "Someone else", KEY)
    assert e.value.status == 409
    assert mgr.store.get_approval(a["id"])["status"] == "APPROVED"


def test_expired_approval_cannot_be_decided(mgr):
    a = _approval(mgr)
    past = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    mgr.store._exec("UPDATE approvals SET expires_at=? WHERE id=?", (past, a["id"]))
    with pytest.raises(ApprovalError) as e:
        mgr.decide(a["id"], "APPROVE", "Oren", KEY)
    assert e.value.status == 410
    assert mgr.store.get_approval(a["id"])["status"] == "EXPIRED"


def test_invalid_decision_value(mgr):
    a = _approval(mgr)
    with pytest.raises(ApprovalError):
        mgr.decide(a["id"], "MAYBE", "Oren", KEY)


def test_approval_is_never_implied(mgr):
    a = _approval(mgr)
    # silence / time passing / reading it does not change the status
    for _ in range(3):
        assert mgr.expire_if_needed(mgr.store.get_approval(a["id"]))["status"] == "PENDING"
