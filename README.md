# propio-obs-sdk

Unified observability SDK for Propio agents. One Python package, one config file, one verb interface — fan-out to LangSmith / Datadog / Postgres without each agent importing vendor SDKs directly.

> **Status: v0.0.1** — validated in production on `propio_agent`. Supports LangSmith, Datadog APM, Datadog Logs, Postgres event-mirror.

---

## Who is this for?

Any Propio agent that wants to observability-enable its calls:

| Agent type | `modality` | What you track |
|---|---|---|
| Voice / Realtime agent | `voice` | STT latency, TTS latency, turn audio, LLM response time |
| Chat / Text agent | `text` | User message, LLM calls, tool actions, response quality |
| Tool-calling agent | `tool_agent` | Each tool invocation (input, output, error, duration) |
| Batch / Workflow | `batch` / `workflow` | Job-level spans, per-step metrics |

**Three signal types, three backends:**

- **Traces** (OTel spans) → LangSmith + Datadog APM
- **Logs** (Python `logger.*`) → Datadog Logs
- **Business events** (async Postgres writes) → Propio internal DB

## High Level Design
v1 transport: OpenTelemetry (OTel) — agent emits OTel spans / logs / metrics; an OTel Collector (in-process or sidecar) fans out to backends. Audio is out-of-band — uploaded directly to S3, with a metadata pointer in Postgres + a span attribute referencing the S3 key.
```bash
┌─────────────────────────────────────────────────────────────┐
│                  Agent code (any framework)                 │
│   import obs_sdk as obs                                  │
│   obs.init_agent(...)                                       │
│   req = obs.start_request(...)                              │
│   obs.record_tool(req, ...)                                 │
│   obs.record_voice_event(req, ..., audio_wav=...)           │
│   obs.finish_request(req)                                   │
└─────────────────────────────┬───────────────────────────────┘
                              │
                  ┌───────────▼─────────────┐
                  │     obs_sdk (SDK)    │
                  ├─────────────────────────┤
                  │  verb layer             │
                  │  ↓                      │
                  │  channel router         │
                  │  ├─→ OTel emit (spans/logs/metrics)
                  │  └─→ S3 audio uploader  │ (only for audio channel)
                  └─┬───────────────────┬───┘
                    │                   │
       ┌────────────▼─────────┐    ┌────▼──────────────┐
       │   OTel Collector     │    │  S3 (audio blobs) │
       │  (per-process or     │    │  + Postgres       │
       │   sidecar deployment)│    │  (metadata index) │
       └─┬─────┬─────┬────────┘    └───────────────────┘
         │     │     │
   ┌─────▼┐ ┌──▼──┐ ┌▼──────────┐
   │Lang- │ │ DD  │ │ Propio DB │
   │Smith │ │     │ │ (events)  │
   │OTLP  │ │OTLP │ │ Postgres  │
   └──────┘ └─────┘ └───────────┘
```
---

## Install

```bash
pip install propio-obs-sdk
```

Or inside the Propio monorepo as an editable workspace dependency:

```bash
uv add --editable ../obs_sdk
```

---

## Architecture

```
Your agent
    │
    ├── obs.start_request()           ← opens OTel span (parent)
    │       ├── obs.record_voice_event() / obs.record_tool()
    │       │       └── child OTel spans (nest under parent)
    │       ├── obs.wrap_llm_client()  ← OpenAI auto-instrumentation
    │       │       └── gen_ai.* attributes on every LLM call
    │       └── obs.finish_request()   ← closes parent span
    │
    ├── Python logger.* / logger.error()  ← Datadog Logs
    │
    └── obs.broadcast_event()         ← asyncpg → Postgres `logs` table
            └── obs.start_session()   ← session heartbeat (Postgres)

All spans/logs:
    │
    └── OTel Collector (localhost:4318)
            ├── → LangSmith (https://api.smith.langchain.com/otel)
            └── → Datadog APM
```

---

## Quick Start

### 1. Create `observability.yml`

