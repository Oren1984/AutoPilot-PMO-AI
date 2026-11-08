"""Usage and cost tracking. Costs are only ever estimates computed from provider-reported token
counts and configured prices; when either is missing the figure is labelled unavailable."""

from __future__ import annotations

from app.config import Limits, Settings


class LimitExceeded(Exception):
    pass


def new_usage(settings: Settings) -> dict:
    priced = settings.price_input_per_mtok > 0 or settings.price_output_per_mtok > 0
    return {
        "model_calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "estimated_model_cost_usd": 0.0,
        "cost_basis": (
            "no model used (deterministic simulation)"
            if settings.agent_brain != "claude"
            else (
                f"estimate: provider-reported tokens x configured price "
                f"(${settings.price_input_per_mtok}/MTok in, ${settings.price_output_per_mtok}/MTok out)"
                if priced
                else "unavailable: no model pricing configured"
            )
        ),
        "api_calls": 0,
        "ui_operations": 0,
        "tool_calls": 0,
        "policy_denials": 0,
        "approvals_requested": 0,
        "errors": 0,
    }


def add_model_usage(usage: dict, settings: Settings, input_tokens: int | None, output_tokens: int | None) -> float:
    usage["model_calls"] += 1
    usage["input_tokens"] += int(input_tokens or 0)
    usage["output_tokens"] += int(output_tokens or 0)
    cost = (
        int(input_tokens or 0) * settings.price_input_per_mtok + int(output_tokens or 0) * settings.price_output_per_mtok
    ) / 1_000_000
    usage["estimated_model_cost_usd"] = round(usage["estimated_model_cost_usd"] + cost, 6)
    return cost


def check_limits(usage: dict, step_count: int, elapsed_s: float, limits: Limits) -> None:
    if step_count > limits.max_steps:
        raise LimitExceeded(f"maximum steps reached ({limits.max_steps})")
    if usage["tool_calls"] > limits.max_tool_calls:
        raise LimitExceeded(f"maximum tool calls reached ({limits.max_tool_calls})")
    if usage["model_calls"] > limits.max_model_calls:
        raise LimitExceeded(f"maximum model calls reached ({limits.max_model_calls})")
    if usage["estimated_model_cost_usd"] > limits.max_cost_usd:
        raise LimitExceeded(f"estimated cost ceiling reached (${limits.max_cost_usd})")
    if elapsed_s > limits.run_timeout_seconds:
        raise LimitExceeded(f"run timeout reached ({limits.run_timeout_seconds}s)")
