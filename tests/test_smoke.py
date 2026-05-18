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


def test_config_langsmith_project_falls_back_to_agent_id(monkeypatch):
    """Unset project + no env → agent.agent_id."""
    from propio_obs.config import AgentConfig

    monkeypatch.delenv("LANGSMITH_PROJECT", raising=False)
    cfg = AgentConfig.load(
        {
            "agent": {"agent_id": "support_voice", "service": "svc", "environment": "prod"},
            "backends": {"langsmith": {"enabled": True}},
        }
    )
    assert cfg.backends.langsmith.project == "support_voice"


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


def test_request_handle():
    from propio_obs import Request

    r = Request(request_id="abc", request_type="chat", session_id="s1")
    assert r.request_id == "abc"
    assert r.session_id == "s1"
    assert r.metadata == {}


def test_verb_layer_emits_parent_child_runs(monkeypatch):
    """start_request opens a parent RunTree; record_voice_event creates
    children on it; finish_request ends + patches the parent. Verified by
    monkey-patching the langsmith primitives."""
    from propio_obs import api as obs_api
    from propio_obs.exporters import langsmith as ls

    class FakeChild:
        def __init__(self, name, run_type, inputs):
            self.name = name
            self.run_type = run_type
            self.inputs = inputs
            self.attachments = {}
            self.outputs = None
            self.error = None
            self.posted = False
            self.patched = False
            self.metadata = {}

        def add_metadata(self, md):
            self.metadata.update(md)

        def end(self, outputs=None, error=None):
            self.outputs = outputs
            self.error = error

        def post(self):
            self.posted = True

        def patch(self):
            self.patched = True

    class FakeRunTree:
        instances = []

        def __init__(self, **data):
            self.name = data.get("name")
            self.run_type = data.get("run_type")
            self.inputs = data.get("inputs", {})
            self.extra = data.get("extra", {})
            self.children = []
            self.ended = False
            self.patched = False
            self.end_outputs = None
            self.end_error = None
            FakeRunTree.instances.append(self)

        def post(self):
            self.posted = True

        def create_child(self, name, run_type, inputs):
            c = FakeChild(name, run_type, inputs)
            self.children.append(c)
            return c

        def end(self, outputs=None, error=None):
            self.ended = True
            self.end_outputs = outputs
            self.end_error = error

        def patch(self):
            self.patched = True

    class FakeCM:
        def __init__(self, *, parent=None, project_name=None):
            self.parent = parent
            self.entered = False
            self.exited = False

        def __enter__(self):
            self.entered = True
            return self

        def __exit__(self, *exc):
            self.exited = True
            return False

    FakeRunTree.instances.clear()

    monkeypatch.setattr(ls, "ENABLED", True)
    monkeypatch.setattr(ls, "_RunTree", FakeRunTree)
    monkeypatch.setattr(ls, "_tracing_context", lambda **kw: FakeCM(**kw))
    # Real Attachment OK to keep — not exercised when audio_wav is None.

    req = obs_api.start_request(
        "voice_turn",
        session_id="sess-1",
        inputs={"transcript": "hi"},
        metadata={"tenant_id": "hospital_a"},
    )
    assert "run_tree" in req._state
    parent = req._state["run_tree"]
    assert parent.name == "agent.request"
    assert parent.inputs == {"transcript": "hi"}
    assert parent.extra["metadata"]["session_id"] == "sess-1"
    assert parent.extra["metadata"]["tenant_id"] == "hospital_a"
    assert req._state["tracing_cm"].parent is parent
    assert req._state["tracing_cm"].entered is True

    obs_api.record_voice_event(
        req,
        "asr_finalized",
        metrics={"asr_latency_ms": 280},
        metadata={"provider": "deepgram"},
    )
    assert len(parent.children) == 1
    child = parent.children[0]
    assert child.name == "voice.asr_finalized"
    assert child.run_type == "tool"
    assert child.inputs["metrics"] == {"asr_latency_ms": 280}
    assert child.inputs["provider"] == "deepgram"
    assert child.outputs == {"event": "asr_finalized", "audio_bytes": None}
    assert child.posted and child.patched

    obs_api.record_tool(req, "my_tool", input={"q": 1}, output={"r": 2})
    assert len(parent.children) == 2
    tool_child = parent.children[1]
    assert tool_child.name == "tool.my_tool"
    assert tool_child.outputs == {"output": {"r": 2}}

    obs_api.finish_request(req, status="success", outputs={"final": "done"})
    assert parent.ended is True
    # finish_request now stamps `status` into outputs so both backends can
    # pivot on barge-in vs success without using the error flag.
    assert parent.end_outputs == {"final": "done", "status": "success"}
    assert parent.patched is True
    assert req._state["tracing_cm"] if "tracing_cm" in req._state else True
    # tracing_cm was popped from _state by finish_request
    assert "run_tree" not in req._state
    assert "tracing_cm" not in req._state