```yaml
agent:
  agent_id: my_agent
  agent_name: My Example Agent
  agent_type: chat_agent
  modality: text
  service: my-service
  environment: dev          # dev | qa | staging | prod (required)
  version: "1.0.0"
  default_tags:
    team: ai-platform

quality_metrics:
  - task_success
  - answer_grounded

backends:
  langsmith:
    enabled: true
    api_key_env: LANGSMITH_API_KEY
    # project unset → SDK auto-generates "{agent_id}-{env}" (e.g. my_agent-dev)
  datadog:
    enabled: false
  datadog_logs:
    enabled: false
  postgres_db:
    enabled: true          # default — Propio internal DB

otel:
  collector_endpoint: http://localhost:4318
```

### 2. Initialize once at startup

```python
import propio_obs as obs

obs.init_agent("observability.yml")   # or pass a dict directly
```

### 3. Wrap your LLM client (optional but recommended)

```python
from openai import AsyncOpenAI

client = obs.wrap_llm_client(AsyncOpenAI(api_key=api_key))
# Every chat.completions.create() now emits an OTel span
# with gen_ai.* semantic conventions automatically.
```

---

## Voice Agent — Full Turn Example

`propio_agent` uses this pattern on every user turn. A full voice turn:

```python
import propio_obs as obs

# ── Per session (once per WebSocket connection) ──────────────────
await obs.start_session(
    session_id=ws.session_id,
    config={
        "stt_provider": "deepgram",
        "stt_model": "nova-3",
        "stt_language": "en",
        "llm_model": "gpt-4o-mini",
        "tts_provider": "openai",
        "tts_voice": "alloy",
    },
    env="prod",
)

# ── Per user turn ───────────────────────────────────────────────
req = obs.start_request(
    "voice_turn",
    session_id=ws.session_id,
    inputs={"transcript": transcript},         # final STT output
    metadata={
        "stt_provider": "deepgram",
        "stt_model": "nova-3",
        "stt_language": "en",
        "tts_provider": "openai",
        "tts_voice": "alloy",
        "llm_model": "gpt-4o-mini",
        "turn_id": turn_id,
        "tenant_id": tenant_id,                # optional — surfaces in LangSmith threads
    },
)

# ── STT done ─────────────────────────────────────────────────────
obs.record_voice_event(
    req,
    "asr_finalized",
    metrics={
        "user_speech_ms": 1850.0,             # time user was speaking
        "stt_finalize_ms": 320.0,             # time to get final transcript
    },
    metadata={
        "transcript": transcript,
        "provider": "deepgram",
        "model": "nova-3",
        "language": "en",
    },
)

# obs.wrap_llm_client() already auto-instrumented your LLM calls.
# Each call becomes a child span under `req` automatically.

# ── TTS done ─────────────────────────────────────────────────────
obs.record_voice_event(
    req,
    "tts_finished",
    metrics={
        "latency_ms": 540.0,                  # end-to-end TTS time
        "ttfb_ms": 80.0,                       # time to first audio byte
        "audio_duration_ms": 3200.0,          # generated audio length
        "total_bytes": 51200,
        "num_chunks": 16,
    },
    metadata={
        "text": spoken_text,
        "provider": "openai",
        "voice": "alloy",
    },
)

# ── Quality check (optional) ─────────────────────────────────────
obs.record_quality(req, "tool_returned_data", value=True, comment="Data was fresh")

# ── Finish the turn ──────────────────────────────────────────────
obs.finish_request(
    req,
    status="completed",                        # completed | interrupted | error
    outputs={"response_text": spoken_text},
    # error="..." only for true exceptions, not user interruptions
)
```

**On WebSocket disconnect:**

```python
finally:
    await obs.end_session(ws.session_id)
```

**Broadcasting pipeline events (fire-and-forget):**

```python
async def event_callback(event: dict):
    # e.g. { "type": "user_transcript", "text": "hello", "timestamp": ... }
    await obs.broadcast_event(event, session_id=ws.session_id)
```

Every event type (`user_transcript`, `turn_metrics`, `interruption`, `error`, …) gets mirrored to the Postgres `logs` table asynchronously.

---

## Text / Chat Agent — Equivalent Example

For a REST API chatbot or text agent, the pattern is the same but without voice-specific events:

