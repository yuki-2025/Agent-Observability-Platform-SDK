"""Smoke tests — package imports + basic API surface."""
import pytest


def test_top_level_imports():
    import propio_obs

    # Verb layer present
    for attr in (
        "init_agent",
        "start_request",
        "record_tool",
        "record_quality",
        "record_voice_event",
        "finish_request",
        "flush",
        "wrap_llm_client",
        "pcm16_to_wav",
        "Request",
    ):
        assert hasattr(propio_obs, attr), f"missing top-level export: {attr}"


def test_back_compat_imports():
    """Drop-in compat with backend/app/services/tracing.py."""
    import propio_obs

    for attr in ("record_stt", "record_tts", "turn_trace", "wrap_openai", "pcm16_to_wav"):
        assert hasattr(propio_obs, attr), f"missing back-compat export: {attr}"


def test_pcm16_to_wav_produces_valid_wav():
    from propio_obs import pcm16_to_wav

    pcm = b"\x00\x01" * 16000  # 1 sec @ 16 kHz
    wav = pcm16_to_wav(pcm, sample_rate=16000)
    # WAV header: 'RIFF' .... 'WAVE'
    assert wav[:4] == b"RIFF"
    assert wav[8:12] == b"WAVE"
    assert len(wav) == 32044  # 32000 PCM + 44 header


def test_config_loads_from_dict():
    from propio_obs.config import AgentConfig

    cfg = AgentConfig.load(
        {
            "agent": {
                "agent_id": "test_agent",
                "service": "test-svc",
                "environment": "dev",
            },
            "backends": {"langsmith": {"enabled": False}},
        }
    )
    assert cfg.agent.agent_id == "test_agent"
    assert cfg.agent.environment == "dev"
    assert cfg.backends.langsmith.enabled is False


def test_config_environment_required(monkeypatch):
    """No agent.environment + no PROPIO_ENV → loader raises."""
    from propio_obs.config import AgentConfig

    monkeypatch.delenv("PROPIO_ENV", raising=False)
    with pytest.raises(ValueError, match="environment is required"):
        AgentConfig.load(
            {"agent": {"agent_id": "x", "service": "y"}, "backends": {}}
        )


def test_config_environment_falls_back_to_env_var(monkeypatch):
    """agent.environment unset → PROPIO_ENV env var picks it up."""
    from propio_obs.config import AgentConfig

    monkeypatch.setenv("PROPIO_ENV", "staging")
    cfg = AgentConfig.load(
        {"agent": {"agent_id": "x", "service": "y"}, "backends": {}}
    )
    assert cfg.agent.environment == "staging"


def test_config_invalid_environment_rejected(monkeypatch):
    """pydantic catches bad literal values at parse time."""
    from propio_obs.config import AgentConfig

    monkeypatch.delenv("PROPIO_ENV", raising=False)
    with pytest.raises(ValueError, match="dev.*qa.*staging.*prod"):
        AgentConfig.load(
            {
                "agent": {"agent_id": "x", "service": "y", "environment": "production"},
                "backends": {},
            }
        )


def test_config_langsmith_project_falls_back_to_agent_id_env(monkeypatch):
    """Unset project + no env var → {agent_id}-{environment} so prod/dev
    are isolated LangSmith projects."""
    from propio_obs.config import AgentConfig

    monkeypatch.delenv("LANGSMITH_PROJECT", raising=False)
    cfg = AgentConfig.load(
        {
            "agent": {"agent_id": "support_voice", "service": "svc", "environment": "prod"},
            "backends": {"langsmith": {"enabled": True}},
        }
    )
    assert cfg.backends.langsmith.project == "support_voice-prod"


def test_config_langsmith_project_env_var_wins_over_fallback(monkeypatch):
    from propio_obs.config import AgentConfig

    monkeypatch.setenv("LANGSMITH_PROJECT", "from-env")
    cfg = AgentConfig.load(
        {
            "agent": {"agent_id": "agent1", "service": "svc", "environment": "prod"},
            "backends": {"langsmith": {"enabled": True}},
        }
    )
    assert cfg.backends.langsmith.project == "from-env"


def test_config_langsmith_project_explicit_wins_over_env(monkeypatch):
    from propio_obs.config import AgentConfig

    monkeypatch.setenv("LANGSMITH_PROJECT", "from-env")
    cfg = AgentConfig.load(
        {
            "agent": {"agent_id": "agent1", "service": "svc", "environment": "prod"},
            "backends": {"langsmith": {"enabled": True, "project": "explicit"}},
        }
    )
    assert cfg.backends.langsmith.project == "explicit"


