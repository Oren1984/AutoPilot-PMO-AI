"""Run state machine. Transitions are enforced, not advisory."""

from __future__ import annotations

from enum import StrEnum


class RunState(StrEnum):
    CREATED = "CREATED"
    PLANNING = "PLANNING"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    RESUMING = "RESUMING"
    VERIFYING = "VERIFYING"
    COMPLETED = "COMPLETED"
    REJECTED = "REJECTED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


TERMINAL = {RunState.COMPLETED, RunState.REJECTED, RunState.BLOCKED, RunState.FAILED, RunState.CANCELLED}
ACTIVE = {RunState.PLANNING, RunState.RUNNING, RunState.RESUMING, RunState.VERIFYING}

_ALLOWED: dict[RunState, set[RunState]] = {
    RunState.CREATED: {RunState.PLANNING, RunState.CANCELLED, RunState.FAILED},
    RunState.PLANNING: {RunState.RUNNING, RunState.FAILED, RunState.CANCELLED, RunState.BLOCKED, RunState.PAUSED},
    RunState.RUNNING: {
        RunState.RUNNING,
        RunState.WAITING_APPROVAL,
        RunState.VERIFYING,
        RunState.PAUSED,
        RunState.FAILED,
        RunState.BLOCKED,
        RunState.CANCELLED,
    },
    RunState.PAUSED: {RunState.RUNNING, RunState.CANCELLED, RunState.FAILED},
    RunState.WAITING_APPROVAL: {RunState.RESUMING, RunState.CANCELLED, RunState.FAILED, RunState.BLOCKED},
    RunState.RESUMING: {
        RunState.RUNNING,
        RunState.VERIFYING,
        RunState.WAITING_APPROVAL,
        RunState.FAILED,
        RunState.BLOCKED,
        RunState.CANCELLED,
    },
    RunState.VERIFYING: {
        RunState.VERIFYING,
        RunState.COMPLETED,
        RunState.REJECTED,
        RunState.FAILED,
        RunState.BLOCKED,
        RunState.CANCELLED,
        RunState.PAUSED,
        RunState.WAITING_APPROVAL,
        RunState.RUNNING,
    },
}


class InvalidTransition(RuntimeError):
    pass


def check_transition(current: RunState | str, new: RunState | str) -> RunState:
    cur, nxt = RunState(current), RunState(new)
    if cur in TERMINAL:
        raise InvalidTransition(f"run is terminal ({cur}); cannot move to {nxt}")
    if nxt not in _ALLOWED.get(cur, set()):
        raise InvalidTransition(f"illegal transition {cur} -> {nxt}")
    return nxt


# Labels shown in the operator UI
LABELS = {
    RunState.CREATED: "Created",
    RunState.PLANNING: "Planning",
    RunState.RUNNING: "Running",
    RunState.PAUSED: "Paused",
    RunState.WAITING_APPROVAL: "Awaiting Approval",
    RunState.RESUMING: "Resuming",
    RunState.VERIFYING: "Verifying",
    RunState.COMPLETED: "Completed",
    RunState.REJECTED: "Completed — Approval Rejected",
    RunState.BLOCKED: "Blocked",
    RunState.FAILED: "Failed",
    RunState.CANCELLED: "Cancelled",
}
