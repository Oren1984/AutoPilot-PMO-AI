"""Agent brain interface. A brain decides the next action from what it has observed; it never
executes anything itself. All execution goes through the ExecutionGateway."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Protocol


@dataclass
class ToolCall:
    action: str
    params: dict
    rationale: str = ""
    method: str | None = None
    phase: str = "execution"  # analysis | execution | verification
    context: dict = field(default_factory=dict)
    call_id: str | None = None  # model tool_use id (Claude brain)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> ToolCall:
        return cls(**d)


@dataclass
class Finish:
    verified: bool
    headline: str
    report: dict = field(default_factory=dict)


class Brain(Protocol):
    kind: str
    simulated: bool

    def start(self, assignment: str, capabilities: list, preview) -> None: ...

    def next_action(self) -> ToolCall | Finish: ...

    def observe(self, call: ToolCall, outcome: dict) -> None: ...

    def export_state(self) -> dict: ...

    def load_state(self, state: dict, preview) -> None: ...

    def plan(self) -> list[dict]: ...

    def drain_notes(self) -> list[dict]: ...

    def drain_model_usage(self) -> list[dict]: ...


def note(kind: str, message: str, **detail: Any) -> dict:
    return {"kind": kind, "message": message, "detail": detail}