def test_config_postgres_db_url_env_resolves_from_environment():
    """url_env unset → per-env table from platform_defaults."""
    from propio_obs.config import AgentConfig

    cfg = AgentConfig.load(
        {
            "agent": {"agent_id": "a", "service": "s", "environment": "qa"},
            "backends": {"postgres_db": {"enabled": True}},
        }
    )
    assert cfg.backends.postgres_db.url_env == "POSTGRES_DB_URL_QA"


def test_config_postgres_db_enabled_by_default():
    """postgres_db is the catch-all default backend — every agent gets it
    unless explicitly opted out."""
    from propio_obs.config import AgentConfig

    cfg = AgentConfig.load(
        {
            "agent": {"agent_id": "a", "service": "s", "environment": "dev"},
            # no backends declared at all
        }
    )
    assert cfg.backends.postgres_db.enabled is True
    assert cfg.backends.postgres_db.url_env == "POSTGRES_DB_URL_DEV"
    # LangSmith remains off by default (external SaaS).
    assert cfg.backends.langsmith.enabled is False


def test_config_postgres_db_explicit_disable_wins():
    from propio_obs.config import AgentConfig

    cfg = AgentConfig.load(
        {
            "agent": {"agent_id": "a", "service": "s", "environment": "dev"},
            "backends": {"postgres_db": {"enabled": False}},
        }
    )
    assert cfg.backends.postgres_db.enabled is False


def test_config_platform_defaults_baked_in():
    """Pydantic defaults pull from propio_obs.platform_defaults — agents should
    never need to pass endpoint / api_key_env in their inline dict."""
    from propio_obs.config import AgentConfig
    from propio_obs import platform_defaults as pd

    cfg = AgentConfig.load(
        {
            "agent": {"agent_id": "a", "service": "s", "environment": "dev"},
            "backends": {"langsmith": {"enabled": True}},
        }
    )
    assert cfg.backends.langsmith.endpoint == pd.LANGSMITH_ENDPOINT
    assert cfg.backends.langsmith.api_key_env == pd.LANGSMITH_API_KEY_ENV


def test_config_otel_default_collector_endpoint():
    """OTel section defaults to localhost:4318 for local Collector."""
    from propio_obs.config import AgentConfig

    cfg = AgentConfig.load(
        {"agent": {"agent_id": "a", "service": "s", "environment": "dev"}}
    )
    assert cfg.otel.collector_endpoint == "http://localhost:4318"


def test_request_handle():
    from propio_obs import Request

    r = Request(request_id="abc", request_type="chat", session_id="s1")
    assert r.request_id == "abc"
    assert r.session_id == "s1"
    assert r.metadata == {}


# ──────────────────────────────────────────────────────────────
# OTel verb-layer tests
# ──────────────────────────────────────────────────────────────

def _setup_in_memory_tracer(monkeypatch):
    """Helper: install an in-memory OTel exporter on the SDK's tracer slot.
    Returns the exporter so tests can pull captured spans."""
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    from propio_obs import otel_init
    from propio_obs.exporters import langsmith as ls

    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")

    monkeypatch.setattr(otel_init, "_tracer", tracer)
    monkeypatch.setattr(otel_init, "ENABLED", True)
    monkeypatch.setattr(ls, "ENABLED", True)  # enables langsmith decoration
    return exporter


def test_verb_layer_emits_otel_spans(monkeypatch):
    """start_request → record_voice_event → record_tool → finish_request emits
    one parent OTel span with two children; correlation attributes carry through."""
    from propio_obs import api as obs_api

    exporter = _setup_in_memory_tracer(monkeypatch)

    req = obs_api.start_request(
        "voice_turn",
        session_id="sess-1",
        inputs={"transcript": "hi"},
        metadata={"tenant_id": "hospital_a"},
    )
    obs_api.record_voice_event(
        req, "asr_finalized",
        metrics={"asr_latency_ms": 280},
        metadata={"provider": "deepgram"},
    )
    obs_api.record_tool(req, "my_tool", input={"q": 1}, output={"r": 2})
    obs_api.finish_request(req, status="success", outputs={"final": "done"})

    spans = exporter.get_finished_spans()
    names = [s.name for s in spans]
    assert "agent.request" in names
    assert "voice.asr_finalized" in names
    assert "tool.my_tool" in names

    parent = next(s for s in spans if s.name == "agent.request")
    # Correlation keys present on parent.
    assert parent.attributes.get("session.id") == "sess-1"
    assert parent.attributes.get("tenant.id") == "hospital_a"
    assert parent.attributes.get("request.type") == "voice_turn"
    assert parent.attributes.get("status") == "success"
    assert parent.attributes.get("output.final") == "done"
    # LangSmith conventions stamped.
    assert parent.attributes.get("langsmith.span.kind") == "chain"
    assert parent.attributes.get("langsmith.metadata.session_id") == "sess-1"

    voice_span = next(s for s in spans if s.name == "voice.asr_finalized")
    assert voice_span.attributes.get("voice.event") == "asr_finalized"
    assert voice_span.attributes.get("voice.metrics.asr_latency_ms") == 280
    assert voice_span.attributes.get("langsmith.span.kind") == "tool"
    # Nests under the request span.
    assert voice_span.parent.span_id == parent.context.span_id

    tool_span = next(s for s in spans if s.name == "tool.my_tool")
    assert tool_span.parent.span_id == parent.context.span_id


