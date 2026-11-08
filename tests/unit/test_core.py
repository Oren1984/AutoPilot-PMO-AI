import json
import logging

import pytest

from app.adapters.base import validate_params
from app.adapters.demo_api import SPECS
from app.config import Limits, Settings
from app.core.redaction import redact, redact_text
from app.core.states import InvalidTransition, RunState, check_transition
from app.observability.logging import JsonFormatter
from app.observability.tracker import LimitExceeded, add_model_usage, check_limits, new_usage

SPEC = {s.name: s for s in SPECS}


# ---------------- state machine ----------------
def test_happy_path_transitions():
    path = ["CREATED", "PLANNING", "RUNNING", "WAITING_APPROVAL", "RESUMING", "RUNNING", "VERIFYING", "COMPLETED"]
    for a, b in zip(path, path[1:], strict=False):
        check_transition(a, b)


@pytest.mark.parametrize(
    "a,b",
    [("CREATED", "COMPLETED"), ("WAITING_APPROVAL", "RUNNING"), ("PLANNING", "WAITING_APPROVAL"), ("RUNNING", "COMPLETED")],
)
def test_illegal_transitions(a, b):
    with pytest.raises(InvalidTransition):
        check_transition(a, b)


@pytest.mark.parametrize("terminal", ["COMPLETED", "REJECTED", "BLOCKED", "FAILED", "CANCELLED"])
def test_terminal_states_are_final(terminal):
    with pytest.raises(InvalidTransition):
        check_transition(terminal, RunState.RUNNING)


# ---------------- redaction ----------------
def test_redaction_nested_and_patterns():
    data = {
        "api_key": "abc",
        "nested": {"password": "hunter2", "note": "Authorization: Bearer abcdefghijklmnop"},
        "list": ["sk-ant-api03-SECRETSECRETSECRET", "card 4111 1111 1111 1111"],
        "safe": "PR-2001 48500 USD",
    }
    out = redact(data)
    dumped = json.dumps(out)
    for secret in ("hunter2", "abcdefghijklmnop", "SECRETSECRET", "4111 1111"):
        assert secret not in dumped
    assert out["api_key"] == "[REDACTED]"
    assert out["safe"] == "PR-2001 48500 USD"


def test_token_usage_counters_are_not_redacted():
    out = redact({"usage": {"input_tokens": 1200, "output_tokens": 340}, "input_tokens": "sk-ant-api03-SECRETSECRETSECRET"})
    assert out["usage"] == {"input_tokens": 1200, "output_tokens": 340}
    assert "SECRETSECRET" not in json.dumps(out)


def test_redact_known_secret_values():
    assert "my-very-secret-token" not in redact_text("token my-very-secret-token used", ["my-very-secret-token"])


def test_json_logs_are_redacted():
    rec = logging.LogRecord("t", logging.INFO, __file__, 1, "calling with token=supersecret123", None, None)
    rec.ctx = {"authorization": "Bearer abcdefghijk", "run_id": "RUN-1"}
    line = JsonFormatter().format(rec)
    assert "supersecret123" not in line and "abcdefghijk" not in line and "RUN-1" in line


# ---------------- cost / limits ----------------
def test_cost_labels():
    assert "deterministic simulation" in new_usage(Settings(agent_brain="simulated"))["cost_basis"]
    priced = Settings(agent_brain="claude", price_input_per_mtok=4.0, price_output_per_mtok=20.0)
    assert new_usage(priced)["cost_basis"].startswith("estimate")
    unpriced = Settings(agent_brain="claude")
    assert new_usage(unpriced)["cost_basis"].startswith("unavailable")


def test_cost_estimate_math():
    s = Settings(agent_brain="claude", price_input_per_mtok=4.0, price_output_per_mtok=20.0)
    u = new_usage(s)
    cost = add_model_usage(u, s, 1_000_000, 100_000)
    assert cost == pytest.approx(6.0)
    assert u["model_calls"] == 1 and u["estimated_model_cost_usd"] == pytest.approx(6.0)


def test_limits_enforced():
    u = new_usage(Settings())
    lim = Limits(max_steps=3, max_tool_calls=5, max_model_calls=1, max_cost_usd=1.0, run_timeout_seconds=10)
    check_limits(u, 3, 1, lim)
    with pytest.raises(LimitExceeded):
        check_limits(u, 4, 1, lim)
    with pytest.raises(LimitExceeded):
        check_limits(u, 1, 11, lim)
    u["estimated_model_cost_usd"] = 1.5
    with pytest.raises(LimitExceeded):
        check_limits(u, 1, 1, lim)


# ---------------- input validation ----------------
def test_param_validation():
    assert validate_params(SPEC["get_project"], {"project_id": "PRJ-1001"}) == []
    assert validate_params(SPEC["get_project"], {}) == ["missing required parameter 'project_id'"]
    assert "invalid format" in validate_params(SPEC["get_project"], {"project_id": "PRJ-1/../../admin/reset"})[0]
    assert "unexpected parameter" in validate_params(SPEC["get_project"], {"project_id": "PRJ-1001", "x": 1})[0]
    errs = validate_params(SPEC["update_task_status"], {"task_id": "T-1", "status": "Hacked", "expected_version": "1"})
    assert any("one of" in e for e in errs) and any("integer" in e for e in errs)
    assert validate_params(SPEC["get_project"], "not-a-dict") == ["parameters must be an object"]
