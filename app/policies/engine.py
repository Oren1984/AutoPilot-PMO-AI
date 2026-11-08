"""Deterministic policy engine.

The model is never the authority on whether an action is permitted. Every tool call - whether it is
executed through the API or through the browser - is evaluated here at the execution boundary.
"""

from __future__ import annotations

import copy
import json
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any

DEFAULT_POLICY_PATH = Path(__file__).with_name("default_policy.json")


class Level(StrEnum):
    AUTONOMOUS = "autonomous"  # Level 1
    APPROVAL_REQUIRED = "approval_required"  # Level 2
    PROHIBITED = "prohibited"  # Level 3


class Decision(StrEnum):
    ALLOW = "ALLOW"
    REQUIRE_APPROVAL = "REQUIRE_APPROVAL"
    DENY = "DENY"


@dataclass(frozen=True)
class PolicyDecision:
    decision: Decision
    level: Level
    reason: str
    rule: str
    financial: bool = False

    @property
    def allowed(self) -> bool:
        return self.decision == Decision.ALLOW

    def as_dict(self) -> dict:
        d = asdict(self)
        d["decision"] = self.decision.value
        d["level"] = self.level.value
        return d

    def label(self) -> str:
        return {
            Decision.ALLOW: "ALLOW (L1 autonomous)",
            Decision.REQUIRE_APPROVAL: "APPROVAL REQUIRED (L2)",
            Decision.DENY: f"DENY ({'L3 prohibited' if self.level == Level.PROHIBITED else 'policy violation'})",
        }[self.decision]


def _freeze(obj: Any) -> Any:
    if isinstance(obj, dict):
        return MappingProxyType({k: _freeze(v) for k, v in obj.items()})
    if isinstance(obj, list):
        return tuple(_freeze(v) for v in obj)
    return obj


class PolicyEngine:
    def __init__(self, policy: dict | None = None, path: Path = DEFAULT_POLICY_PATH):
        raw = policy if policy is not None else json.loads(Path(path).read_text(encoding="utf-8"))
        self._raw = copy.deepcopy(raw)
        self._policy = _freeze(raw)  # immutable at runtime

    @property
    def version(self) -> str:
        return self._policy.get("version", "unknown")

    def document(self) -> dict:
        return copy.deepcopy(self._raw)

    def rule_for(self, action: str) -> Any:
        return self._policy["actions"].get(action)

    def is_financial(self, action: str) -> bool:
        rule = self.rule_for(action)
        return bool(rule and rule.get("financial"))

    def evaluate(self, action: str, params: dict, method: str) -> PolicyDecision:
        rule = self.rule_for(action)
        if rule is None:
            return PolicyDecision(
                Decision.DENY, Level.PROHIBITED, f"'{action}' is not in the policy allow-list (default deny)", "default_deny"
            )

        level = Level(rule["level"])
        financial = bool(rule.get("financial"))
        if level == Level.PROHIBITED:
            return PolicyDecision(
                Decision.DENY, level, f"'{action}' is a Level 3 prohibited action", "prohibited_action", financial
            )

        allowed_methods = rule.get("methods", ())
        if financial:
            allowed_methods = tuple(m for m in allowed_methods if m in self._policy["financial"]["allowed_methods"])
        if method not in allowed_methods:
            why = "financial actions may only run through the governed API path" if financial else "method not permitted"
            return PolicyDecision(
                Decision.DENY, level, f"'{action}' via '{method}' is not permitted: {why}", "method_not_allowed", financial
            )

        for field_name, max_len in (rule.get("max_length") or {}).items():
            value = params.get(field_name)
            if isinstance(value, str) and len(value) > max_len:
                return PolicyDecision(
                    Decision.DENY, level, f"'{field_name}' exceeds {max_len} characters", "max_length", financial
                )

        needs_approval = level == Level.APPROVAL_REQUIRED
        for field_name, allowed in (rule.get("allowed_values") or {}).items():
            if field_name not in params:
                continue
            value = params[field_name]
            approval_values = (rule.get("approval_values") or {}).get(field_name, ())
            if value in approval_values:
                needs_approval = True
            elif value not in allowed:
                return PolicyDecision(
                    Decision.DENY, level, f"value '{value}' for '{field_name}' is not permitted", "value_not_allowed", financial
                )

        if financial:
            fin = self._policy["financial"]
            amount = params.get("amount")
            currency = params.get("currency")
            if not isinstance(amount, (int, float)) or amount <= 0:
                return PolicyDecision(
                    Decision.DENY, level, "financial action requires a positive amount", "financial_amount", True
                )
            if currency not in fin["allowed_currencies"]:
                return PolicyDecision(Decision.DENY, level, f"currency '{currency}' not allowed", "financial_currency", True)
            if amount > fin["hard_limit_per_action"]:
                return PolicyDecision(
                    Decision.DENY,
                    Level.PROHIBITED,
                    f"amount {amount:,.2f} {currency} exceeds the per-action hard limit "
                    f"{fin['hard_limit_per_action']:,.0f} - must be handled by Finance outside the agent",
                    "financial_hard_limit",
                    True,
                )
            needs_approval = True  # every financial action requires explicit human approval

        if needs_approval:
            return PolicyDecision(
                Decision.REQUIRE_APPROVAL,
                Level.APPROVAL_REQUIRED,
                "explicit human approval is required before execution"
                + (" (financial commitment)" if financial else " (high-impact change)"),
                "approval_required",
                financial,
            )
        return PolicyDecision(
            Decision.ALLOW, Level.AUTONOMOUS, "Level 1 autonomous action within policy", "autonomous", financial
        )