def test_verb_layer_otel_disabled_is_passive():
    """When otel_init.setup() hasn't been called, all verbs are silent no-ops."""
    from propio_obs import api as obs_api
    from propio_obs import otel_init

    # Belt + suspenders: ensure tracer slot is None.
    assert otel_init.get_tracer() is None or not otel_init.ENABLED

    req = obs_api.start_request("voice_turn", session_id="s1")
    assert "span" not in req._state  # no OTel span was created

    # These must not raise.
    obs_api.record_voice_event(req, "asr_finalized", audio_wav=b"\x00\x01" * 1000)
    obs_api.record_tool(req, "noop", input={}, output={})
    obs_api.finish_request(req, status="success")


def test_audio_wav_drops_with_warning(monkeypatch, caplog):
    """OTel migration loses LangSmith audio attachments. First time
    record_voice_event sees audio_wav we log one warning, then silently
    drop subsequent ones."""
    from propio_obs import api as obs_api
    from propio_obs.exporters import langsmith as ls

    # Reset the one-time flag in case other tests set it.
    monkeypatch.setattr(ls, "_audio_warning_fired", False)
    monkeypatch.setattr(ls, "ENABLED", True)
    _setup_in_memory_tracer(monkeypatch)

    req = obs_api.start_request("voice_turn", session_id="s1")
    with caplog.at_level("WARNING", logger="propio_obs.exporters.langsmith"):
        obs_api.record_voice_event(req, "asr_finalized", audio_wav=b"\x00\x01" * 100)
    obs_api.finish_request(req)

    # At least one warning mentions dropped audio.
    assert any("OTel migration dropped LangSmith attachments" in r.message for r in caplog.records)


# ──────────────────────────────────────────────────────────────
# Datadog config (post-migration: backend config is mostly toggle)
# ──────────────────────────────────────────────────────────────

def test_datadog_backend_pydantic_defaults():
    from propio_obs.config import AgentConfig
    from propio_obs import platform_defaults as pd

    cfg = AgentConfig.load({
        "agent": {"agent_id": "a", "service": "svc", "environment": "prod"},
        "backends": {"datadog": {"enabled": True}},
    })
    assert cfg.backends.datadog.api_key_env == pd.DATADOG_API_KEY_ENV
    assert cfg.backends.datadog.site == pd.DATADOG_SITE
    # service / env_tag / version fall back to agent.*
    assert cfg.backends.datadog.service == "svc"
    assert cfg.backends.datadog.env_tag == "prod"


def test_datadog_logs_backend_pydantic_defaults():
    """Logs backend reuses APM's DD_API_KEY env / site, defaults disabled."""
    from propio_obs.config import AgentConfig
    from propio_obs import platform_defaults as pd

    cfg = AgentConfig.load({
        "agent": {"agent_id": "a", "service": "svc", "environment": "prod"},
        "backends": {"datadog_logs": {"enabled": True}},
    })
    assert cfg.backends.datadog_logs.api_key_env == pd.DATADOG_API_KEY_ENV
    assert cfg.backends.datadog_logs.site == pd.DATADOG_SITE
    # Fall through to agent.* (same pattern as APM backend).
    assert cfg.backends.datadog_logs.service == "svc"
    assert cfg.backends.datadog_logs.env_tag == "prod"
    # Default min_level / exclude_loggers.
    assert cfg.backends.datadog_logs.min_level == "DEBUG"
    assert "ddtrace" in cfg.backends.datadog_logs.exclude_loggers