def test_verb_layer_disabled_is_passive(monkeypatch):
    """When LangSmith is disabled, start_request still returns a Request but
    creates no run tree, and the other verbs are silent no-ops."""
    from propio_obs import api as obs_api
    from propio_obs.exporters import langsmith as ls

    monkeypatch.setattr(ls, "ENABLED", False)

    req = obs_api.start_request("voice_turn", session_id="s1")
    assert req.session_id == "s1"
    assert "run_tree" not in req._state

    # Should not raise.
    obs_api.record_voice_event(req, "asr_finalized", audio_wav=b"\x00\x01" * 16000)
    obs_api.record_tool(req, "noop", input={}, output={})
    obs_api.finish_request(req, status="success")


def test_datadog_fan_out_alongside_langsmith(monkeypatch):
    """When both backends are enabled, every verb emits to both. Verified by
    monkey-patching the dd module's primitives."""
    from propio_obs import api as obs_api
    from propio_obs.exporters import datadog as dd
    from propio_obs.exporters import langsmith as ls

    # ── LangSmith mocks (same shape as the parent/child test above) ──
    class FakeChild:
        def __init__(self, name, run_type, inputs):
            self.name, self.inputs = name, inputs
            self.attachments = {}

        def add_metadata(self, md): pass
        def end(self, outputs=None, error=None): pass
        def post(self): pass
        def patch(self): pass

    class FakeRunTree:
        def __init__(self, **data):
            self.name = data.get("name")
            self.children = []

        def post(self): pass
        def create_child(self, name, run_type, inputs):
            c = FakeChild(name, run_type, inputs)
            self.children.append(c)
            return c
        def end(self, outputs=None, error=None): pass
        def patch(self): pass

    class FakeCM:
        def __init__(self, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *exc): return False

    # ── Datadog mocks ──
    class FakeDDSpan:
        def __init__(self, name, parent=None):
            self.name = name
            self.parent = parent
            self.tags = {}
            self.error = 0
            self.finished = False
            self.children: list = []

        def set_tag(self, k, v):
            self.tags[k] = v

        def finish(self):
            self.finished = True

    class FakeTracer:
        def __init__(self):
            self.opened: list = []

        def trace(self, name, resource=None, service=None, span_type=None):
            s = FakeDDSpan(name)
            self.opened.append(s)
            return s

        def start_span(self, name, child_of=None, resource=None, service=None,
                       span_type=None, activate=True):
            s = FakeDDSpan(name, parent=child_of)
            if child_of is not None:
                child_of.children.append(s)
            return s

    fake_tracer = FakeTracer()
    monkeypatch.setattr(ls, "ENABLED", True)
    monkeypatch.setattr(ls, "_RunTree", FakeRunTree)
    monkeypatch.setattr(ls, "_tracing_context", lambda **kw: FakeCM(**kw))
    monkeypatch.setattr(dd, "ENABLED", True)
    monkeypatch.setattr(dd, "_tracer", fake_tracer)
    monkeypatch.setattr(dd, "_service", "test-svc")
    monkeypatch.setattr(dd, "_env", "dev")

    req = obs_api.start_request("voice_turn", session_id="s1", inputs={"x": 1})

    # Both backends should have stashed parent handles on the request
    assert "run_tree" in req._state
    assert "dd_span" in req._state
    parent_dd = req._state["dd_span"]
    assert parent_dd.name == "agent.request"
    assert parent_dd.tags.get("env") == "dev"

    obs_api.record_voice_event(req, "asr_finalized", metrics={"latency_ms": 250})
    assert len(parent_dd.children) == 1
    child = parent_dd.children[0]
    assert child.name == "voice.asr_finalized"
    assert child.tags.get("input.event") == "asr_finalized"
    assert child.finished is True

    obs_api.record_tool(req, "my_tool", input={"q": 1}, output={"r": 2})
    assert len(parent_dd.children) == 2
    assert parent_dd.children[1].name == "tool.my_tool"

    obs_api.finish_request(req, status="success", outputs={"final": "ok"})
    assert parent_dd.finished is True
    assert parent_dd.tags.get("output.final") == "ok"
    assert "dd_span" not in req._state


def test_datadog_disabled_is_passive(monkeypatch):
    """LangSmith on, Datadog off → only LangSmith fires; no errors."""
    from propio_obs import api as obs_api
    from propio_obs.exporters import datadog as dd
    from propio_obs.exporters import langsmith as ls

    monkeypatch.setattr(ls, "ENABLED", False)  # both off to test no-op path
    monkeypatch.setattr(dd, "ENABLED", False)

    req = obs_api.start_request("voice_turn", session_id="s1")
    assert "dd_span" not in req._state
    obs_api.record_voice_event(req, "asr_finalized")
    obs_api.record_tool(req, "noop", input={}, output={})
    obs_api.finish_request(req, status="success")  # no raise


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


