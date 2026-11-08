"""Human approval manager.

Approvals are bound to the exact action parameters (SHA-256 of a canonical JSON encoding) and to a
record snapshot. They are decided only through an authenticated operator call; the agent has no
tool that can reach this code path.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime

from app.core.store import Store, now_iso, parse_iso

AGENT_IDENTITIES = {"agent", "autopilot", "autopilot agent", "system", "claude", "assistant", "model"}


class ApprovalError(Exception):
    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.status = status


def params_hash(action: str, method: str, params: dict) -> str:
    canonical = json.dumps({"action": action, "method": method, "params": params}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


class ApprovalManager:
    def __init__(self, store: Store, approver_key: str):
        self.store = store
        self._key = approver_key

    def expire_if_needed(self, approval: dict) -> dict:
        if approval["status"] in ("PENDING", "APPROVED") and parse_iso(approval["expires_at"]) < datetime.now(UTC):
            self.store.transition_approval(
                approval["id"], approval["status"], "EXPIRED", status_reason="approval expired before execution"
            )
            approval = self.store.get_approval(approval["id"])
        return approval

    def decide(self, approval_id: str, decision: str, approver: str, approver_key: str | None, comment: str = "") -> dict:
        if not approver_key or not self._key or not hmac.compare_digest(approver_key, self._key):
            raise ApprovalError("unauthorized: a valid approver key is required", 401)
        approver = (approver or "").strip()
        if not approver:
            raise ApprovalError("approver name is required", 422)
        if approver.lower() in AGENT_IDENTITIES:
            raise ApprovalError("the agent cannot approve its own requests", 403)
        decision = decision.upper()
        if decision not in ("APPROVE", "REJECT"):
            raise ApprovalError("decision must be APPROVE or REJECT", 422)

        approval = self.store.get_approval(approval_id)
        if not approval:
            raise ApprovalError("approval not found", 404)
        approval = self.expire_if_needed(approval)
        if approval["status"] == "EXPIRED":
            raise ApprovalError("approval has expired; the agent must request a new one", 410)
        if approval["status"] != "PENDING":
            raise ApprovalError(f"approval already {approval['status']}; duplicate decisions are not accepted", 409)

        new_status = "APPROVED" if decision == "APPROVE" else "REJECTED"
        ok = self.store.transition_approval(
            approval_id,
            "PENDING",
            new_status,
            decided_at=now_iso(),
            decided_by=approver[:80],
            decision_comment=(comment or "")[:500],
        )
        if not ok:  # lost a race with another decision
            raise ApprovalError("approval was decided concurrently", 409)
        return self.store.get_approval(approval_id)