def test_datadog_logs_default_off():
    """Unlike postgres_db (default-on Propio infra), DD Logs is external SaaS
    and must be explicitly opted in."""
    from propio_obs.config import AgentConfig

    cfg = AgentConfig.load({
        "agent": {"agent_id": "a", "service": "s", "environment": "dev"},
    })
    assert cfg.backends.datadog_logs.enabled is False


# ──────────────────────────────────────────────────────────────
# Postgres event-mirror (asyncpg, unchanged by OTel migration)
# ──────────────────────────────────────────────────────────────

def test_postgres_db_verbs_exported():
    """New session/event verbs must be importable from the top-level package."""
    import propio_obs

    for attr in ("start_session", "end_session", "broadcast_event"):
        assert hasattr(propio_obs, attr), f"missing export: {attr}"


def test_postgres_db_disabled_is_noop():
    """When postgres_db is disabled (no URL), verbs are silent no-ops."""
    import asyncio

    from propio_obs.exporters.postgres_db import PostgresDBExporter

    exp = PostgresDBExporter()
    assert exp.enabled is False  # default

    async def _run():
        await exp.start_session("s1", config={}, env="dev", agent_id="a")
        await exp.broadcast_event({"type": "test"}, session_id="s1", agent_id="a")
        await exp.end_session("s1")

    asyncio.run(_run())  # must not raise


def _install_fake_asyncpg(monkeypatch, captured):
    """Shared fixture: install a fake asyncpg pool that records executed SQL."""
    from propio_obs.exporters import postgres_db as pg_mod

    class FakeConn:
        async def execute(self, sql, *args):
            captured.append((sql, args))

    class FakeAcquire:
        def __init__(self, conn): self.conn = conn
        async def __aenter__(self): return self.conn
        async def __aexit__(self, *exc): return False

    class FakePool:
        def __init__(self): self.conn = FakeConn()
        def acquire(self): return FakeAcquire(self.conn)
        async def close(self): pass

    class FakeAsyncpg:
        @staticmethod
        async def create_pool(*args, **kwargs): return FakePool()

    monkeypatch.setattr(pg_mod, "_asyncpg", FakeAsyncpg)
    return pg_mod


def test_postgres_db_broadcast_emits_expected_sql(monkeypatch):
    """When enabled with a mocked asyncpg pool, broadcast_event INSERTs into
    logs with agent_id + fires pg_notify."""
    import asyncio

    captured: list = []
    pg_mod = _install_fake_asyncpg(monkeypatch, captured)

    exp = pg_mod.PostgresDBExporter()
    exp.enabled = True
    exp.url = "postgresql://test"

    async def _run():
        await exp.broadcast_event(
            {"type": "user_transcript", "text": "hello"},
            session_id="sess-1",
            agent_id="propio_agent_pro",
        )
        # broadcast is fire-and-forget — drain pending tasks before assert
        if exp._pending_writes:
            await asyncio.wait(exp._pending_writes)

    asyncio.run(_run())

    assert len(captured) == 1
    sql, args = captured[0]
    assert "INSERT INTO logs" in sql
    assert "pg_notify('monitor_events'" in sql
    assert "agent_id" in sql
    # Args: (session_id, ts, event_type, payload, agent_id)
    assert args[0] == "sess-1"
    assert args[2] == "user_transcript"
    assert args[4] == "propio_agent_pro"


def test_postgres_db_start_session_inserts_with_agent_id(monkeypatch):
    """start_session INSERTs the sessions row with agent_id populated."""
    import asyncio

    captured: list = []
    pg_mod = _install_fake_asyncpg(monkeypatch, captured)

    exp = pg_mod.PostgresDBExporter()
    exp.enabled = True
    exp.url = "postgresql://test"

    async def _run():
        await exp.start_session(
            "sess-1",
            config={"foo": "bar"},
            env="dev",
            agent_id="propio_agent_pro",
        )
        await exp.end_session("sess-1")

    asyncio.run(_run())

    assert len(captured) >= 2
    start_sql, start_args = captured[0]
    assert "INSERT INTO sessions" in start_sql
    assert "agent_id" in start_sql
    # Args order from SQL: ($1=id, $2=ts, $3=config_str, $4=env, $5=agent_id)
    assert start_args[0] == "sess-1"          # id
    assert start_args[3] == "dev"             # env
    assert start_args[4] == "propio_agent_pro"  # agent_id

    end_sql, _ = captured[1]
    assert "UPDATE sessions" in end_sql