def test_datadog_logs_handler_attaches_and_formats(monkeypatch):
    """When configure() runs, a handler attaches to root logger and emit()
    pipes records through _format_log → HTTPLogItem."""
    import logging as _logging

    from propio_obs.exporters import datadog_logs as dl

    # Reset module state so a fresh configure() succeeds.
    monkeypatch.setattr(dl, "ENABLED", False)
    monkeypatch.setattr(dl, "_handler", None)

    # Stub out the DD API client primitives so no real HTTPS calls happen.
    class FakeApiClient:
        def __init__(self, cfg): self.cfg = cfg
        def close(self): pass

    class FakeConfig:
        def __init__(self):
            self.api_key = {}
            self.server_variables = {}

    class FakeLogsApi:
        def __init__(self, client): pass
        def submit_log(self, body=None): pass

    captured_items: list = []

    class FakeHTTPLogItem:
        def __init__(self, **kw):
            captured_items.append(kw)
            for k, v in kw.items():
                setattr(self, k, v)

    monkeypatch.setattr(dl, "_ApiClient", FakeApiClient)
    monkeypatch.setattr(dl, "_Configuration", FakeConfig)
    monkeypatch.setattr(dl, "_LogsApi", FakeLogsApi)
    monkeypatch.setattr(dl, "_HTTPLogItem", FakeHTTPLogItem)
    monkeypatch.setattr(dl, "_HTTPLog", lambda batch: batch)
    monkeypatch.setattr(dl, "_unset", object())
    # Bypass real import — we just installed the stubs above.
    monkeypatch.setattr(dl, "_try_import_datadog_api_client", lambda: True)

    dl.configure(
        enabled=True,
        api_key="fake-key",
        site="datadoghq.com",
        service="svc",
        env="dev",
        agent_id="propio_agent_pro",
        min_level=_logging.DEBUG,
        exclude_loggers=["ddtrace"],
    )
    try:
        assert dl.ENABLED is True
        assert dl._handler is not None
        # Handler is attached to root logger.
        assert dl._handler in _logging.getLogger().handlers

        # Emit a record — _format_log should run and produce one HTTPLogItem.
        record = _logging.LogRecord(
            name="myapp", level=_logging.INFO, pathname="x.py", lineno=1,
            msg="hello %s", args=("world",), exc_info=None,
        )
        dl._handler._format_log(record)
        assert len(captured_items) == 1
        item = captured_items[0]
        assert item["service"] == "svc"
        assert "env:dev" in item["ddtags"]
        assert "service:svc" in item["ddtags"]
        assert "agent.id:propio_agent_pro" in item["ddtags"]

        # Excluded loggers are filtered at emit() time, not _format_log.
        excluded = _logging.LogRecord(
            name="ddtrace.tracer", level=_logging.INFO, pathname="x.py", lineno=1,
            msg="internal noise", args=(), exc_info=None,
        )
        before = len(captured_items)
        dl._handler.emit(excluded)
        assert len(captured_items) == before  # filtered out
    finally:
        dl.shutdown()


def test_traceable_rechecks_enabled_at_call_time():
    """Regression: ENABLED can flip True *after* a function is decorated
    (init_agent() runs at lifespan startup, but record_stt / record_tts are
    decorated at module import). The wrapper must observe the new ENABLED."""
    import functools as _ft

    from propio_obs.exporters import langsmith as ls

    orig_enabled = ls.ENABLED
    orig_ts = ls._langsmith_traceable
    try:
        # Simulate the bad-ordering case: tracing off at decoration time.
        ls.ENABLED = False
        ls._langsmith_traceable = None

        @ls.traceable(name="my_fn", run_type="tool")
        def my_fn(x):
            return x * 2

        assert my_fn(3) == 6  # no-op pass-through

        # Now init_agent() runs and flips state. Install a fake traceable
        # that records every call it intercepts.
        calls = []

        def fake_traceable(**deco_kwargs):
            def _deco(fn):
                @_ft.wraps(fn)
                def _w(*a, **kw):
                    calls.append((deco_kwargs.get("name"), a, kw))
                    return fn(*a, **kw)

                return _w

            return _deco

        ls.ENABLED = True
        ls._langsmith_traceable = fake_traceable

        assert my_fn(5) == 10
        assert calls == [("my_fn", (5,), {})]

        # Second call uses the cached wrapped fn — still traced.
        assert my_fn(7) == 14
        assert len(calls) == 2
    finally:
        ls.ENABLED = orig_enabled
        ls._langsmith_traceable = orig_ts
