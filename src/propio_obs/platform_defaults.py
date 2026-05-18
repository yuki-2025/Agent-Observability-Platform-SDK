"""Propio platform conventions — values that are the same across all Propio agents.

These constants belong to the *platform*, not to any individual agent. Agents
should never duplicate them in their own config. Three layers of how Propio
observability config changes:

1. **Platform constants** (this module) — years between changes (LangSmith host
   migration, Datadog site change, region migration). Update: edit this file,
   bump SDK version, agents `uv sync` + restart.

2. **Env vars** — months between changes (API key rotation, DB instance swap).
   Update: change `.env` / k8s secret, restart agent. SDK is untouched.

3. **Per-agent overrides** — exceptions only (one agent uses self-hosted
   LangSmith, custom S3 bucket). Update: agent passes the field in its
   `init_agent({...})` dict, SDK is untouched.

Anything an agent *must* declare for itself (S3 bucket name, langsmith project
when not derivable) lives in agent config, not here.
"""
from __future__ import annotations

from typing import Dict, Literal


Environment = Literal["dev", "qa", "staging", "prod"]
ENVIRONMENTS: tuple[Environment, ...] = ("dev", "qa", "staging", "prod")


# ─── LangSmith ───────────────────────────────────────────────────
LANGSMITH_ENDPOINT: str = "https://api.smith.langchain.com"
LANGSMITH_API_KEY_ENV: str = "LANGSMITH_API_KEY"


# ─── Datadog (lands when v0.1 ships the adapter) ─────────────────
DATADOG_API_KEY_ENV: str = "DD_API_KEY"
DATADOG_SITE: str = "datadoghq.com"


# ─── Audio S3 (lands when v0.2 ships the adapter) ────────────────
AUDIO_S3_REGION: str = "us-east-1"
# bucket: per-agent — no default.


# ─── Per-env DB URL env var names ────────────────────────────────
# When a Postgres-backed backend (audio metadata index, internal events DB)
# has `url_env` unset, SDK looks the agent's `environment` up in these tables.
AUDIO_INDEX_PG_URL_ENV_BY_ENV: Dict[Environment, str] = {
    "dev":     "AUDIO_INDEX_PG_URL_DEV",
    "qa":      "AUDIO_INDEX_PG_URL_QA",
    "staging": "AUDIO_INDEX_PG_URL_STAGING",
    "prod":    "AUDIO_INDEX_PG_URL_PROD",
}

POSTGRES_DB_URL_ENV_BY_ENV: Dict[Environment, str] = {
    "dev":     "POSTGRES_DB_URL_DEV",
    "qa":      "POSTGRES_DB_URL_QA",
    "staging": "POSTGRES_DB_URL_STAGING",
    "prod":    "POSTGRES_DB_URL_PROD",
}


# ─── Env-var name an agent can use to declare its environment ────
# Read by AgentConfig.load() as the fallback when `agent.environment` is unset
# in the user-supplied dict.
PROPIO_ENV_ENV_VAR: str = "PROPIO_ENV"


# ─── Env-var name for the LangSmith project, when agent omits it ─
LANGSMITH_PROJECT_ENV: str = "LANGSMITH_PROJECT"
