"""Execution gateway - the single boundary every agent action passes through.

Order of checks for each call (API and UI alike):
  1. capability exists + parameter validation
  2. hybrid routing (API / UI)
  3. deterministic policy evaluation (allow / approval / deny)
  4. for approval-required actions: approval lookup bound to exact parameters, expiry check,
     re-validation of the live record against the approved snapshot
  5. idempotency ledger (never duplicate a completed write; never auto-retry an uncertain financial action)
  6. adapter execution + structured event
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any

from app.adapters.base import AdapterError, validate_params
from app.adapters.toolbox import Toolbox
from app.config import Settings
from app.core.store import Store
from app.observability.logging import get_logger
from app.policies.engine import Decision, PolicyEngine
from app.workflows.approvals import ApprovalManager, params_hash

log = get_logger("gateway")


@dataclass
class Outcome:
    status: str  # ok | error | blocked | approval_required | rejected | expired | invalidated | uncertain
    action: str
    method: str
    data: Any = None
    message: str = ""
    policy: dict = field(default_factory=dict)
    approval_id: str | None = None
    screenshot: str | None = None
    browser_steps: list[str] = field(default_factory=list)
    duration_ms: int = 0
    replayed: bool = False

    def for_agent(self) -> dict:
        """What the agent sees as the tool result."""
        d = {"status": self.status, "action": self.action, "method": self.method, "message": self.message}
        if self.data is not None:
            d["data"] = self.data
        if self.approval_id:
            d["approval_id"] = self.approval_id
        if self.policy:
            d["policy_decision"] = self.policy.get("decision")
            d["policy_reason"] = self.policy.get("reason")
        return d


def _summarize(data: Any, limit: int = 1500) -> Any:
    text = json.dumps(data, default=str)
    if len(text) <= limit:
        return data
    if isinstance(data, list):
        return {"items": len(data), "preview": json.loads(json.dumps(data[:3], default=str))}
    return {"preview": text[:limit] + "..."}


class ExecutionGateway:
    def __init__(self, store: Store, policy: PolicyEngine, toolbox: Toolbox, approvals: ApprovalManager, settings: Settings):
        self.store = store
        self.policy = policy
        self.toolbox = toolbox
        self.approvals = approvals
        self.settings = settings

    # ------------------------------------------------------------------
    def preview(self, action: str, params: dict, method: str | None = None) -> dict:
        """Plan-time policy check (no execution). Enforcement still happens again in execute()."""
        m = self.toolbox.route(action, method)
        return self.policy.evaluate(action, params, m).as_dict() | {"method": m}

    # ------------------------------------------------------------------
    def execute(
        self,
        run: dict,
        step: int,
        action: str,
        params: dict,
        *,
        method: str | None = None,
        rationale: str = "",
        context: dict | None = None,
    ) -> Outcome:
        t0 = time.monotonic()
        usage = run["usage"]
        usage["tool_calls"] += 1
        spec = self.toolbox.spec(action)
        m = self.toolbox.route(action, method)

        if spec is None:
            decision = self.policy.evaluate(action, params if isinstance(params, dict) else {}, m)
            usage["policy_denials"] += 1
            return self._finish(
                run,
                step,
                Outcome(
                    "blocked", action, m, message=f"unknown capability '{action}'; {decision.reason}", policy=decision.as_dict()
                ),
                t0,
                params,
                rationale,
            )

        errors = validate_params(spec, params)
        if errors:
            usage["errors"] += 1
            return self._finish(
                run, step, Outcome("error", action, m, message="invalid parameters: " + "; ".join(errors)), t0, params, rationale
            )

        decision = self.policy.evaluate(action, params, m)
        pol = decision.as_dict()

        if decision.decision == Decision.DENY:
            usage["policy_denials"] += 1
            return self._finish(
                run,
                step,
                Outcome("blocked", action, m, message=f"Blocked by policy: {decision.reason}", policy=pol),
                t0,
                params,
                rationale,
            )

        approval = None
        if decision.decision == Decision.REQUIRE_APPROVAL:
            gate = self._approval_gate(run, step, action, m, params, pol, rationale, context or {})
            if isinstance(gate, Outcome):
                return self._finish(run, step, gate, t0, params, rationale)
            approval = gate

        return self._finish(
            run, step, self._run_adapter(run, step, action, m, params, pol, decision.financial, approval), t0, params, rationale
        )

    # ------------------------------------------------------------------
    def _approval_gate(self, run, step, action, method, params, pol, rationale, context) -> Outcome | dict:
        h = params_hash(action, method, params)
        approval = self.store.find_approval(run["id"], h)
        if approval:
            approval = self.approvals.expire_if_needed(approval)

        if approval is None or approval["status"] in ("EXPIRED", "INVALIDATED") and context.get("request_new_approval"):
            try:
                snapshot = self.toolbox.financial_snapshot(action, params)
            except AdapterError as exc:
                return Outcome("error", action, method, message=f"could not read record for approval: {exc}", policy=pol)
            mismatch = self._snapshot_mismatch(action, params, snapshot)
            if mismatch:
                return Outcome(
                    "blocked", action, method, message=f"request does not match the live record: {mismatch}", policy=pol
                )
            ctx = {
                "requested_action": action,
                "rationale": rationale[:1000],
                "risks": context.get("risks", []),
                "policy_reason": pol.get("reason"),
                "policy_level": pol.get("level"),
                "simulated": self.settings.enterprise == "demo",
                **{
                    k: snapshot.get(k)
                    for k in (
                        "project_id",
                        "project_name",
                        "title",
                        "justification",
                        "amount",
                        "currency",
                        "vendor",
                        "recipient_account_masked",
                        "requested_by",
                        "budget_remaining",
                    )
                },
            }
            approval = self.store.create_approval(
                run["id"], action, method, params, h, snapshot, ctx, self.settings.limits.approval_ttl_minutes
            )
            run["usage"]["approvals_requested"] += 1
            return Outcome(
                "approval_required",
                action,
                method,
                message="Execution paused: explicit human approval required",
                policy=pol,
                approval_id=approval["id"],
            )

        status = approval["status"]
        if status == "PENDING":
            return Outcome(
                "approval_required",
                action,
                method,
                message="Awaiting human decision; not executed",
                policy=pol,
                approval_id=approval["id"],
            )
        if status == "REJECTED":
            return Outcome(
                "rejected",
                action,
                method,
                message=f"Rejected by {approval.get('decided_by')}; action not executed",
                policy=pol,
                approval_id=approval["id"],
            )
        if status == "EXPIRED":
            return Outcome(
                "expired", action, method, message="Approval expired; action not executed", policy=pol, approval_id=approval["id"]
            )
        if status == "INVALIDATED":
            return Outcome(
                "invalidated",
                action,
                method,
                message=f"Approval invalidated: {approval.get('status_reason')}",
                policy=pol,
                approval_id=approval["id"],
            )
        if status == "CONSUMED":
            return approval  # ledger replay below returns the stored result without re-executing
        if status == "EXECUTION_UNCERTAIN":
            return Outcome(
                "uncertain",
                action,
                method,
                message="Previous execution outcome is uncertain; operator reconciliation required",
                policy=pol,
                approval_id=approval["id"],
            )

        # APPROVED -> re-validate against the live record right at the execution boundary
        try:
            live = self.toolbox.financial_snapshot(action, params)
        except AdapterError as exc:
            return Outcome(
                "error",
                action,
                method,
                message=f"re-validation failed (record unreadable): {exc}",
                policy=pol,
                approval_id=approval["id"],
            )
        reason = self._revalidate(approval, live, params)
        if reason:
            self.store.transition_approval(approval["id"], "APPROVED", "INVALIDATED", status_reason=reason)
            return Outcome(
                "invalidated",
                action,
                method,
                message=f"Approval invalidated at execution time: {reason}",
                policy=pol,
                approval_id=approval["id"],
            )
        return approval

    @staticmethod
    def _snapshot_mismatch(action: str, params: dict, snap: dict) -> str | None:
        if action != "execute_payment" or not snap:
            return None
        if snap["status"] != "Pending Approval":
            return f"purchase request status is '{snap['status']}'"
        if abs(float(snap["amount"]) - float(params["amount"])) > 0.005:
            return "amount differs from purchase request"
        if snap["currency"] != params["currency"]:
            return "currency differs from purchase request"
        if snap["vendor"] != params["recipient"]:
            return "recipient differs from purchase request vendor"
        if int(snap["version"]) != int(params["expected_version"]):
            return f"record version changed (now {snap['version']})"
        return None

    def _revalidate(self, approval: dict, live: dict, params: dict) -> str | None:
        snap = approval["record_snapshot"]
        if not snap:
            return None
        for key in ("version", "amount", "currency", "vendor", "status"):
            if live.get(key) != snap.get(key):
                return f"record changed after approval ({key}: {snap.get(key)!r} -> {live.get(key)!r})"
        recheck = self.policy.evaluate(approval["action"], params, approval["method"])
        if recheck.decision == Decision.DENY:
            return f"policy no longer permits the action: {recheck.reason}"
        if live.get("budget_remaining") is not None and float(live["budget_remaining"]) < float(params["amount"]):
            return "insufficient remaining project budget"
        return None

    # ------------------------------------------------------------------
    def _run_adapter(self, run, step, action, method, params, pol, financial, approval) -> Outcome:
        spec = self.toolbox.spec(action)
        is_write = spec.kind in ("write", "financial", "admin")
        if approval is not None:
            idem = f"pay-{approval['id']}"
        else:
            idem = hashlib.sha256(json.dumps([run["id"], action, method, params], sort_keys=True).encode()).hexdigest()[:32]

        if is_write:
            existing = self.store.get_execution(idem)
            if existing and existing["status"] == "SUCCEEDED":
                return Outcome(
                    "ok",
                    action,
                    method,
                    data=existing["result"],
                    message="Already executed - returning recorded result (no duplicate write)",
                    policy=pol,
                    approval_id=approval and approval["id"],
                    replayed=True,
                )
            if existing and existing["status"] in ("STARTED", "UNCERTAIN") and financial:
                return Outcome(
                    "uncertain",
                    action,
                    method,
                    message="A previous attempt of this financial action has an unknown outcome; it will not be retried automatically",
                    policy=pol,
                    approval_id=approval and approval["id"],
                )
            if existing:
                self.store.reset_execution(idem)
            if not self.store.begin_execution(idem, run["id"], action, method, params, financial):
                return Outcome("error", action, method, message="concurrent execution detected; not executed", policy=pol)

        ctx = {"run_id": run["id"], "step": step, "idempotency_key": idem, "approval_id": approval["id"] if approval else None}
        attempts = 1 if is_write else 3  # writes are never retried blindly
        result = None
        last_exc: AdapterError | None = None
        for attempt in range(attempts):
            try:
                result = self.toolbox.execute(action, params, method, ctx)
                last_exc = None
                break
            except AdapterError as exc:
                last_exc = exc
                if not exc.retryable or attempt == attempts - 1:
                    break
                time.sleep(0.3 * (attempt + 1))

        if result is None and last_exc is not None:
            run["usage"]["errors"] += 1
            if is_write:
                if last_exc.uncertain:
                    self.store.finish_execution(idem, "UNCERTAIN", {"error": str(last_exc)})
                    if approval:
                        self.store.transition_approval(
                            approval["id"], "APPROVED", "EXECUTION_UNCERTAIN", status_reason=str(last_exc)
                        )
                    return Outcome(
                        "uncertain",
                        action,
                        method,
                        message=f"Outcome unknown, not retried: {last_exc}",
                        policy=pol,
                        approval_id=approval and approval["id"],
                    )
                self.store.finish_execution(idem, "FAILED", {"error": str(last_exc)})
            return Outcome("error", action, method, message=str(last_exc), policy=pol, approval_id=approval and approval["id"])

        if method == "ui":
            run["usage"]["ui_operations"] += 1
        else:
            run["usage"]["api_calls"] += 1
        if is_write:
            self.store.finish_execution(idem, "SUCCEEDED", result.data)
        if approval is not None:
            self.store.transition_approval(approval["id"], "APPROVED", "CONSUMED", status_reason="executed once")
        msg = "Executed (SIMULATED payment - no real funds moved)" if financial else "Executed"
        return Outcome(
            "ok",
            action,
            method,
            data=result.data,
            message=msg,
            policy=pol,
            approval_id=approval and approval["id"],
            screenshot=result.screenshot,
            browser_steps=result.meta.get("browser_steps", []),
        )

    # ------------------------------------------------------------------
    def _finish(self, run: dict, step: int, out: Outcome, t0: float, params: dict, rationale: str) -> Outcome:
        out.duration_ms = int((time.monotonic() - t0) * 1000)
        result_label = {
            "ok": "SUCCESS" if not out.replayed else "SUCCESS (replayed)",
            "error": "ERROR",
            "blocked": "BLOCKED",
            "approval_required": "APPROVAL REQUIRED",
            "rejected": "REJECTED",
            "expired": "EXPIRED",
            "invalidated": "INVALIDATED",
            "uncertain": "UNCERTAIN",
        }[out.status]
        self.store.add_event(
            run["id"],
            "TOOL_CALL",
            state=run.get("state"),
            step=step,
            action=out.action,
            method=out.method.upper(),
            result=result_label,
            policy=out.policy.get("decision", "DENY" if out.status == "blocked" else None),
            duration_ms=out.duration_ms,
            cost_usd=0.0,
            message=out.message,
            screenshot=out.screenshot,
            detail={
                "params": params,
                "rationale": rationale,
                "policy": out.policy,
                "approval_id": out.approval_id,
                "browser_steps": out.browser_steps,
                "result": _summarize(out.data) if out.data is not None else None,
            },
        )
        log.info(
            "tool call",
            extra={
                "ctx": {
                    "run_id": run["id"],
                    "step": step,
                    "action": out.action,
                    "method": out.method,
                    "status": out.status,
                    "duration_ms": out.duration_ms,
                }
            },
        )
        return out
