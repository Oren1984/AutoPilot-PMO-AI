"""Agent runtime / orchestrator.

Drives a brain step by step, sends every action through the ExecutionGateway, persists a durable
checkpoint after every step, pauses at approval checkpoints, and resumes from the stored checkpoint
after a human decision or an application restart.
"""

from __future__ import annotations

import threading
import time
import traceback
from collections.abc import Callable
from datetime import UTC, datetime

from app.agent.base import Finish, ToolCall
from app.config import Settings
from app.core.states import ACTIVE, TERMINAL, RunState, check_transition
from app.core.store import Store, parse_iso
from app.observability.logging import get_logger
from app.observability.tracker import LimitExceeded, add_model_usage, check_limits, new_usage
from app.workflows.gateway import ExecutionGateway

log = get_logger("runtime")


class AgentRuntime:
    def __init__(self, store: Store, gateway: ExecutionGateway, settings: Settings, brain_factory: Callable[[], object]):
        self.store = store
        self.gateway = gateway
        self.settings = settings
        self.brain_factory = brain_factory
        self._threads: dict[str, threading.Thread] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    # ------------------------------------------------------------------ public API
    def start_run(self, assignment: str, background: bool = True) -> dict:
        assignment = (assignment or "").strip()
        if not assignment:
            raise ValueError("assignment is required")
        if len(assignment) > 4000:
            raise ValueError("assignment too long (max 4000 characters)")
        brain = self.brain_factory()
        run = self.store.create_run(assignment, self.settings.mode, brain.kind)
        self.store.update_run(run["id"], usage=new_usage(self.settings))
        self._event(
            run["id"],
            "RUN_CREATED",
            RunState.CREATED,
            message="Assignment received",
            detail={
                "assignment": assignment,
                "agent": "simulated (deterministic, not Claude)" if brain.simulated else f"Claude {self.settings.claude_model}",
            },
        )
        self._spawn(run["id"], self._drive_new, background)
        return self.store.get_run(run["id"])

    def resume_after_decision(self, run_id: str, background: bool = True) -> None:
        self._spawn(run_id, self._drive_resume, background)

    def request_pause(self, run_id: str) -> dict:
        run = self._require(run_id)
        if run["state"] in TERMINAL:
            raise ValueError(f"run is {run['state']}")
        self.store.update_run(run_id, pause_requested=True)
        self._event(run_id, "OPERATOR", run["state"], message="Pause requested by operator")
        return self.store.get_run(run_id)

    def request_resume(self, run_id: str, background: bool = True) -> dict:
        run = self._require(run_id)
        if run["state"] != RunState.PAUSED:
            raise ValueError("only a PAUSED run can be resumed (approval waits resume via the approval decision)")
        self.store.update_run(run_id, pause_requested=False)
        self._event(run_id, "OPERATOR", run["state"], message="Resume requested by operator")
        self._spawn(run_id, self._drive_paused, background)
        return self.store.get_run(run_id)

    def request_cancel(self, run_id: str) -> dict:
        run = self._require(run_id)
        if run["state"] in TERMINAL:
            raise ValueError(f"run is already {run['state']}")
        self.store.update_run(run_id, cancel_requested=True)
        self._event(run_id, "OPERATOR", run["state"], message="Cancel requested by operator")
        if run["state"] in (RunState.WAITING_APPROVAL, RunState.PAUSED, RunState.CREATED):
            with self._lock_for(run_id):
                if run["pending_approval_id"]:
                    self.store.transition_approval(
                        run["pending_approval_id"], "PENDING", "INVALIDATED", status_reason="run cancelled by operator"
                    )
                self._transition(run_id, RunState.CANCELLED, "Run cancelled; pending action was not executed")
        return self.store.get_run(run_id)

    def recover(self) -> list[str]:
        """Called at application start. Resumes interrupted runs from their last checkpoint."""
        resumed = []
        for run in self.store.runs_in_states([s.value for s in ACTIVE]):
            uncertain = [e for e in self.store.list_executions(run["id"]) if e["status"] == "STARTED"]
            for ex in uncertain:
                if ex["financial"]:
                    self.store.finish_execution(ex["idem_key"], "UNCERTAIN", {"error": "process stopped mid-execution"})
                else:
                    self.store.finish_execution(
                        ex["idem_key"], "FAILED", {"error": "process stopped mid-execution; safe to retry"}
                    )
            if any(e["financial"] for e in uncertain):
                self._event(
                    run["id"],
                    "RECOVERY",
                    run["state"],
                    message="Restart detected an in-flight financial action; it will NOT be retried automatically",
                )
                self._transition(
                    run["id"],
                    RunState.BLOCKED,
                    "Blocked after restart: financial action outcome unknown - operator reconciliation required",
                )
                continue
            self._event(
                run["id"],
                "RECOVERY",
                run["state"],
                message=f"Application restart: resuming from checkpoint at step {run['step_count']}",
            )
            self._spawn(run["id"], self._drive_existing, True)
            resumed.append(run["id"])
        for run in self.store.runs_in_states([RunState.WAITING_APPROVAL.value]):
            approval = self.store.get_approval(run["pending_approval_id"]) if run["pending_approval_id"] else None
            if approval and approval["status"] in ("APPROVED", "REJECTED"):
                # decision was recorded but the resume did not start before the restart
                self._spawn(run["id"], self._drive_resume, True)
                resumed.append(run["id"])
        return resumed

    def wait(self, run_id: str, timeout: float = 60.0) -> None:
        t = self._threads.get(run_id)
        if t:
            t.join(timeout)

    # ------------------------------------------------------------------ internals
    def _require(self, run_id: str) -> dict:
        run = self.store.get_run(run_id)
        if not run:
            raise KeyError(run_id)
        return run

    def _lock_for(self, run_id: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(run_id, threading.Lock())

    def _spawn(self, run_id: str, fn, background: bool) -> None:
        def target():
            lock = self._lock_for(run_id)
            if not lock.acquire(blocking=False):
                log.info("run already being driven", extra={"ctx": {"run_id": run_id}})
                return
            try:
                fn(run_id)
            except Exception as exc:  # noqa: BLE001 - never leave a run hanging silently
                log.error("run crashed", exc_info=True, extra={"ctx": {"run_id": run_id}})
                self._fail(run_id, f"internal error: {type(exc).__name__}: {exc}", traceback.format_exc(limit=3))
            finally:
                lock.release()

        if background:
            th = threading.Thread(target=target, name=f"run-{run_id}", daemon=True)
            self._threads[run_id] = th
            th.start()
        else:
            target()

    def _event(self, run_id: str, type_: str, state, **kw) -> None:
        self.store.add_event(run_id, type_, state=str(state) if state else None, **kw)

    def _transition(self, run_id: str, new: RunState, message: str = "", **fields) -> None:
        run = self.store.get_run(run_id)
        if run["state"] == new and new not in (RunState.RUNNING, RunState.VERIFYING):
            return
        if run["state"] != new:
            check_transition(run["state"], new)
        self.store.update_run(run_id, state=new.value, **fields)
        if run["state"] != new:
            self._event(
                run_id,
                "STATE",
                new,
                message=message or f"{run['state']} -> {new}",
                detail={"from": run["state"], "to": new.value},
            )

    def _fail(self, run_id: str, message: str, trace: str = "") -> None:
        run = self.store.get_run(run_id)
        if run and run["state"] not in TERMINAL:
            self.store.update_run(run_id, state=RunState.FAILED.value, error=message)
            self._event(
                run_id,
                "ERROR",
                RunState.FAILED,
                result="FAILED",
                message=message,
                detail={"trace": trace[-1500:]} if trace else {},
            )

    def _load_brain(self, run: dict):
        brain = self.brain_factory()
        brain.load_state(run["brain_state"], self.gateway.preview)
        return brain

    def _checkpoint(self, run_id: str, brain, run: dict, **extra) -> None:
        self.store.update_run(
            run_id, brain_state=brain.export_state(), plan=brain.plan(), usage=run["usage"], step_count=run["step_count"], **extra
        )

    # ------------------------------------------------------------------ drivers
    def _drive_new(self, run_id: str) -> None:
        run = self._require(run_id)
        self._transition(run_id, RunState.PLANNING, "Analyzing the assignment and discovering capabilities")
        brain = self.brain_factory()
        caps = self.gateway.toolbox.capabilities()
        brain.start(run["assignment"], caps, self.gateway.preview)
        self._flush_notes(run_id, brain, RunState.PLANNING, run)
        run = self._require(run_id)
        self._checkpoint(run_id, brain, run)
        self._event(run_id, "PLAN", RunState.PLANNING, message="Initial execution plan", detail={"plan": brain.plan()})
        self._transition(run_id, RunState.RUNNING, "Executing plan")
        self._loop(run_id, brain)

    def _drive_existing(self, run_id: str) -> None:
        run = self._require(run_id)
        if not run.get("brain_state"):
            # interrupted before the first checkpoint - plan again from the start (nothing was executed)
            brain = self.brain_factory()
            brain.start(run["assignment"], self.gateway.toolbox.capabilities(), self.gateway.preview)
            self._checkpoint(run_id, brain, run)
        else:
            brain = self._load_brain(run)
        if run["state"] in (RunState.PLANNING, RunState.RESUMING):
            self._transition(run_id, RunState.RUNNING, "Continuing from checkpoint")
        if run.get("pending_call") and run["state"] != RunState.WAITING_APPROVAL:
            # a call was issued before the interruption but its result was never observed
            self._execute_and_observe(run_id, brain, ToolCall.from_dict(run["pending_call"]))
        self._loop(run_id, brain)

    def _drive_paused(self, run_id: str) -> None:
        run = self._require(run_id)
        brain = self._load_brain(run)
        self._transition(run_id, RunState.RUNNING, "Resumed by operator")
        self._loop(run_id, brain)

    def _drive_resume(self, run_id: str) -> None:
        run = self._require(run_id)
        if run["state"] != RunState.WAITING_APPROVAL:
            return
        if run["cancel_requested"]:
            return
        approval = self.store.get_approval(run["pending_approval_id"])
        if approval["status"] == "PENDING":
            return  # nothing to resume - approval must be explicit
        self._transition(
            run_id, RunState.RESUMING, f"Human decision received: {approval['status']} by {approval.get('decided_by')}"
        )
        brain = self._load_brain(run)
        call = ToolCall.from_dict(run["pending_call"])
        self.store.update_run(run_id, pending_approval_id=None)
        outcome = self._execute_and_observe(run_id, brain, call)
        if outcome is None:
            return
        run = self._require(run_id)
        if run["state"] == RunState.RESUMING:
            self._transition(run_id, RunState.RUNNING, "Continuing autonomous execution after the checkpoint")
        self._loop(run_id, brain)

    def _execute_and_observe(self, run_id: str, brain, call: ToolCall):
        run = self._require(run_id)
        run["step_count"] += 1
        self.store.update_run(run_id, pending_call=call.to_dict(), current_step=f"{call.action}", step_count=run["step_count"])
        outcome = self.gateway.execute(
            run, run["step_count"], call.action, call.params, method=call.method, rationale=call.rationale, context=call.context
        )
        self.store.update_run(run_id, usage=run["usage"])

        if outcome.status == "approval_required":
            approval = self.store.get_approval(outcome.approval_id)
            self._checkpoint(run_id, brain, run, pending_call=call.to_dict(), pending_approval_id=outcome.approval_id)
            self._event(
                run_id,
                "APPROVAL_REQUESTED",
                RunState.WAITING_APPROVAL,
                step=run["step_count"],
                action=call.action,
                result="WAITING FOR HUMAN APPROVAL",
                policy="REQUIRE_APPROVAL",
                message=f"WAITING FOR HUMAN APPROVAL: {call.action} {approval['context'].get('amount', '')} {approval['context'].get('currency', '')} ({approval['id']})",
                detail={"approval": approval["context"], "approval_id": approval["id"], "expires_at": approval["expires_at"]},
            )
            self._transition(run_id, RunState.WAITING_APPROVAL, "Paused at mandatory approval checkpoint")
            return None
        if outcome.status == "uncertain":
            self._checkpoint(run_id, brain, run)
            self._transition(run_id, RunState.BLOCKED, f"Blocked: {outcome.message}", error=outcome.message)
            return None

        if outcome.approval_id:
            approval = self.store.get_approval(outcome.approval_id)
            self._event(
                run_id,
                "APPROVAL_OUTCOME",
                self._require(run_id)["state"],
                step=run["step_count"],
                action=call.action,
                result=outcome.status.upper(),
                message=outcome.message,
                detail={
                    "approval_id": outcome.approval_id,
                    "decided_by": approval.get("decided_by"),
                    "status": approval["status"],
                },
            )
        brain.observe(call, outcome.for_agent())
        self._checkpoint(run_id, brain, run, pending_call=None)
        return outcome

    def _flush_notes(self, run_id: str, brain, state, run: dict) -> None:
        for n in brain.drain_notes():
            self._event(
                run_id,
                n["kind"],
                state,
                message=n["message"],
                detail=n.get("detail", {}),
                policy=(n.get("detail", {}).get("decision") or {}).get("decision") if n["kind"] == "POLICY_PREVIEW" else None,
            )
        for u in brain.drain_model_usage():
            cost = add_model_usage(run["usage"], self.settings, u.get("input_tokens"), u.get("output_tokens"))
            self._event(
                run_id,
                "MODEL_CALL",
                state,
                method="MODEL",
                result=u.get("stop_reason"),
                duration_ms=u.get("duration_ms"),
                cost_usd=round(cost, 6),
                message=f"Claude call: {u.get('input_tokens')} in / {u.get('output_tokens')} out tokens",
                detail=u,
            )

    def _loop(self, run_id: str, brain) -> None:
        while True:
            run = self._require(run_id)
            if run["state"] in TERMINAL or run["state"] == RunState.WAITING_APPROVAL:
                return
            if run["cancel_requested"]:
                self._checkpoint(run_id, brain, run)
                self._transition(run_id, RunState.CANCELLED, "Cancelled by operator between steps")
                return
            if run["pause_requested"]:
                self._checkpoint(run_id, brain, run)
                self._transition(run_id, RunState.PAUSED, "Paused by operator between steps")
                return
            elapsed = (datetime.now(UTC) - parse_iso(run["created_at"])).total_seconds()
            try:
                check_limits(run["usage"], run["step_count"], elapsed, self.settings.limits)
            except LimitExceeded as exc:
                self._checkpoint(run_id, brain, run)
                self._transition(
                    run_id, RunState.BLOCKED, f"Execution limit reached: {exc}. Operator intervention required.", error=str(exc)
                )
                return

            try:
                decision = brain.next_action()
            except Exception as exc:  # noqa: BLE001 - model/service errors become a clear failure
                self._flush_notes(run_id, brain, run["state"], run)
                self.store.update_run(run_id, usage=run["usage"])
                self._fail(run_id, f"agent decision failed: {_explain(exc)}")
                return
            self._flush_notes(run_id, brain, run["state"], run)
            self.store.update_run(run_id, usage=run["usage"], plan=brain.plan())

            if isinstance(decision, Finish):
                self._finish(run_id, brain, decision)
                return
            if decision.phase == "verification" and run["state"] == RunState.RUNNING:
                self._transition(run_id, RunState.VERIFYING, "Verifying final record state")
            if self._execute_and_observe(run_id, brain, decision) is None:
                return
            self._flush_notes(run_id, brain, self._require(run_id)["state"], run)

    def _finish(self, run_id: str, brain, decision: Finish) -> None:
        run = self._require(run_id)
        if run["state"] == RunState.RUNNING:
            self._transition(run_id, RunState.VERIFYING, "Final verification")
        approvals = self.store.list_approvals(run_id=run_id)
        rejected = [a for a in approvals if a["status"] == "REJECTED"]
        void = [a for a in approvals if a["status"] in ("INVALIDATED", "EXPIRED", "EXECUTION_UNCERTAIN")]
        duration = (datetime.now(UTC) - parse_iso(run["created_at"])).total_seconds()
        result = {
            "verified": decision.verified,
            "headline": decision.headline,
            "report": decision.report,
            "approvals": [
                {k: a[k] for k in ("id", "action", "status", "decided_by", "decided_at", "status_reason")} for a in approvals
            ],
            "duration_seconds": round(duration, 1),
            "usage": run["usage"],
        }
        self._checkpoint(run_id, brain, run, result=result, current_step="done")
        self._event(
            run_id,
            "VERIFICATION",
            RunState.VERIFYING,
            result="PASSED" if decision.verified else "FAILED",
            message=decision.headline,
            detail={"checks": decision.report.get("checks", [])},
        )
        if not decision.verified:
            self._transition(run_id, RunState.FAILED, f"Verification failed: {decision.headline}", error=decision.headline)
        elif void:
            self._transition(
                run_id,
                RunState.BLOCKED,
                "Completed safe work; a financial approval was invalidated/expired and the action was not executed",
            )
        elif rejected:
            self._transition(
                run_id, RunState.REJECTED, "Completed; the approver rejected the financial action, which remains unexecuted"
            )
        else:
            self._transition(run_id, RunState.COMPLETED, "All authorized work executed and verified")
        self._event(
            run_id,
            "SUMMARY",
            self._require(run_id)["state"],
            message=decision.headline,
            duration_ms=int(duration * 1000),
            cost_usd=run["usage"].get("estimated_model_cost_usd"),
            detail=result,
        )


def _explain(exc: Exception) -> str:
    name = type(exc).__name__
    try:
        import anthropic

        if isinstance(exc, anthropic.AuthenticationError):
            return "Claude service rejected the credentials (check ANTHROPIC_API_KEY)"
        if isinstance(exc, anthropic.PermissionDeniedError):
            return "Claude API key lacks permission for this model"
        if isinstance(exc, anthropic.NotFoundError):
            return "configured Claude model not found (check CLAUDE_MODEL)"
        if isinstance(exc, anthropic.RateLimitError):
            return "Claude rate limit reached; retry later"
        if isinstance(exc, anthropic.APIConnectionError):
            return "Claude service unreachable (network/connectivity)"
        if isinstance(exc, anthropic.APIStatusError):
            return f"Claude API error {exc.status_code}"
    except ImportError:  # pragma: no cover
        pass
    if "api_key" in str(exc).lower() or "auth" in str(exc).lower():
        return "Claude credentials are missing - set ANTHROPIC_API_KEY or use Demo Mode"
    return f"{name}: {str(exc)[:300]}"


def wait_for_state(store: Store, run_id: str, states: set[str], timeout: float = 60.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = store.get_run(run_id)
        if run and run["state"] in states:
            return run
        time.sleep(0.1)
    raise TimeoutError(f"run {run_id} did not reach {states}; last state {store.get_run(run_id)['state']}")
