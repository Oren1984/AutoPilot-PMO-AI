"""Environment-based configuration. No secrets are hard-coded here."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    value = os.environ.get(name, "").strip()
    return value if value else default


def _int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)))
    except ValueError:
        return default


@dataclass(frozen=True)
class Limits:
    max_steps: int = 40
    max_tool_calls: int = 60
    max_model_calls: int = 25
    max_cost_usd: float = 2.0
    run_timeout_seconds: int = 900
    approval_ttl_minutes: int = 30


@dataclass(frozen=True)
class Settings:
    # "demo" (deterministic simulated agent + local demo enterprise app)
    # or "connected" (Claude agent and/or Salesforce adapter)
    mode: str = "demo"
    # Which agent brain drives the run: "simulated" or "claude"
    agent_brain: str = "simulated"
    # Which enterprise target: "demo" or "salesforce"
    enterprise: str = "demo"

    data_dir: Path = Path("data")
    enterprise_base_url: str = "http://127.0.0.1:8100"
    enterprise_ui_base_url: str = ""  # defaults to enterprise_base_url
    enterprise_api_token: str = field(default="", repr=False)
    enterprise_admin_token: str = field(default="", repr=False)
    approver_api_key: str = field(default="", repr=False)

    claude_model: str = "claude-opus-5-5"
    claude_effort: str = "medium"
    claude_use_fallbacks: bool = True
    price_input_per_mtok: float = 0.0
    price_output_per_mtok: float = 0.0

    salesforce_instance_url: str = ""
    salesforce_client_id: str = field(default="", repr=False)
    salesforce_client_secret: str = field(default="", repr=False)
    salesforce_api_version: str = "v62.0"

    headless: bool = True
    retention_days: int = 30
    limits: Limits = field(default_factory=Limits)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "agent.db"

    @property
    def screenshot_dir(self) -> Path:
        return self.data_dir / "screenshots"

    @property
    def ui_base_url(self) -> str:
        return self.enterprise_ui_base_url or self.enterprise_base_url

    @property
    def has_anthropic_credentials(self) -> bool:
        return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))

    def public_view(self) -> dict:
        """Configuration safe to show in the UI (no secrets)."""
        return {
            "mode": self.mode,
            "agent_brain": self.agent_brain,
            "enterprise": self.enterprise,
            "enterprise_base_url": self.enterprise_base_url if self.enterprise == "demo" else "(salesforce)",
            "claude_model": self.claude_model if self.agent_brain == "claude" else None,
            "claude_credentials_present": self.has_anthropic_credentials,
            "salesforce_configured": bool(self.salesforce_instance_url and self.salesforce_client_id),
            "pricing_configured": self.price_input_per_mtok > 0 or self.price_output_per_mtok > 0,
            "limits": self.limits.__dict__,
            "retention_days": self.retention_days,
        }


def load_settings() -> Settings:
    mode = _env("APP_MODE", "demo").lower()
    brain = _env("AGENT_BRAIN", "simulated" if mode == "demo" else "claude").lower()
    enterprise = _env("ENTERPRISE_TARGET", "demo" if mode == "demo" else "salesforce").lower()
    model = _env("CLAUDE_MODEL", "claude-opus-5-5")
    # Published list prices (USD per million tokens) for the default model; override via env.
    default_prices = {"claude-opus-5-5": (4.0, 20.0), "claude-sonnet-5-5": (2.0, 10.0), "claude-haiku-5-5": (0.1, 0.5)}
    p_in, p_out = default_prices.get(model, (0.0, 0.0))
    return Settings(
        mode=mode,
        agent_brain=brain,
        enterprise=enterprise,
        data_dir=Path(_env("DATA_DIR", "data")),
        enterprise_base_url=_env("ENTERPRISE_BASE_URL", "http://127.0.0.1:8100").rstrip("/"),
        enterprise_ui_base_url=_env("ENTERPRISE_UI_BASE_URL", "").rstrip("/"),
        enterprise_api_token=_env("ENTERPRISE_API_TOKEN", "demo-service-token"),
        enterprise_admin_token=_env("ENTERPRISE_ADMIN_TOKEN", "demo-admin-token"),
        approver_api_key=_env("APPROVER_API_KEY", "demo-approver-key"),
        claude_model=model,
        claude_effort=_env("CLAUDE_EFFORT", "medium"),
        claude_use_fallbacks=_env("CLAUDE_USE_FALLBACKS", "true").lower() == "true",
        price_input_per_mtok=_float("MODEL_PRICE_INPUT_PER_MTOK", p_in),
        price_output_per_mtok=_float("MODEL_PRICE_OUTPUT_PER_MTOK", p_out),
        salesforce_instance_url=_env("SALESFORCE_INSTANCE_URL").rstrip("/"),
        salesforce_client_id=_env("SALESFORCE_CLIENT_ID"),
        salesforce_client_secret=_env("SALESFORCE_CLIENT_SECRET"),
        salesforce_api_version=_env("SALESFORCE_API_VERSION", "v62.0"),
        headless=_env("BROWSER_HEADLESS", "true").lower() != "false",
        retention_days=_int("AUDIT_RETENTION_DAYS", 30),
        limits=Limits(
            max_steps=_int("MAX_STEPS", 40),
            max_tool_calls=_int("MAX_TOOL_CALLS", 60),
            max_model_calls=_int("MAX_MODEL_CALLS", 25),
            max_cost_usd=_float("MAX_COST_USD", 2.0),
            run_timeout_seconds=_int("RUN_TIMEOUT_SECONDS", 900),
            approval_ttl_minutes=_int("APPROVAL_TTL_MINUTES", 30),
        ),
    )