```python
import propio_obs as obs

# ── Per session ───────────────────────────────────────────────────
await obs.start_session(
    session_id=request.session_id,
    config={"llm_model": "gpt-4o", "prompt_version": "v2"},
    env="prod",
)

# ── Per user message ──────────────────────────────────────────────
req = obs.start_request(
    "chat_turn",                              # or "chat_agent", "workflow_step"
    session_id=request.session_id,
    inputs={"user_message": user_text},
    metadata={
        "user_id": user_id,
        "tenant_id": tenant_id,
    },
)

# LLM call auto-instrumented via wrap_llm_client() — no extra code needed.

# ── Record a tool call ───────────────────────────────────────────
obs.record_tool(
    req,
    "search_knowledge_base",
    input={"query": user_text, "top_k": 5},
    output={"results": [...], "count": 3},
    error=None,
    metadata={"retriever": "pinecone", "index": "kb_v2"},
)

# ── Record another tool ──────────────────────────────────────────
obs.record_tool(
    req,
    "call_customer_api",
    input={"customer_id": customer_id, "action": "get_balance"},
    output={"balance": 1234.56},
    error=None,
)

# ── Quality metric ───────────────────────────────────────────────
obs.record_quality(req, "answer_grounded", value=True)
obs.record_quality(req, "task_success", value=success_bool)

# ── Finish ───────────────────────────────────────────────────────
obs.finish_request(
    req,
    status="completed",
    outputs={"assistant_message": response_text},
)
```

Key differences from voice agents:

| | Voice agent | Text / Chat agent |
|---|---|---|
| `request_type` | `voice_turn` | `chat_turn`, `chat_agent`, `workflow_step` |
| Voice events | `asr_finalized`, `tts_finished` | — (omit) |
| STT/TTS metrics | yes | — |
| Audio buffers | captured (PCM → WAV) | — |
| `broadcast_event` | every pipeline event | typically only error / session start/end |

---

## Tool Agent — record_tool Reference

`record_tool` creates a child span for each tool invocation. Use it for MCP tools, API calls, function executions:

```python
obs.record_tool(
    req,
    "get_weather",                          # tool name → span name: tool.get_weather
    input={
        "city": "Tokyo",
        "unit": "celsius",
    },
    output={
        "temperature": 22,
        "conditions": "partly cloudy",
    },
    error=None,                             # set for failures
    metadata={
        "provider": "openweather",
        "cache_hit": False,
    },
)
```

On failure:

```python
obs.record_tool(
    req,
    "payment",
    input={"amount": 99.90, "currency": "USD"},
    output=None,
    error="Card declined: insufficient funds",
    metadata={"processor": "stripe"},
)
```

The `error` field sets `error=True` on the span and stamps the message — LangSmith and Datadog both surface failed spans visually.

---

## Configuration Reference

Full `observability.yml` schema:

```yaml
# ── Required ──────────────────────────────────────────────────────
agent:
  agent_id: str                    # unique across all Propio agents
  agent_type: realtime_agent | chat_agent | tool_agent | batch | workflow
  modality: text | voice | multimodal
  service: str                     # Datadog service tag
  environment: dev | qa | staging | prod  # required; falls back to PROPIO_ENV

# ── Optional ─────────────────────────────────────────────────────
  agent_name: str                  # human-readable
  version: str                     # e.g. git short sha
  default_tags:                    # extra tags on every span
    team: ai-platform
    region: us-east-1

# ── Product metrics ───────────────────────────────────────────────
quality_metrics:                   # validated at record_quality() time
  - task_success
  - answer_grounded

voice_metrics:                     # voice-only; informational
  - first_audio_ms
  - asr_latency_ms
  - barge_in_success

# ── Backends ──────────────────────────────────────────────────────
backends:
  langsmith:
    enabled: bool
    api_key_env: str               # default LANGSMITH_API_KEY
    project: str                   # default "{agent_id}-{env}" (e.g. my_agent-dev)
    endpoint: str                  # default https://api.smith.langchain.com

  datadog:
    enabled: bool
    api_key_env: str               # default DD_API_KEY
    site: str                      # default datadoghq.com
    agent_url: str                 # override if not using localhost:8126

  datadog_logs:
    enabled: bool
    api_key_env: str
    site: str
    service: str
    env_tag: str
    version: str
    min_level: DEBUG | INFO | WARNING | ERROR   # default DEBUG
    exclude_loggers: [ddtrace, urllib3, datadog, httpx]

  postgres_db:
    enabled: bool                  # default True (Propio internal DB)
    url_env: str                   # default POSTGRES_DB_URL_<env> from platform

# ── Wire config ───────────────────────────────────────────────────
otel:
  collector_endpoint: str          # default http://localhost:4318
```

