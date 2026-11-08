"""Combines the enterprise adapters into one capability catalogue and routes each action to an
execution method (hybrid routing: API preferred, UI when the capability only exists in the UI)."""

from __future__ import annotations

from app.adapters.base import ActionSpec, AdapterError, AdapterResult


class Toolbox:
    def __init__(self, api_adapter, ui_adapter=None):
        self.api = api_adapter
        self.ui = ui_adapter
        self._specs: dict[str, ActionSpec] = {}
        for adapter in (api_adapter, ui_adapter):
            if adapter is None:
                continue
            for spec in adapter.capabilities():
                self._specs.setdefault(spec.name, spec)

    def capabilities(self) -> list[ActionSpec]:
        return list(self._specs.values())

    def spec(self, action: str) -> ActionSpec | None:
        return self._specs.get(action)

    def route(self, action: str, requested_method: str | None = None) -> str:
        """Pick the execution method. Prefer the structured API; use the UI only when the action is
        UI-only or the caller explicitly asks for UI. Policy decides afterwards whether that is allowed."""
        spec = self._specs.get(action)
        if spec is None:
            return requested_method or "api"
        if requested_method in ("api", "ui"):
            return requested_method
        return spec.method

    def execute(self, action: str, params: dict, method: str, context: dict) -> AdapterResult:
        adapter = self.ui if method == "ui" else self.api
        if adapter is None:
            raise AdapterError(f"no adapter available for method '{method}'")
        if method == "ui" and action not in {s.name for s in adapter.capabilities()}:
            raise AdapterError(f"action '{action}' has no UI implementation")
        return adapter.execute(action, params, context)

    def health(self) -> dict:
        out = {"api": self.api.health()}
        if self.ui is not None:
            out["ui"] = self.ui.health()
        return out

    # ---- record snapshots used to bind approvals and to re-validate before execution ----
    def financial_snapshot(self, action: str, params: dict) -> dict:
        if action != "execute_payment" or not hasattr(self.api, "fetch_purchase_request"):
            return {}
        pr = self.api.fetch_purchase_request(params["pr_id"])
        project = self.api.fetch_project(pr["project_id"])
        return {
            "pr_id": pr["id"],
            "version": pr["version"],
            "amount": pr["amount"],
            "currency": pr["currency"],
            "vendor": pr["vendor"],
            "status": pr["status"],
            "project_id": pr["project_id"],
            "project_name": project.get("name"),
            "justification": pr.get("justification"),
            "title": pr.get("title"),
            "recipient_account_masked": pr.get("recipient_account_masked"),
            "requested_by": pr.get("requested_by"),
            "budget_remaining": project.get("budget_remaining"),
        }
