"""Composition root: builds the object graph from Settings (simple dependency injection)."""

from __future__ import annotations

from dataclasses import dataclass

from app.adapters.demo_api import DemoApiAdapter
from app.adapters.demo_ui import DemoUiAdapter
from app.adapters.toolbox import Toolbox
from app.agent.claude_agent import ClaudeAgent
from app.agent.runtime import AgentRuntime
from app.agent.simulated import SimulatedAgent
from app.config import Settings
from app.core.store import Store
from app.policies.engine import PolicyEngine
from app.workflows.approvals import ApprovalManager
from app.workflows.gateway import ExecutionGateway


@dataclass
class Container:
    settings: Settings
    store: Store
    policy: PolicyEngine
    toolbox: Toolbox
    approvals: ApprovalManager
    gateway: ExecutionGateway
    runtime: AgentRuntime


def build_toolbox(settings: Settings) -> Toolbox:
    if settings.enterprise == "salesforce":
        from app.adapters.salesforce import SalesforceAdapter

        api = SalesforceAdapter(
            settings.salesforce_instance_url,
            settings.salesforce_client_id,
            settings.salesforce_client_secret,
            settings.salesforce_api_version,
        )
        return Toolbox(api, None)  # UI automation against Salesforce is not part of V1.0
    api = DemoApiAdapter(settings.enterprise_base_url, settings.enterprise_api_token)
    ui = DemoUiAdapter(settings.ui_base_url, settings.enterprise_api_token, settings.screenshot_dir, headless=settings.headless)
    return Toolbox(api, ui)


def build_container(
    settings: Settings, toolbox: Toolbox | None = None, brain_factory=None, policy: PolicyEngine | None = None
) -> Container:
    store = Store(settings.db_path)
    policy = policy or PolicyEngine()
    toolbox = toolbox or build_toolbox(settings)
    approvals = ApprovalManager(store, settings.approver_api_key)
    gateway = ExecutionGateway(store, policy, toolbox, approvals, settings)
    if brain_factory is None:
        if settings.agent_brain == "claude":
            brain_factory = lambda: ClaudeAgent(settings)  # noqa: E731
        else:
            brain_factory = SimulatedAgent
    runtime = AgentRuntime(store, gateway, settings, brain_factory)
    return Container(settings, store, policy, toolbox, approvals, gateway, runtime)
