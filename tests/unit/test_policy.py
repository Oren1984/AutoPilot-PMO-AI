import pytest

from app.policies.engine import Decision, Level, PolicyEngine


@pytest.fixture(scope="module")
def policy():
    return PolicyEngine()


PAY = {"pr_id": "PR-2001", "amount": 48500.0, "currency": "USD", "recipient": "Skyline", "expected_version": 1}


def test_reads_are_autonomous(policy):
    d = policy.evaluate("list_tasks", {"project_id": "PRJ-1001"}, "api")
    assert d.decision == Decision.ALLOW and d.level == Level.AUTONOMOUS


def test_unknown_action_default_deny(policy):
    d = policy.evaluate("transfer_funds", {}, "api")
    assert d.decision == Decision.DENY and d.rule == "default_deny"


@pytest.mark.parametrize("action", ["change_user_role", "delete_record"])
def test_level3_prohibited(policy, action):
    d = policy.evaluate(action, {"user_id": "U-7", "role": "Admin"}, "api")
    assert d.decision == Decision.DENY and d.level == Level.PROHIBITED


def test_every_payment_requires_approval(policy):
    d = policy.evaluate("execute_payment", PAY, "api")
    assert d.decision == Decision.REQUIRE_APPROVAL and d.financial


def test_tiny_payment_still_requires_approval(policy):
    d = policy.evaluate("execute_payment", {**PAY, "amount": 0.01}, "api")
    assert d.decision == Decision.REQUIRE_APPROVAL


def test_payment_over_hard_limit_is_prohibited(policy):
    d = policy.evaluate("execute_payment", {**PAY, "amount": 185000.0}, "api")
    assert d.decision == Decision.DENY and d.rule == "financial_hard_limit"


def test_financial_action_cannot_use_ui_path(policy):
    d = policy.evaluate("execute_payment", PAY, "ui")
    assert d.decision == Decision.DENY and d.rule == "method_not_allowed"


def test_disallowed_currency_and_amount(policy):
    assert policy.evaluate("execute_payment", {**PAY, "currency": "BTC"}, "api").decision == Decision.DENY
    assert policy.evaluate("execute_payment", {**PAY, "amount": -5}, "api").decision == Decision.DENY


def test_value_constraints(policy):
    base = {"task_id": "T-101", "expected_version": 1}
    assert policy.evaluate("update_task_status", {**base, "status": "At Risk"}, "api").decision == Decision.ALLOW
    assert policy.evaluate("update_task_status", {**base, "status": "Done"}, "api").decision == Decision.DENY
    assert policy.evaluate("update_task_status", {**base, "status": "Cancelled"}, "api").decision == Decision.REQUIRE_APPROVAL
    ms = {"milestone_id": "M-11", "expected_version": 1}
    assert policy.evaluate("update_milestone_health", {**ms, "health": "Off Track"}, "api").decision == Decision.REQUIRE_APPROVAL


def test_ui_only_action_rejected_via_api(policy):
    d = policy.evaluate("ui_submit_status_report", {"project_id": "PRJ-1001", "rag": "Red", "summary": "x"}, "api")
    assert d.decision == Decision.DENY


def test_max_length(policy):
    d = policy.evaluate("add_task_comment", {"task_id": "T-101", "body": "x" * 2001}, "api")
    assert d.decision == Decision.DENY and d.rule == "max_length"


def test_policy_is_immutable_at_runtime(policy):
    with pytest.raises(TypeError):
        policy._policy["actions"]["change_user_role"] = {"level": "autonomous"}  # type: ignore[index]
    doc = policy.document()
    doc["actions"]["change_user_role"]["level"] = "autonomous"
    assert policy.evaluate("change_user_role", {"user_id": "U-7", "role": "Admin"}, "api").decision == Decision.DENY
