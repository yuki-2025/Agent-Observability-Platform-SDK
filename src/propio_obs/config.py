"""Agent configuration schema, loaded from observability.yml or an inline dict.

The schema is intentionally thin: agents declare their identity + which
backends are enabled. Platform-wide constants (endpoints, default env-var
names, region names) live in propio_obs.platform_defaults — agents never
duplicate them here.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Union

import yaml
from pydantic import BaseModel, Field

from . import platform_defaults as pd


class AgentSection(BaseModel):
    agent_id: str
    agent_name: Optional[str] = None
    agent_type: Literal["realtime_agent", "chat_agent", "tool_agent", "batch", "workflow"] = (
        "chat_agent"
    )
    modality: Literal["text", "voice", "multimodal"] = "text"
    service: str
    # environment is required. AgentConfig.load() falls back to the PROPIO_ENV
    # env var; if both are unset the loader raises. We model it Optional only
    # so pydantic doesn't reject the inline dict before the env-var fallback
    # runs.
    environment: Optional[pd.Environment] = None
    version: Optional[str] = None
    default_tags: Dict[str, str] = Field(default_factory=dict)


class LangSmithBackend(BaseModel):
    enabled: bool = False
    api_key_env: str = pd.LANGSMITH_API_KEY_ENV
    # project: None → SDK falls back to LANGSMITH_PROJECT env, then to agent.agent_id
    project: Optional[str] = None
    endpoint: str = pd.LANGSMITH_ENDPOINT


class DatadogBackend(BaseModel):
    enabled: bool = False
    api_key_env: str = pd.DATADOG_API_KEY_ENV
    site: str = pd.DATADOG_SITE
    # Override the DD Agent URL when there's no localhost:8126 agent (e.g.
    # routing through an OTel Collector, or pointing at a sidecar).
    agent_url: Optional[str] = None
    # When None, SDK fills from agent.service / agent.environment / agent.version
    # at runtime — agents should not need to duplicate those.
    service: Optional[str] = None
    env_tag: Optional[str] = None
    version: Optional[str] = None


class DatadogLogsBackend(BaseModel):
    """Datadog Logs exporter — independent of APM. Ships Python logger output
    via HTTPS to https://http-intake.logs.{site}/api/v2/logs."""

    enabled: bool = False
    api_key_env: str = pd.DATADOG_API_KEY_ENV    # reused from APM
    site: str = pd.DATADOG_SITE                   # reused from APM
    # None → fall through to agent.* at init_agent time
    service: Optional[str] = None
    env_tag: Optional[str] = None
    version: Optional[str] = None
    # Behavior tuning.
    min_level: str = "DEBUG"
    batch_size: int = 10
    flush_interval_seconds: float = 5.0
    exclude_loggers: List[str] = Field(
        default_factory=lambda: ["ddtrace", "urllib3", "datadog", "httpx"]
    )


class PostgresDBBackend(BaseModel):
    # Postgres events-mirror is the default catch-all backend — every agent
    # gets it unless explicitly opted out. (LangSmith / Datadog are external
    # SaaS, so they default off; Postgres is propio-owned infra so it defaults
    # on.)
    enabled: bool = True
    # url_env: None → SDK looks up agent.environment in the per-env table
    # (platform_defaults.POSTGRES_DB_URL_ENV_BY_ENV).
    url_env: Optional[str] = None


class BackendsSection(BaseModel):
    langsmith: LangSmithBackend = Field(default_factory=LangSmithBackend)
    datadog: DatadogBackend = Field(default_factory=DatadogBackend)
    datadog_logs: DatadogLogsBackend = Field(default_factory=DatadogLogsBackend)
    postgres_db: PostgresDBBackend = Field(default_factory=PostgresDBBackend)


class BehaviorSection(BaseModel):
    async_export: bool = True
    export_queue_size: int = 1000
    export_timeout_ms: int = 5000
    sampling: Dict[str, float] = Field(default_factory=lambda: {"llm_trace": 1.0})


class AgentConfig(BaseModel):
    agent: AgentSection
    quality_metrics: List[str] = Field(default_factory=list)
    voice_metrics: List[str] = Field(default_factory=list)
    backends: BackendsSection = Field(default_factory=BackendsSection)
    routing: Dict[str, List[str]] = Field(default_factory=dict)
    behavior: BehaviorSection = Field(default_factory=BehaviorSection)

    @classmethod
    def load(cls, source: Union[str, Path, Dict[str, Any]]) -> "AgentConfig":
        if isinstance(source, dict):
            cfg = cls(**source)
        else:
            path = Path(source)
            if not path.exists():
                raise FileNotFoundError(f"observability config not found: {path}")
            with path.open("r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            cfg = cls(**data)

        cfg._resolve_environment()
        cfg._resolve_langsmith_project()
        cfg._resolve_postgres_db_url_env()
        cfg._resolve_datadog_defaults()
        cfg._resolve_datadog_logs_defaults()
        return cfg

    # ─── Post-parse resolution ───────────────────────────────────
    def _resolve_environment(self) -> None:
        """environment is required. Falls back to PROPIO_ENV env var; raises
        if both are unset so prod data can't accidentally land in dev."""
        env = self.agent.environment or os.environ.get(pd.PROPIO_ENV_ENV_VAR)
        if not env:
            raise ValueError(
                "agent.environment is required (one of "
                f"{list(pd.ENVIRONMENTS)}). Pass it in the init_agent() dict "
                f"or set the {pd.PROPIO_ENV_ENV_VAR} env var."
            )
        if env not in pd.ENVIRONMENTS:
            raise ValueError(
                f"agent.environment must be one of {list(pd.ENVIRONMENTS)}, got {env!r}"
            )
        self.agent.environment = env  # type: ignore[assignment]

    def _resolve_langsmith_project(self) -> None:
        """project unset → LANGSMITH_PROJECT env → agent.agent_id."""
        if self.backends.langsmith.project:
            return
        env_project = os.environ.get(pd.LANGSMITH_PROJECT_ENV)
        self.backends.langsmith.project = env_project or self.agent.agent_id

    def _resolve_postgres_db_url_env(self) -> None:
        """url_env unset → per-env table lookup."""
        if self.backends.postgres_db.url_env:
            return
        env: pd.Environment = self.agent.environment  # type: ignore[assignment]
        self.backends.postgres_db.url_env = pd.POSTGRES_DB_URL_ENV_BY_ENV.get(env)

    def _resolve_datadog_defaults(self) -> None:
        """Datadog service / env / version fall back to the agent.* fields so
        every agent doesn't have to repeat them."""
        dd = self.backends.datadog
        if not dd.service:
            dd.service = self.agent.service
        if not dd.env_tag:
            dd.env_tag = self.agent.environment  # already resolved
        if not dd.version:
            dd.version = self.agent.version

    def _resolve_datadog_logs_defaults(self) -> None:
        """Same fallback chain as Datadog APM, applied to the Logs backend."""
        dl = self.backends.datadog_logs
        if not dl.service:
            dl.service = self.agent.service
        if not dl.env_tag:
            dl.env_tag = self.agent.environment
        if not dl.version:
            dl.version = self.agent.version

    def resolve_env(self, env_var: str) -> str:
        """Resolve an env var name to its value, returning empty string when unset."""
        return os.environ.get(env_var, "")