---

## What Ends Up Where

### Traces (OTel spans) — LangSmith + Datadog

| SDK call | Span name | Key attributes |
|---|---|---|
| `start_request(...)` | `agent.request` | `request.id`, `session.id`, `langsmith.span.kind=chain`, `input.value` (JSON), `langsmith.inputs.*` |
| `record_voice_event(req, "asr_finalized", ...)` | `voice.asr_finalized` | `voice.event`, `voice.metrics.user_speech_ms`, `voice.metrics.stt_finalize_ms`, `provider`, `model`, `language` |
| `record_voice_event(req, "tts_finished", ...)` | `voice.tts_finished` | `voice.metrics.latency_ms`, `voice.metrics.ttfb_ms`, `voice.metrics.audio_duration_ms`, `provider`, `voice` |
| `record_tool(req, "tool_name", ...)` | `tool.tool_name` | `langsmith.span.kind=tool`, `tool.output` (truncated 2000 chars), `error`, `error.message` |
| LLM call (via `wrap_llm_client`) | auto-named by OpenAI instrumentor | `gen_ai.*` semantic conventions (model, usage, latency) |
| `finish_request(req, status, outputs, error)` | — (closes parent) | `status`, `output.*`, `output.value` (JSON), `error`, `error.message` |
| `record_quality(req, "metric_name", ...)` | — (on parent span) | `quality.metric_name`, `quality.metric_name.comment` |

### Logs — Datadog Logs

All Python `logger.info / logger.warning / logger.error` calls route to Datadog Logs via the OTel `LoggingHandler`. Filter out noisy loggers via `backends.datadog_logs.exclude_loggers`.

### Business Events — Postgres

| SDK call | Postgres table | What lands |
|---|---|---|
| `start_session(...)` | `sessions` | `session_id`, `config` (JSON), `env`, `agent_id`, `is_active=TRUE`, heartbeat every 5s |
| `broadcast_event(event, session_id=...)` | `logs` | Full event dict + `session_id`, `agent_id`, `_monitor.timestamp`. Fires `pg_notify('monitor_events', id)`. Fire-and-forget. |
| `end_session(...)` | `sessions` | `is_active=FALSE` |

---

## LangSmith Project Naming

When `backends.langsmith.project` is unset, the SDK auto-generates:

```
{agent_id}-{environment}    # e.g. my_agent-dev, my_agent-prod
```

This gives per-env project isolation — dev data never pollutes prod in LangSmith.

To override, set `project` explicitly in your config:

```yaml
backends:
  langsmith:
    project: my-custom-project-name   # always used as-is
```

---

## Local Dev: OTel Collector

The SDK sends spans via OTLP HTTP to a Collector. Start it locally:

```bash
cd observability_platform
docker compose -f deploy/docker-compose.yml up -d
```

The Collector (default `http://localhost:4318`) fans out to LangSmith and Datadog automatically — no per-agent config needed there.

---

## Error Handling

All SDK calls are **defensive** — failures log a warning and continue:

```python
try:
    obs.record_voice_event(req, "asr_finalized", ...)
except Exception:
    logger.warning("[obs] record_voice_event 'asr_finalized' failed: ...")
    # Your agent keeps running.
```

`finish_request` is **idempotent** — safe to call from multiple `except` branches.

---

## Status & Roadmap

| Version | What's supported |
|---|---|
| **v0.0.1** | LangSmith (traces + session events), Postgres event-mirror, `wrap_llm_client` via OpenAI auto-instrumentation |
| **v0.1** | Datadog APM (traces), Datadog Logs |
| **v0.2** | S3 audio attachments (audio playback in LangSmith) |
| **v0.3** | Auto-dashboard provisioning, alerting |

Database schema (`sessions` / `logs` tables) is owned by `observability_platform`. Schema changes go there, not here.
