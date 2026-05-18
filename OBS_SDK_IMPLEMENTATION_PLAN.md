# Propio Observability SDK — Implementation Plan

> Status: **Draft / Pre-implementation** — design doc for team review before code.
> Target package name: `propio-obs-sdk`
> Author: AI platform / scaffold from propio voice agent observability stack.

---

## 1. Executive Summary

### 1.1 What v1 ships — `propio-obs-sdk`

**The v1 deliverable is a single Python SDK.** Nothing else. No new platform, no new UI, no new backend service.

```bash
pip install propio-obs-sdk
```

Every new agent imports the SDK, writes one YAML, calls 6 verbs. The SDK handles everything else:

- Sends LLM traces to **LangSmith**
- Sends APM spans + logs + metrics to **Datadog**
- Uploads voice audio to **S3** (with a metadata index in **Postgres**)
- Mirrors events to **Propio's existing monitor DB**

**Agents stop importing `langsmith` / `ddtrace` / `boto3` / `asyncpg` directly.** Only the SDK touches those. One dependency, one configuration surface, three backends covered.

### 1.2 Design principle — thin orchestration, not reinvention

The SDK is **plumbing**. It does not build new observability features that LangSmith or Datadog already provide. Concretely:

- ❌ SDK does **not** ship its own trace UI (LangSmith already has one — better than anything we'd build).
- ❌ SDK does **not** ship its own APM viewer (Datadog already has one).
- ❌ SDK does **not** ship its own LLM evaluator engine (LangSmith schedules evaluators natively; SDK pulls scores back via API).
- ❌ SDK does **not** ship its own alert engine (LangSmith and Datadog each have one; alerts route to the same incident channel).
- ✅ SDK **enforces a unified schema** so all three backends carry the same correlation keys (`request_id`, `tenant_id`, `agent_id`, `version`, etc.).
- ✅ SDK **routes data** to the right backend per a per-agent YAML.
- ✅ SDK **provisions** default dashboards / alerts on first init via vendor admin APIs (Phase 6+).

That's it. The SDK is the *enforcement layer* for standardization across agents. It is not a new product line.

### 1.3 Why this matters

Today every team wires up LangSmith / Datadog / internal DB **independently**, with inconsistent IDs, event names, and dashboards. Cross-agent or cross-tenant questions ("p95 first-audio latency for hospital_a across all our agents this week") are unanswerable because the data is shaped differently in each backend. The SDK fixes this by being the only path agents use to emit observability data.

### 1.4 Verb surface

A single internal Python library with ~6 high-level "verbs" (`init_agent`, `start_request`, `record_tool`, `record_quality`, `record_voice_event`, `finish_request`) plus helpers (`wrap_llm_client`, `langchain_callback`, `attach_openai_realtime`). Internally the SDK emits **OpenTelemetry** spans / logs / metrics, and an OTel Collector fans out to LangSmith, Datadog, and Propio DB. Audio is uploaded out-of-band to S3 with metadata indexed in Postgres.

**Selective routing is a first-class feature**: an agent can decide that LLM traces go *only* to LangSmith, server logs go *only* to Datadog Logs, voice events go to *both* LangSmith and Propio DB, etc. The SDK does not force every backend to receive every event.

### 1.5 First delivery

Validate the design inside the propio voice agent (we already have `backend/app/services/tracing.py` that does ~30% of this for LangSmith). Then extract as a standalone package and onboard the next agent.

### 1.6 Out of scope for v1 (future work, NOT shipping now)

The following are **not** part of the SDK and **not** part of v1. They are a longer-term **Agent Observability Platform** vision that may follow once the SDK is in production at 2-3 agents:

- A cross-agent / cross-tenant **summary analytics store** (Postgres) for queries that neither LangSmith nor Datadog can answer cheaply.
- A **drill-down hub UI** — one URL per `request_id` that pivots into LangSmith trace + Datadog APM trace + audio playback + evaluator scores.
- A **unified incident inbox** that aggregates LangSmith and Datadog alerts.

These are interesting but **not** v1. v1 is just the SDK.

---

## 2. Goals & Non-Goals

### Goals

1. **Uniform interface**: any new agent calls the same 6 verbs regardless of LLM provider, framework, or agent type (voice / chat / multi-agent).
2. **Multi-backend fan-out**: write code once, observe in N backends. Adding a new backend is a config change, not a code change.
3. **Selective routing**: a per-agent config decides which channel goes to which backends.
4. **Zero-touch defaults**: `init_agent()` reads a YAML, sets up `ddtrace` auto-patch, hooks Python `logging` to Datadog, and configures LangSmith env vars. Existing `logger.info()` and `httpx`/`openai` calls become observable without code changes.
5. **Failure isolation**: a backend outage must not break the agent. Exports run on a background queue with timeouts.
6. **Standard metadata**: every event carries `agent_id`, `agent_type`, `service`, `env`, `team`, `request_id`, `session_id`, `turn_id`. Cross-agent dashboards become possible.
7. **Audio support**: voice agents can attach raw audio (user speech, agent TTS) to LangSmith traces playable inline in the UI.

### Non-Goals (v1)

- **Not** a replacement for LangChain / OpenAI SDK / Pipecat — it sits *next to* them.
- **Not** vendor-locked. v1 uses OpenTelemetry as the wire layer so backends are swappable via Collector config.
- **Not** an evaluator / scorer engine. `record_quality()` accepts a precomputed score; it does not run evals.
- **Not** auto-creating dashboards in v1 (deferred — see §13).
- **Not** a multi-language SDK. Python only for v1; Node / Go are future work.

---

## 3. High-Level Architecture

**v1 transport: OpenTelemetry (OTel)** — agent emits OTel spans / logs / metrics; an OTel Collector (in-process or sidecar) fans out to backends. Audio is **out-of-band** — uploaded directly to S3, with a metadata pointer in Postgres + a span attribute referencing the S3 key.

```
┌─────────────────────────────────────────────────────────────┐
│                  Agent code (any framework)                 │
│   import propio_obs as obs                                  │
│   obs.init_agent(...)                                       │
│   req = obs.start_request(...)                              │
│   obs.record_tool(req, ...)                                 │
│   obs.record_voice_event(req, ..., audio_wav=...)           │
│   obs.finish_request(req)                                   │
└─────────────────────────────┬───────────────────────────────┘
                              │
                  ┌───────────▼─────────────┐
                  │     propio_obs (SDK)    │
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

**Key design decisions:**

- **OTel is the wire format**. SDK produces OTel `Span` / `Log` / `Metric` objects. Collector decides where each one lands (LangSmith / Datadog / Propio DB).
- **Audio is NOT an OTel attachment** — OTel doesn't have a robust binary attachment story. Audio goes to S3 directly; the OTel span carries the S3 key as an attribute (`audio.s3_key`). Backends that want playback follow the pointer.
- **Postgres is the metadata index for audio** — observability platform queries PG for "give me all audio for session X" (fast, indexed) and only fetches WAV from S3 on demand (`s3.get_object` → signed URL).
- **In-process SDK** — agent imports it directly. The OTel Collector can be embedded (in-process exporter) or run as a sidecar; default v1 = in-process.
- **Verb layer is the only public API** — channel router and OTel internals are not exposed.
- **Async export queue** — verbs return immediately; OTel batch span processor + S3 upload run on background tasks.
- **Channel-based routing** — events tagged by *channel* (`llm_trace`, `tool_call`, `log`, etc.); router decides what to emit (OTel span / OTel log / OTel metric / S3 upload) and the Collector decides which backend(s) receive each.

---

## 3.5 Layered Architecture & Division of Labor

> **v1 scope clarification**: v1 ships **only Layer A** (the `propio-obs-sdk` package). Layer B is a data contract enforced by the SDK at emit time — not a separate component. Layer C (the **Agent Observability Platform** UI for cross-agent analytics, drill-down, audio playback) is **future work** and is **not** part of v1. For v1, observability consumption = LangSmith UI + Datadog UI directly.

The system is **3 layers** in the long-term vision. Each layer has one job, and existing tools own most of the work.

```
                     ┌──────────────────────────────────────┐
                     │  Layer C: Consumption / UI           │
                     │  ────────────────────────────────    │
                     │  • LangSmith UI    (LLM trace deep dive,
                     │                     evaluator scores,
                     │                     thread view)
                     │  • Datadog UI      (APM, logs, infra,
                     │                     unified service map)
                     │  • Agent Observability Platform UI   ← FUTURE, not in v1
                     │      (cross-agent / cross-tenant
                     │       summary, drill-down hub,
                     │       audio playback)
                     └──────────────────────────────────────┘
                                       ▲
                                       │
                     ┌──────────────────────────────────────┐
                     │  Layer B: Unified Data Model         │
                     │  ────────────────────────────────    │
                     │  • Correlation keys                  │
                     │  • Standard event names              │
                     │  • Standard metric definitions       │
                     │  • Platform vs Product metric split  │
                     └──────────────────────────────────────┘
                                       ▲
                                       │
                     ┌──────────────────────────────────────┐
                     │  Layer A: Collection                 │
                     │  ────────────────────────────────    │
                     │  • propio-obs-sdk (this package)     │
                     │  • OTel Collector → fan-out          │
                     │  • S3 audio upload                   │
                     │  • Postgres metadata index           │
                     │  • Postgres summary store            │
                     └──────────────────────────────────────┘
                                       ▲
                                       │
                     ┌──────────────────────────────────────┐
                     │  Agent code (any framework)          │
                     └──────────────────────────────────────┘
```

### Division of labor — who owns what

| Concern | Owner | Why |
|---|---|---|
| LLM run tree, prompt/response capture, threads, online evaluators | **LangSmith** | Native — its product. We pay for it; we use it. |
| APM spans, infra metrics, structured logs, log/trace correlation, env/service/version tagging | **Datadog** | Native — its product. We pay for it; we use it. |
| Service map, infra alerts, runbook integration | **Datadog** | Same. |
| Online evaluator scheduling, feedback API, evaluator UI | **LangSmith** | Same. |
| **Standard schema enforcement** (everyone uses same field names) | **propio-obs-sdk** | Only we can enforce this. Vendors can't. |
| **Cross-agent / cross-tenant aggregation** (e.g. p95 latency by tenant by model) | **Agent Observability Platform** (PG summary store) — *future, not v1* | LangSmith and Datadog don't talk to each other; cross-platform queries need a common store. v1 SDK ensures the schema is ready; the platform itself comes later. |
| **Unified drill-down URL** (one click → LangSmith trace + Datadog APM + audio) | **Agent Observability Platform** — *future, not v1* | A small web layer that takes `request_id` and renders pivots into both backends. |
| **Default dashboards / alerts per new agent** | **propio-obs-sdk** (provisioning) | Has the agent config; calls Datadog Dashboard API + LangSmith project API on init. |
| **Audio blob storage** | **S3** | Cheap, auditable, retention-controllable. |
| **Audio metadata index** | **Postgres** | Fast queries by session/tenant. Don't `LIST` S3. |
| **Summary events** (one row per request with key metrics) | **Postgres summary store** | Powers cross-agent analytics in our own UI. |

### Things flowing per request

**v1 (SDK only):**
```
Agent processes one request
    │
    ├─→ Datadog: APM trace + logs + custom metrics  (operational view)
    ├─→ LangSmith: root trace + child runs + thread (LLM view)
    └─→ Propio internal monitor DB: event mirror    (live realtime monitor — already exists)
```

**Future (Agent Observability Platform — not in v1):**
```
                                                 ┌──────────────────────────────────┐
    ... same as above ...                        │ + Postgres summary store         │
                                                 │   one row per request for cross- │
                                                 │   agent analytics & drill-down   │
                                                 └──────────────────────────────────┘
```

The SDK's schema is built so adding the summary store later is a config change, not a code change. All destinations carry the **same correlation keys** (`request_id`, `session_id`, `conversation_id`, `agent_id`, `version`, `env`, `tenant_id`). That's the entire reason the unified data model in Layer B exists.

### What v1 explicitly does NOT build

- **Our own LLM trace UI** — LangSmith is better than anything we'd build in 6 months.
- **Our own log search engine** — Datadog Logs is better.
- **Our own APM / span viewer** — Datadog APM is better.
- **Our own evaluator engine** — LangSmith's online evaluators run on schedule and produce scores; we just consume them via API.
- **Our own alerting engine for trace/log/metric thresholds** — both Datadog and LangSmith already do this; we route alerts to the same incident channel (e.g. PagerDuty / Slack).
- **An Agent Observability Platform UI** — that's future work after the SDK is in production at 2-3 agents.

### What v1 SDK actually builds

- **The verb layer** (`init_agent`, `start_request`, etc.) — the only thing agents call.
- **Standard schema enforcement** — every event carries the unified correlation keys.
- **OTel emission + Collector config** — fan-out to LangSmith / Datadog.
- **Audio storage path** (S3 + PG metadata index) — neither vendor handles this well.
- **Provisioning glue** — `init_agent()` calls Datadog Dashboard API + LangSmith project API for default dashboards / alerts (Phase 6+).

---

## 4. Package & Distribution

### Package metadata

```toml
# pyproject.toml (excerpt)
[project]
name = "propio-obs-sdk"
version = "0.1.0"
description = "Propio agent observability SDK — LangSmith + Datadog + Propio DB fan-out"
requires-python = ">=3.11"

dependencies = [
    "pyyaml>=6.0",
    "pydantic>=2.7",
    "httpx>=0.28",
    # OpenTelemetry — the wire layer
    "opentelemetry-api>=1.25",
    "opentelemetry-sdk>=1.25",
    "opentelemetry-exporter-otlp>=1.25",
    "opentelemetry-instrumentation-httpx>=0.46b0",
    "opentelemetry-instrumentation-fastapi>=0.46b0",
    # OpenLLMetry — auto-instrumentation for OpenAI / Anthropic / etc., emits OTel
    "traceloop-sdk>=0.30",
    # S3 + Postgres for audio out-of-band path
    "boto3>=1.34",
    "asyncpg>=0.29",
    # Optional (loaded lazily based on config.backends):
    "langsmith>=0.7",         # only when LangSmith backend wants the native client (e.g. fetching evaluator scores)
    "ddtrace>=2.0",           # only when Datadog APM/Logs configured directly (vs OTel route)
    "openai>=2.14",
]

[project.optional-dependencies]
langchain = ["langchain-core>=0.2"]
realtime  = ["websockets>=14"]
dev       = ["pytest", "pytest-asyncio", "ruff", "mypy"]
```

### Distribution

- **v0.x**: published to internal pypi (CodeArtifact / Artifactory / Nexus).
- Agents install with `pip install propio-obs-sdk`.
- Bootstrap fallback: `pip install git+ssh://git@github.com/propio/propio-obs-sdk.git@v0.1.0`.
- **Pinning**: agents must pin `propio-obs-sdk==0.1.x` (compatible release operator) to absorb patch updates without breaking on minor bumps.

### Versioning

Semver. Breaking verb-signature changes → major bump. Adding a new exporter → minor. Bug fix → patch.

---

## 5. Configuration Schema

Every agent ships a YAML alongside its code. Loaded once by `init_agent()`.

### Full example: `observability.yml`

```yaml
# ─── Agent identity (required) ───────────────────────────────
agent:
  agent_id: support_voice              # unique per agent across propio
  agent_type: realtime_agent           # realtime_agent | chat_agent | tool_agent | batch
  modality: voice                      # voice | text | multimodal
  service: agent-gateway               # which deployable service hosts this agent
  default_tags:                        # applied to every event
    team: ai-platform
    env: prod                          # or staging | dev
    domain: support

# ─── Custom metric definitions ────────────────────────────────
quality_metrics:
  - task_success                       # bool / float, recorded per request
  - answer_grounded
  - escalation_avoided

voice_metrics:
  - first_audio_ms                     # numeric, recorded per voice event
  - barge_in_success
  - asr_latency_ms

# ─── OTel Collector (transport layer) ────────────────────────
# All spans / logs / metrics flow through this. Backends are configured
# at the Collector level (separate YAML), not here — agent only points
# at one OTLP endpoint.
otel:
  endpoint: http://localhost:4317      # in-process Collector or sidecar
  protocol: grpc                       # grpc | http
  service_name: agent-gateway          # OTel resource attribute
  resource_attributes:                 # become OTel resource attrs
    deployment.environment: prod

# ─── Backend-specific configs (only when SDK needs the native client) ──
# These are NOT primary export paths — primary path is OTel above.
# Native clients are used only for things OTel can't do:
#   - LangSmith: fetch evaluator scores back via REST
#   - S3: audio blob upload (out-of-band)
#   - Postgres: audio metadata index (out-of-band)
backends:
  langsmith:
    enabled: true
    api_key_env: LANGSMITH_API_KEY
    project: customer-support-prod
    endpoint: https://api.smith.langchain.com
    # Used by SDK to PULL evaluator scores LangSmith computes on schedule.
    fetch_evaluator_scores: true

  audio_s3:
    enabled: true
    bucket: agent-recordings
    region: us-east-1
    key_prefix: ""                     # default; agent path injected automatically

  audio_index_pg:                      # metadata index for audio
    enabled: true
    url_env: AUDIO_INDEX_PG_URL        # postgres DSN
    table: audio_recordings

  propio_db:                           # event mirror DB (separate from audio index)
    enabled: true
    url_env: PROPIO_DB_URL

# ─── Channel routing (the "fan-out" map) ─────────────────────
# Channels declare WHAT they are. The OTel Collector config (separate file)
# decides which downstream backend each channel lands in. This map below
# only controls SDK-side decisions: emit OTel? upload audio? insert PG row?
routing:
  llm_trace:    [otel]                           # → Collector → LangSmith
  tool_call:    [otel]
  voice_event:  [otel, audio_s3, audio_index_pg] # OTel span + audio blob + PG row
  quality:      [otel]                           # OTel attrs; LangSmith evaluator runs scheduled
  apm_span:     [otel]                           # → Collector → Datadog APM
  log:          [otel]                           # → Collector → Datadog Logs
  metric:       [otel]                           # → Collector → Datadog Metrics

# ─── Behavior tuning ─────────────────────────────────────────
behavior:
  async_export: true                   # background queue
  export_queue_size: 1000              # drops oldest if full
  export_timeout_ms: 5000
  sampling:                            # 100% across the board for v1 — see §11
    llm_trace: 1.0
    voice_event: 1.0
    log: 1.0
  redaction:
    pii_fields: [email, phone, ssn]
```

> **Why OTel routing isn't a flat list per channel** — once a span/log/metric is in OTel, the **OTel Collector** decides where it goes (LangSmith via OTLP, Datadog via OTLP, etc.). The Collector config is operational (deployed once per env), not per-agent. The agent's `routing` map just controls "does this channel emit OTel at all? does it also need an out-of-band audio upload?" The list `[otel]` means "emit one OTel signal"; `[otel, audio_s3, audio_index_pg]` means "OTel + S3 upload + PG insert".

### Config loading

```python
# Inside SDK
from propio_obs.config import AgentConfig
cfg = AgentConfig.from_yaml("observability.yml")
# Pydantic validates schema on load; raises with helpful errors.
```

### Channels (canonical list)

| Channel | Purpose | Wire signal | Downstream (set at Collector) |
|---|---|---|---|
| `llm_trace` | LLM completion calls, prompt/response, tokens | OTel Span (kind=client) | LangSmith |
| `tool_call` | Function/tool invocation with inputs/outputs | OTel Span (kind=internal) | LangSmith |
| `voice_event` | STT / TTS / VAD / barge-in events | OTel Span + audio→S3 + metadata→PG | LangSmith (span attrs only); audio playback via S3 signed URL |
| `quality` | Per-request quality scores (raw I/O — score computed by LangSmith on schedule) | OTel Span attributes | LangSmith |
| `apm_span` | HTTP request, DB query, external API timing | OTel Span (auto via instrumentations) | Datadog APM |
| `log` | Structured server logs | OTel Log | Datadog Logs |
| `metric` | Custom counters/histograms | OTel Metric | Datadog Metrics |

Channels are **fixed in v1** — the SDK defines the set. Agents can't invent new ones.

---

## 6. Public API — The Verbs

All under top-level `import propio_obs as obs`.

### 6.1 `obs.init_agent(config_path: str | Path | dict) -> None`

Called **once** at process startup. Side effects:

1. Loads + validates YAML.
2. Reads `*_env` variables from environment.
3. Instantiates each enabled exporter; if a backend is misconfigured, logs a warning and continues (one bad backend doesn't kill init).
4. Calls `ddtrace.patch_all()` if `datadog_apm.auto_patch`.
5. Bridges stdlib `logging` to Datadog if `datadog_logs.bridge_python_logging`.
6. Sets `os.environ["LANGSMITH_*"]` if LangSmith enabled.
7. Starts the async export queue worker.

Idempotent: calling twice is a no-op (warns).

### 6.2 `obs.start_request(...) -> Request`

```python
def start_request(
    request_type: str,                       # "voice_turn" | "chat" | "tool_run" | ...
    *,
    agent_id: str | None = None,             # defaults to config.agent.agent_id
    session_id: str | None = None,           # auto-generated if missing
    user_id: str | None = None,
    inputs: dict | None = None,              # initial inputs (e.g. user message)
    metadata: dict | None = None,            # arbitrary extra tags
) -> Request:
```

- Mints `request_id` (uuid).
- Opens a parent **LangSmith run** (run_type="chain") if `llm_trace` routes to LangSmith.
- Opens a **Datadog APM span** (`agent.request`) if `apm_span` routes to Datadog.
- Inserts a row in Propio DB if routed there.
- Returns a `Request` opaque handle the caller passes to subsequent verbs.

### 6.3 `obs.record_tool(req, name, input, output, *, error=None, metadata=None) -> None`

- Emits `tool_call` channel.
- LangSmith: creates child run (run_type="tool") under the request's parent run.
- Datadog: child APM span if also routed there.

### 6.4 `obs.record_quality(req, metric: str, value: float | bool | None = None, *, comment: str | None = None) -> None`

- Emits `quality` channel — written as **OTel span attributes on the parent run**, not a separate event.
- `metric` must be in `config.quality_metrics` (validated; warning if not).
- **`value` is optional** in v1. The agent does NOT have to compute a score itself.
- **Where scores actually come from**: LangSmith runs a scheduled evaluator (configured in the LangSmith project) that reads the trace's input/output and writes a feedback score back. The observability platform pulls scores from the LangSmith Feedback API on a schedule (e.g. hourly). See §15 for rationale.
- If `value` is provided, it's recorded immediately as a fast-path (useful for deterministic checks like "did the tool return non-null"). Pre-computed scores and LangSmith-evaluated scores coexist on the same trace under different keys (`metric` vs `metric.evaluated`).

### 6.5 `obs.record_voice_event(req, event: str, *, metrics: dict | None = None, audio_wav: bytes | None = None) -> None`

```python
# event: "speech_start" | "speech_end" | "stt_complete" | "tts_first_byte"
#        | "tts_complete" | "barge_in" | custom string
# metrics: keys must intersect config.voice_metrics
# audio_wav: optional WAV bytes — uploaded to S3, NOT attached to LangSmith
```

- Emits `voice_event` channel as an OTel Span (kind=internal) under the parent request span.
- If `audio_wav` is provided AND `audio_s3` is in routing:
  1. Computes deterministic S3 key from `agent_id / date / session_id / turn_id / event` (see §8.6 for layout).
  2. **Async** uploads WAV to S3 (does not block verb return).
  3. **Sync** inserts a row into the `audio_index_pg` table with the S3 key + metadata.
  4. Adds `audio.s3_key` and `audio.duration_ms` as **OTel span attributes** so LangSmith / Datadog can show the pointer (and follow the signed URL on click).
- Audio blob is **never sent to LangSmith** — it's referenced by S3 key. LangSmith UI plugins (or our observability frontend) resolve the key to a signed URL on demand.

### 6.6 `obs.finish_request(req, *, status="success", outputs=None, error=None) -> None`

- Closes LangSmith run with outputs / status.
- Closes Datadog APM span; sets error tag if `error` provided.
- Updates Propio DB row with final timing + status.
- Emits a `summary` event derived from accumulated child events:
  - `request_duration_ms`
  - `tool_calls_count`
  - `voice_events_count`
  - `llm_tokens_total`
  - quality scores collected during the request

### 6.7 Helpers (not core verbs but shipped with SDK)

```python
# Wraps OpenAI / AsyncOpenAI client so all .chat.completions.create() calls
# auto-emit `llm_trace` events. Internally uses langsmith.wrappers.wrap_openai
# when LangSmith is in the routing for llm_trace.
client = obs.wrap_llm_client(AsyncOpenAI())

# LangChain callback handler — drop into AgentExecutor / Chain config.
agent = AgentExecutor(...).with_config({"callbacks": [obs.langchain_callback()]})

# OpenAI Realtime API: subscribe to a websocket and translate events to verbs.
obs.attach_openai_realtime(req, openai_ws)

# Manual structured log (alternative to logger.info, when you want explicit channel)
obs.log(req, level="info", message="user said X", **kwargs)
```

---

## 7. Channel Routing System

### Concept

Every event the SDK emits internally carries a `channel` string. The router consults `config.routing[channel]` to find a list of backend names. It then dispatches to each named backend's exporter.

```python
# Router (internal, simplified)
class Router:
    def __init__(self, routing: dict[str, list[str]], exporters: dict[str, Exporter]):
        self.routing = routing
        self.exporters = exporters

    def emit(self, channel: str, event: Event) -> None:
        targets = self.routing.get(channel, [])
        for name in targets:
            exporter = self.exporters.get(name)
            if exporter is None:
                continue  # backend disabled — skip silently
            self._submit(exporter, event)  # → async queue

    def _submit(self, exporter: Exporter, event: Event) -> None:
        try:
            self.queue.put_nowait((exporter, event))
        except Full:
            # Drop oldest — backpressure policy
            self.metrics.incr("propio_obs.dropped")
```

### Examples — selective routing

A team that wants *only* LangSmith for LLM, *only* Datadog for logs, *both* for voice:

```yaml
routing:
  llm_trace:    [langsmith]
  tool_call:    [langsmith]
  voice_event:  [langsmith, propio_db]
  log:          [datadog_logs]
  apm_span:     [datadog_apm]
```

A team that wants Datadog LLM Observability *instead of* LangSmith (alternative):

```yaml
routing:
  llm_trace:    [datadog_llm_obs]
  tool_call:    [datadog_llm_obs]
  voice_event:  [datadog_llm_obs, propio_db]
  log:          [datadog_logs]
```

A team that wants *everything mirrored everywhere* (overkill but possible):

```yaml
routing:
  llm_trace:    [langsmith, datadog_apm, propio_db]
  voice_event:  [langsmith, datadog_apm, propio_db]
  ...
```

### What routing does NOT do

- Does not transform events between formats — each exporter handles its own translation.
- Does not aggregate or rollup — that's the backend's job.
- Does not retry on failure beyond the queue worker's per-event attempt (we assume the backend has its own buffering / SLA).

---

## 8. Backend Adapters

In v1 there are **two kinds** of "exporters":

1. **OTel exporter** (single, default for almost every channel) — emits OpenTelemetry spans / logs / metrics over OTLP. The OTel Collector decides downstream destinations.
2. **Out-of-band exporters** — used for things OTel can't carry well: audio blobs (`audio_s3`), the metadata index (`audio_index_pg`), and an event-mirror DB for the Propio monitor UI (`propio_db`).

```python
class Exporter(Protocol):
    name: str
    def setup(self, cfg: BackendConfig) -> None: ...
    def emit(self, event: Event) -> None: ...
    def shutdown(self) -> None: ...
```

### 8.1 OTel exporter (primary)

Wraps `opentelemetry-sdk` and `opentelemetry-exporter-otlp`. At `init_agent()`:

1. `Resource` is constructed with: `service.name`, `agent.id`, `agent.type`, `deployment.environment`, `team`, plus `default_tags`.
2. `TracerProvider` + `BatchSpanProcessor(OTLPSpanExporter(endpoint=cfg.otel.endpoint))` registered globally.
3. `LoggerProvider` + `BatchLogRecordProcessor` registered → stdlib logging bridged to OTel logs.
4. `MeterProvider` + periodic exporter for OTel metrics.
5. **Auto-instrumentation** registered for httpx / FastAPI / OpenAI (via `traceloop-sdk`'s OpenLLMetry).

Verb → OTel mapping:

| Verb / Channel | OTel signal |
|---|---|
| `start_request()` | Creates a `Span` (kind=server, name=`agent.request`); span context becomes the active context |
| `record_tool()` | Child `Span` (name=`tool.<name>`, kind=internal) |
| `record_voice_event()` | Child `Span` (name=`voice.<event>`, kind=internal); `audio.s3_key` attribute attached if applicable |
| `record_quality()` | Span attribute on the parent request span: `quality.<metric>` |
| `obs.log(req, ...)` | OTel `LogRecord` carrying `request_id` / `trace_id` |
| `metric` | OTel `Counter` / `Histogram` |
| `finish_request()` | Span ended with status (OK / ERROR), outputs as attributes |

Standard attributes set on every span: `agent.id`, `agent.type`, `service.name`, `deployment.environment`, `team`, `request_id`, `session_id`, `customer_id` (when applicable), `turn_id` (when applicable).

**OTel Collector config** (deployed separately, not per-agent):

```yaml
# /etc/otel-collector-config.yaml
receivers:
  otlp:
    protocols: { grpc: { endpoint: 0.0.0.0:4317 } }

processors:
  batch:
  attributes/redact:
    actions:
      - { key: prompt, action: hash }
      - { key: response, action: hash }   # optional, env-dependent

exporters:
  otlp/langsmith:
    endpoint: api.smith.langchain.com:443
    headers: { x-api-key: "${LANGSMITH_API_KEY}" }
  datadog:
    api: { key: "${DD_API_KEY}", site: datadoghq.com }

service:
  pipelines:
    traces:
      receivers: [otlp]
      processors: [batch, attributes/redact]
      exporters: [otlp/langsmith, datadog]
    logs:
      receivers: [otlp]
      processors: [batch]
      exporters: [datadog]
    metrics:
      receivers: [otlp]
      processors: [batch]
      exporters: [datadog]
```

This is operational config — owned by ops/SRE, not the agent team. Adding a new backend (e.g. Langfuse, New Relic) is a Collector-side change; **agent code unchanged**.

### 8.2 LangSmith — special concerns under OTel

LangSmith natively accepts OTel via OTLP, so the OTel exporter handles it transparently. **However** there are 2 things the SDK still needs the native LangSmith client for:

1. **Fetching evaluator scores** (the scheduled evaluator pattern — see §15.3). The observability platform calls `Client.list_runs(...)` + `Client.list_feedback(...)` on a schedule and writes scores back into our analytics layer.
2. **Project bootstrap** (creating the LangSmith project on first agent init — deferred to §13).

Audio is **not** attached to LangSmith runs — only the S3 key is referenced via the OTel `audio.s3_key` attribute. The LangSmith UI shows the key; clicking it opens a presigned URL the SDK serves (or the observability frontend serves).

### 8.3 Datadog — OTel-native, no `ddtrace` required

In v1 we use the Datadog OTLP ingest endpoint (Datadog supports it natively). No `ddtrace` library at the SDK layer. This means:

- No `patch_all()` call. Auto-instrumentation comes from OTel instrumentation packages instead (`opentelemetry-instrumentation-fastapi`, `opentelemetry-instrumentation-httpx`).
- Datadog Logs received via OTel Logs pipeline (Collector → Datadog OTLP).
- Custom metrics via OTel Metrics → Collector → Datadog.
- LLM-specific UI (Datadog LLM Observability) requires `ddtrace.llmobs`. **Skipped in v1** — LangSmith covers that need; can be added later as a parallel path if a team requests it.

### 8.4 Audio capture & upload — out-of-band path (S3 + Postgres)

This is the most distinctive part of v1's design. Audio is uploaded to S3 directly, with a metadata pointer in Postgres, and only an **S3 key** carried inside the OTel span.

#### 8.4.1 Why not chunk-by-chunk to S3

- Voice chunks are ~256ms / 8 KB. A day of agents = several million tiny PUTs.
- S3 PUT pricing ($0.005 / 1k) and small-file metadata overhead make this expensive.
- S3 Multipart Upload requires ≥5 MB parts (except final) — chunks don't fit.
- The live monitor already broadcasts audio via WebSocket; persistence doesn't need sub-second granularity.

#### 8.4.2 Trigger timing (per voice turn)

```
turn_start  → buffer cleared
   ...      → chunks accumulated in memory
turn_end    → user_audio_capture frozen      → ① async S3 upload of user.wav
LLM produces text
TTS streams chunks → agent_audio_parts grows
audio_complete → joined into one WAV         → ② async S3 upload of agent.wav
                                             → ③ sync INSERT into audio_recordings (Postgres)
```

Steps ① and ② run as `asyncio.create_task(...)` so they don't block the next turn. Step ③ is **sync** because Postgres write is fast (1–5 ms) and we want the metadata row to exist *before* the OTel span carrying its key flushes. If PG insert fails, we still set the OTel attribute; the audio is recoverable via S3 enumeration as a fallback.

#### 8.4.3 Three persistence options compared

| Option | Live chunk push | **Per-turn async upload ✅** | Daily batch upload |
|---|---|---|---|
| User-perceived latency | ↑ (blocks WS write) | **0** (background task) | 0 |
| S3 objects/day | hundreds of thousands | **thousands** | one archive |
| Crash data loss | minimal | at most one turn | up to one day |
| Live playback | possible but pointless | turn-end + ~1s | next day only |
| Cost | high (PUT + small files) | low | lowest, ops-heavy |
| Implementation complexity | high (multipart) | **low** (single put_object) | medium (queue + scheduler) |

**v1 uses the middle column.** Live monitor goes through the existing WebSocket path (unchanged). S3 persistence happens once per turn; PG row written synchronously per turn. The two pipelines are decoupled — live UX doesn't depend on S3, and S3 doesn't block UX.

#### 8.4.4 S3 layout

```
s3://agent-recordings/
└── {agent_id}/                       # e.g. propio-agent
    └── {YYYY-MM-DD}/                 # UTC date
        └── sessions/{session_id}/
            └── turns/{turn_id}/
                ├── user.wav          # ~1-30s, 32-960KB
                ├── agent.wav
                └── meta.json         # transcript + latency snapshot + provider versions
```

- `{agent_id}` prefix isolates agents — adding a new agent doesn't collide.
- Date partition supports Athena / S3 Select queries and Lifecycle policies (e.g. "30d Standard → 90d IA → 1y Glacier → delete").
- `turn_id` in the key makes upload idempotent — retries are safe.
- Optional Opus encoding (~10× smaller) is a follow-up; v1 keeps WAV for simplicity.

#### 8.4.5 Why Postgres index + S3 blob (not just S3)

User asked: "先发给 postgresql？还是 batch 发给 s3？去 s3 取很慢的吧"

**Both — they have different jobs.**

| Concern | Postgres (`audio_index_pg`) | S3 (`audio_s3`) |
|---|---|---|
| Query "all audio for session X" | indexed scan, <10ms | `LIST` is slow + paginated, $0.005/1k requests |
| Write per turn | sync INSERT, 1-5ms | async PUT, 50-500ms |
| Storage cost | $0.10/GB-month | $0.023/GB-month |
| Holds the bytes | no — only metadata | yes |
| Observability platform's primary read | **yes** (fast) | only when user clicks "play" |

The pattern: **PG = index (fast, queryable), S3 = blob (cheap, large, slow scan)**.

Postgres schema:

```sql
CREATE TABLE audio_recordings (
    id BIGSERIAL PRIMARY KEY,
    request_id    TEXT NOT NULL,
    session_id    TEXT NOT NULL,
    turn_id       TEXT NOT NULL,
    customer_id   TEXT,
    agent_id      TEXT NOT NULL,
    role          TEXT NOT NULL,            -- 'user' | 'agent'
    s3_bucket     TEXT NOT NULL,
    s3_key        TEXT NOT NULL,
    duration_ms   INTEGER,
    bytes         INTEGER,
    sample_rate   INTEGER,
    transcript    TEXT,                     -- for fast text search without S3 fetch
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_audio_session ON audio_recordings (session_id, created_at);
CREATE INDEX idx_audio_request ON audio_recordings (request_id);
CREATE INDEX idx_audio_customer ON audio_recordings (customer_id, created_at);
```

Observability platform query path:
1. Query PG by `session_id` / `customer_id` / time range → list of rows with S3 keys.
2. UI renders metadata (duration, transcript, latency).
3. User clicks "play" → backend signs an S3 URL (`s3.generate_presigned_url`, TTL 60s) → browser plays.

This keeps the platform fast (no S3 LIST) while audio storage stays cheap.

### 8.5 Propio internal DB — event mirror (separate from audio index)

Distinct from `audio_index_pg`. This is the existing `monitor_logs.db` that powers the realtime monitor frontend. The schema we already have is reused:

```sql
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    start_time TEXT NOT NULL,
    end_time TEXT,
    is_active BOOLEAN NOT NULL DEFAULT 1,
    config_json TEXT
);
CREATE TABLE IF NOT EXISTS logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
```

This DB is operationally local to the agent (good for live debugging). It is **not** the source of truth for analytics — that's the OTel pipeline. v1 keeps it because the existing realtime monitor depends on it.

---

## 9. Standard Schema (Unified Data Model)

This is the heart of Layer B (§3.5). Every event, span, log, and summary row carries the same field names with the same semantics across every agent and every backend. **This is the single most important standardization** — without it cross-agent dashboards are impossible.

### 9.1 Required correlation keys (every event)

These are minted by the SDK and propagated automatically through OTel context, S3 paths, and PG rows. **Every** OTel span, log, and metric, **every** S3 object key, and **every** PG row carries these:

| Field | Source | Example | Notes |
|---|---|---|---|
| `agent_id` | config (`agent.agent_id`) | `support_voice` | machine-readable, stable |
| `agent_name` | config (`agent.agent_name`) | `Support Voice Agent` | human-readable |
| `agent_type` | config | `chat` / `voice` / `batch` / `workflow` | filter dimension |
| `modality` | config | `text` / `voice` / `multimodal` | filter dimension |
| `service` | config | `agent-gateway` | matches Datadog `service` tag |
| `version` | config or env (`PROPIO_AGENT_VERSION`) | `2026.04.27-abc1234` | deploy version — used by Datadog `version` tag for versioned APM |
| `env` | config (`default_tags.env`) | `prod` / `staging` / `dev` | matches Datadog `env` tag |
| `region` | config or env (`AWS_REGION`) | `us-east-1` | infra dimension |
| `team` | config (`default_tags.team`) | `ai-platform` | ownership |
| `tenant_id` | runtime (`start_request(metadata={"tenant_id": ...})`) | `hospital_a` | propagated by SDK; aliased to `customer_id` in older docs |
| `session_id` | runtime (auth layer) | uuid | one user session |
| `conversation_id` | runtime (SDK-minted) | uuid | one logical dialog within a session |
| `request_id` | SDK-minted | uuid | one `start_request → finish_request` |
| `turn_id` | SDK-minted on first voice/chat turn | uuid | sub-request granularity |
| `trace_id` | derived from `request_id` | 128-bit hex | OTel-compatible — joins LangSmith + Datadog + summary store |
| `user_id_hash` | runtime (auth layer); **already hashed** | sha256 prefix | never raw user_id — privacy |

> **Note on `tenant_id` vs `customer_id`**: same concept. Earlier sections used `customer_id`; we standardize on `tenant_id` going forward (it's the more common term in multi-tenant SaaS literature). The SDK accepts both and aliases `customer_id → tenant_id` for backward compat.

### 9.2 Domain-specific keys (recorded when applicable)

| Field | Set on | Example | Notes |
|---|---|---|---|
| `model_provider` | LLM call spans | `openai` / `anthropic` / `mistral` | always paired with `model_name` |
| `model_name` | LLM call spans | `gpt-4o` / `claude-3-7-sonnet` | exact model used |
| `model_version` | LLM call spans (when provider exposes) | `gpt-4o-2024-08-06` | for A/B comparisons |
| `tool_name` | tool spans | `search_docs` | one tool call = one span |
| `workflow_name` | multi-step agent runs | `appointment_booking_v2` | identifies a named pipeline |
| `stt_provider` / `stt_model` | voice events | `deepgram` / `flux-general-en` | mirrors propio's existing fields |
| `tts_provider` / `tts_model` / `tts_voice` | voice events | `elevenlabs` / `eleven_multilingual_v2` / `Adam` | same |
| `audio.s3_key` | voice events with audio | `propio-agent/2026-04-27/...` | pointer, never bytes |
| `audio.duration_ms` | voice events with audio | `2300.0` | for quick filtering |

### 9.3 ID hierarchy (outer → inner)

```
agent_id  (static, from config)
└── customer_id    (which Propio enterprise customer this traffic belongs to)
    └── session_id  (one user session — typically one auth'd login or one WS connection)
        └── conversation_id  (one logical conversation within a session)
            └── request_id   (one start_request → finish_request)
                └── turn_id  (one voice/chat turn within a request)
                    └── trace_id (OTel 128-bit hex, derived from request_id)
```

| ID | Lifetime | Set by | Notes |
|---|---|---|---|
| `agent_id` | deployment | config (`agent.agent_id`) | static |
| `customer_id` | one session (cannot change mid-session) | passed to `start_request(metadata={"customer_id": ...})` first time, then implicit for the rest of the session | new field — added to data model in v1 (see below) |
| `session_id` | one user session | caller (auth layer / WS handshake) | a session can contain many conversations |
| `conversation_id` | one logical conversation | minted by SDK on first turn; rolls when conversation context resets (e.g. user starts a new topic) | new in v1 — replaces ad-hoc grouping |
| `request_id` | one `start_request` → `finish_request` | minted by SDK | UUID4 |
| `turn_id` | one turn (voice exchange or chat round-trip) | minted by SDK on `record_voice_event("turn_start")` or first child event | shared with audio S3 path |
| `trace_id` | one OTel trace (= one request) | derived from `request_id`, encoded as OTel 128-bit hex | enables cross-backend joins |

**Why customer_id is at the session level, not per-event**

Propio confirmed: **a session never switches customer mid-session**. The customer is determined when the user authenticates / the WebSocket opens. Across sessions a single user can be associated with different customers, so it must be passed in per session — but once set it's constant.

This means:
- `customer_id` is captured at `start_request` (first call of a session) and **propagated** automatically by the SDK to every subsequent verb in that session.
- v1 implementation: simple `session → customer_id` dict cache inside the SDK. Set by first `start_request(metadata={"customer_id": X})`; reused by later verbs that pass the same `session_id`.
- v2 will use OTel context propagation / Python `contextvars` to make this fully implicit (see §15.6).

**Why conversation_id**

A session = one user being logged in. Inside one session a user may have several distinct conversations (different topics, different threads, agent reset, etc.). Without `conversation_id` we lose the ability to compute "average turns per conversation" or to scope evaluators to a single coherent dialog. v1 mints it on `start_request` if not provided; agents can reset it explicitly (`obs.start_conversation(req)` helper).

**Schema changes**

The `audio_recordings` table (§8.4.5) and the OTel resource attributes both carry `customer_id` and `conversation_id` as first-class columns / attributes. The Propio internal DB's `sessions` table gets an additional column:

```sql
ALTER TABLE sessions ADD COLUMN customer_id TEXT;
CREATE INDEX idx_sessions_customer ON sessions (customer_id);
```

**Routing through the backends**

- **LangSmith / Datadog**: receive `customer_id`, `conversation_id`, `request_id`, `turn_id`, `trace_id` as OTel resource + span attributes. Dashboards filter and group by `customer_id`.
- **Postgres `audio_recordings`**: `customer_id` and `conversation_id` indexed for cheap "all audio from customer X in last 24h" queries.
- **Propio DB**: `customer_id` on the session row.

Result: a single click in LangSmith / Datadog / Propio dashboard can pivot to "everything for this customer" with one query each.

---

## 10. Usage Patterns by Agent Type

### 10.1 Voice agent (propio realtime)

```python
# main.py
import propio_obs as obs
obs.init_agent("observability.yml")

# voice_session.py
from openai import AsyncOpenAI

async def handle_voice_session(ws):
    req = obs.start_request(
        request_type="voice_turn",
        session_id=ws.session_id,
        metadata={
            "customer_id": ws.customer_id,   # propagated by SDK to all child events in session
            "caller_id": ws.caller_id,
        },
    )
    try:
        # User speech
        obs.record_voice_event(req, "speech_start")
        transcript, audio_wav = await stt.transcribe(audio_stream)
        obs.record_voice_event(
            req, "stt_complete",
            metrics={"asr_latency_ms": 280},
            audio_wav=audio_wav,                 # → async upload to S3, indexed in PG
        )

        # LLM (auto-traced via OpenLLMetry/OTel auto-instrumentation)
        client = obs.wrap_llm_client(AsyncOpenAI())
        resp = await client.chat.completions.create(model="gpt-4o", messages=[...])

        # TTS
        obs.record_voice_event(req, "tts_first_byte",
                               metrics={"first_audio_ms": 620})
        agent_audio = await tts.synth(resp.choices[0].message.content)
        obs.record_voice_event(req, "tts_complete", audio_wav=agent_audio)

        # No score computed here — LangSmith's scheduled evaluator will fill task_success later.
        # We can still record deterministic flags:
        obs.record_quality(req, "tool_returned_data", value=bool(resp.choices))

        obs.finish_request(req, status="success",
                           outputs={"response_text": resp.choices[0].message.content})
    except Exception as e:
        obs.finish_request(req, status="error", error=str(e))
        raise
```

### 10.2 LangChain agent

```python
import propio_obs as obs
from langchain.agents import AgentExecutor, create_openai_tools_agent

obs.init_agent("observability.yml")

# Single line of integration — handler converts LC callbacks to obs verbs.
agent = AgentExecutor(
    agent=create_openai_tools_agent(...),
    tools=[...],
).with_config({"callbacks": [obs.langchain_callback()]})

# Business code unchanged
result = agent.invoke({"input": "Find me a flight to NYC"})
```

`obs.langchain_callback()` returns a `BaseCallbackHandler` that:
- on `on_chain_start` → calls `start_request`
- on `on_tool_start` / `on_tool_end` → calls `record_tool`
- on `on_llm_start` / `on_llm_end` → emits `llm_trace`
- on `on_chain_end` / `on_chain_error` → calls `finish_request`

### 10.3 OpenAI Realtime agent

```python
import propio_obs as obs
import websockets

obs.init_agent("observability.yml")

async def realtime_session():
    req = obs.start_request(request_type="realtime_session")
    async with websockets.connect("wss://api.openai.com/v1/realtime?...") as ws:
        # Listener auto-translates Realtime events to verbs:
        #   input_audio_buffer.speech_started → record_voice_event("speech_start")
        #   conversation.item.input_audio_transcription.completed → record_voice_event("stt_complete")
        #   response.audio.delta → record_voice_event("tts_first_byte")  (first chunk only)
        #   response.done → emits llm_trace with usage tokens
        obs.attach_openai_realtime(req, ws)

        # Your app code does its thing
        await run_realtime_loop(ws)

    obs.finish_request(req)
```

### 10.4 Plain HTTP chatbot (no framework)

```python
import propio_obs as obs
from openai import AsyncOpenAI

obs.init_agent("observability.yml")
client = obs.wrap_llm_client(AsyncOpenAI())

@app.post("/chat")
async def chat(body: ChatRequest):
    req = obs.start_request(
        request_type="chat",
        session_id=body.session_id,
        inputs={"message": body.message},
    )
    try:
        resp = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": body.message}],
        )
        text = resp.choices[0].message.content
        obs.record_quality(req, "task_success", 1.0)
        obs.finish_request(req, outputs={"response": text})
        return {"response": text}
    except Exception as e:
        obs.finish_request(req, status="error", error=str(e))
        raise
```

---

## 10.5 Canonical Event Taxonomy

Every agent emits the same set of named events. This is what makes "average tool latency across all agents" or "how many barge-ins happened in voice agents this week" answerable from a single query.

### 10.5.1 Common events (all agent types)

| Event | Emitted by | Maps to | Required attributes |
|---|---|---|---|
| `request_started` | `start_request()` | OTel root span open | `request_id`, `session_id`, `tenant_id`, `agent_id`, `version`, `env`, `request_type` |
| `model_started` | LLM call wrap (auto via `wrap_llm_client`) | child OTel span open | `model_provider`, `model_name`, `prompt_tokens` (final), parent `request_id` |
| `model_finished` | LLM call wrap | child OTel span close | `completion_tokens`, `total_tokens`, `latency_ms`, `error?` |
| `tool_started` | `record_tool()` (start) | child OTel span open | `tool_name`, `input` |
| `tool_finished` | `record_tool()` (end) | child OTel span close | `output`, `latency_ms`, `error?` |
| `quality_scored` | `record_quality()` OR LangSmith evaluator pull | OTel span attribute / PG summary | `metric`, `value`, `source` (`inline` / `langsmith_evaluator`) |
| `request_finished` | `finish_request()` | OTel root span close | `status` (`success`/`error`/`interrupted`), `outputs`, `error?`, `duration_ms` |

These names are **fixed** in v1. Adding a new event requires SDK release.

### 10.5.2 Voice-specific events (`modality: voice`)

| Event | Emitted by | Required attributes |
|---|---|---|
| `asr_started` | first audio chunk received by STT | — |
| `asr_partial` | each interim transcript | `transcript_partial`, `latency_since_audio_ms` |
| `asr_finalized` | final transcript ready | `transcript`, `asr_latency_ms`, `audio.s3_key`, `audio.duration_ms` |
| `barge_in_detected` | user speaks while agent is talking | `agent_speaking_ms_at_interrupt` |
| `tts_started` | TTS request sent to provider | `tts_provider`, `tts_model`, `tts_voice`, `text_chars` |
| `audio_first_byte` | first TTS audio chunk emitted to client | `first_audio_ms` (TTFB from request_started) |
| `audio_playback_finished` | client finished playing agent audio | `audio.s3_key`, `audio.duration_ms` |
| `tts_finished` | TTS done generating | `tts_latency_ms`, `total_audio_bytes` |

Note: `barge_in_detected` + `audio_playback_finished` exist specifically for the **product metrics** in §11.5 (barge-in recovery rate, time-to-first-audio).

### 10.5.3 Why a fixed taxonomy

- **Cross-agent dashboards** require predictable event names. If voice agent A calls it `voice_first_audio` and voice agent B calls it `tts_started`, no shared dashboard works.
- **Default alert thresholds** ("alert if `audio_first_byte` p95 > 1.5s") only work if the event name is universal.
- **Onboarding cost is lower** — new team members don't invent names; they pick from the list.

If a team needs an event that's not in the list, the path is "propose addition to SDK" → next release. Do not ad-hoc add custom event names; OTel allows it but the platform won't recognize them in summaries.

---

## 11. Lifecycle & Threading

### Async export queue

- All `emit()` calls return immediately; events go into an `asyncio.Queue` (or `queue.Queue` for sync agents).
- A background worker (asyncio task or thread) drains the queue and calls each exporter's `emit()`.
- Each exporter call is wrapped with a per-event timeout (`export_timeout_ms`).
- On timeout or exception: increment `propio_obs.export_errors` counter, drop event, don't propagate.

### Backpressure

- Queue size capped at `behavior.export_queue_size` (default 1000).
- When full, **drop oldest** (FIFO) and increment `propio_obs.dropped` counter.
- Dropping is preferable to blocking — agent latency must not depend on backend health.

### Failure isolation

- One bad exporter (e.g. Datadog API down) does **not** affect the other exporters.
- Each exporter has its own try/except in the worker.
- If a backend's setup fails at `init_agent`, the SDK logs an error and disables that backend (other backends still work).

### Sampling — **100% across all channels in v1**

- Decided: every span / log / metric is sent. No sampling logic in v1.
- Rationale: cost model is unknown, and decisions made under uncertainty default to "keep everything". When we have one month of production data we'll re-evaluate (see §15.2).
- Configuration knob exists in `behavior.sampling` for forward compatibility, but defaults to `1.0` and shouldn't be touched until the cost model is built.
- If any channel becomes obviously expensive in prod (e.g. millions of `voice_event` spans/day), partial sampling is the first response — but `error` / `slow` requests will be force-kept via OTel tail-sampling at the Collector.

### Shutdown — atexit + explicit flush helper

- **Default mechanism: atexit.** SDK registers an `atexit.register(flush)` hook in `init_agent()`. Long-running servers (FastAPI, daemon processes, our Propio voice agent) shut down gracefully and the hook flushes the OTel batch processor + S3 upload queue with a 5s timeout.
- **Why not async variants for verbs**: Propio's actual agents are all long-running services. atexit is sufficient.
- **Escape hatch**: `obs.flush(timeout_ms=5000)` is exposed for short-lived contexts (Lambda, batch script, SIGKILL preparation). Caller invokes it explicitly when they know they're about to exit.
- Exporters' `shutdown()` is called in parallel from the atexit handler. OTel BatchSpanProcessor's `force_flush()` + S3 upload queue's `join()` both run with their own timeouts; one slow backend doesn't block the others.

---

## 11.5 Platform Metrics vs Product Metrics

A common antipattern: mixing infrastructure metrics ("error rate", "p95 latency") with product metrics ("task success rate", "user satisfaction") on the same dashboard, with the same alert thresholds. They have different audiences, different cadences, and different actions.

### 11.5.1 Platform metrics

**What**: technical health of the agent.
**Owner**: AI platform / SRE team.
**Cadence**: real-time alerting; minute-level granularity.
**Backed by**: Datadog (primary), with cross-agent rollups in our PG summary store.

| Metric | Definition | Alert example |
|---|---|---|
| `request_error_rate` | `count(status=error) / count(*)` per minute | > 1% sustained 5 min → page |
| `request_p50_latency` / `p95` / `p99` | `request_finished.duration_ms` | p95 > 5s sustained → warn |
| `model_p95_latency` | `model_finished.latency_ms` | p95 > 3s for any model → warn |
| `tool_error_rate` | `count(tool_finished where error) / count(tool_finished)` | > 5% per tool → warn |
| `llm_cost_per_request` | derived from `prompt_tokens + completion_tokens × pricing` | budget exceeded → notify |
| `dropped_export_count` | SDK internal — events lost to backpressure | > 0 → warn |
| `audio_upload_failure_rate` | S3 PUT failures / total attempts | > 0.1% → warn |

### 11.5.2 Product metrics

**What**: did the agent do its job well?
**Owner**: product / business / AI quality team.
**Cadence**: hourly / daily; weekly review.
**Backed by**: LangSmith evaluators (primary), aggregated into our PG summary store.

| Metric | Definition | Source |
|---|---|---|
| `task_success_rate` | LLM-judge or rubric eval on `request_finished.outputs` | LangSmith scheduled evaluator → pulled into PG |
| `escalation_rate` | `count(workflow_name=human_handoff) / count(*)` | derived from `workflow_name` in spans |
| `user_satisfaction` | post-call survey or sentiment analysis | external integration → PG |
| `first_audio_latency_p50` | `audio_first_byte.first_audio_ms` p50 | voice events |
| `barge_in_recovery_rate` | `count(barge_in_detected followed by valid response)` / `count(barge_in_detected)` | voice events + LLM event correlation |
| `answer_grounded_rate` | LLM-judge eval on grounding | LangSmith evaluator |
| `conversation_length_p50` | turns per `conversation_id` | summary store |

### 11.5.3 Why this distinction is enforced in the SDK

- `record_quality()` is **only** for product metrics. Latency / error / cost are emitted automatically as platform metrics; agents don't call `record_quality(metric="latency", ...)`.
- The `quality_metrics` field in `observability.yml` is the **list of product metrics** the agent commits to producing. Anything not in that list is rejected by `record_quality()` (warning logged).
- Default dashboard templates (§13) ship as **two sets**: one platform dashboard per agent, one product dashboard per agent. Different audience, different thresholds, different cadence.
- Default alert templates similarly split: platform alerts go to AI Platform / SRE oncall; product alerts (e.g. "task_success_rate dropped below 90%") go to product team's slack, **not** to oncall.

This separation prevents the most common operational failure mode: pages going off because product metrics dropped (not actionable in 15 min) and oncall ignoring them, then missing the actual platform incident underneath.

---

## 12. Migration Plan

### Phase 0 — Design review (now)
- Circulate this doc.
- Confirm verb signatures and channel taxonomy with stakeholders.
- Decide on internal pypi host.

### Phase 1 — In-repo prototype (1-2 weeks)
- Inside this propio repo, create `backend/obs_sdk/` directory.
- Implement: `init_agent`, `start_request`, `finish_request`, `record_voice_event`, `wrap_llm_client`.
- Adapters: LangSmith, Propio DB. (Skip Datadog initially.)
- Migrate `backend/app/services/tracing.py` to use the new SDK internally.
- All existing voice agent functionality unchanged from the user's POV.

### Phase 2 — Datadog adapters (1 week)
- Add `datadog_apm` and `datadog_logs` exporters.
- Add `bridge_python_logging`.
- Test on dev Datadog account.

### Phase 3 — Extract as standalone package (1 week)
- Move `backend/obs_sdk/` to a new repo `propio-obs-sdk/`.
- Set up CI: lint, typecheck, unit tests.
- Publish v0.1.0 to internal pypi.
- Replace propio's in-repo copy with `pip install propio-obs-sdk`.

### Phase 4 — Onboard 2nd agent (timeline depends on team)
- Pick the scheduling agent (or whichever is least risky).
- Author its `observability.yml`.
- Add `obs.init_agent()` + verb calls at lifecycle points.
- Validate dashboards / traces appear correctly.

### Phase 5 — Optional: OTel migration (deferred)
- Once 2-3 agents use the SDK, evaluate whether the underlying transport should be OpenTelemetry.
- OpenLLMetry already provides 40+ provider auto-instrumentations; would simplify maintenance.
- Public API (verbs) doesn't change.

---

## 13. Auto-Dashboard / Auto-Alert Templates (Phase 6+, deferred)

**Goal**: when `init_agent(config)` runs for a *new* agent (first time), the SDK calls each backend's admin API to provision standard dashboards and alerts. New agent → 5 dashboards + N alerts, no manual UI clicks.

### 13.1 The 5 dashboard templates

Each new agent gets these dashboards auto-provisioned. Datadog hosts the platform-flavored ones; LangSmith / our internal UI host the product-flavored ones.

| Template | Layer | Host | Key panels |
|---|---|---|---|
| **Reliability** | Platform | Datadog | error rate, request rate, top errors, dropped exports, audio upload failures |
| **Latency** | Platform | Datadog | p50/p95/p99 of `request_finished.duration_ms`, `model_finished.latency_ms`, `tool_finished.latency_ms`, `audio_first_byte.first_audio_ms` |
| **Quality** | Product | LangSmith UI (+ Agent Observability Platform summary store, future) | `task_success_rate`, `answer_grounded_rate`, `escalation_rate`, evaluator score histograms |
| **Cost** | Platform/Product | Datadog (+ PG summary fallback, future) | tokens / request, $/request, $/tenant, $/model_provider |
| **Voice** *(if `modality: voice`)* | Product | Datadog (+ Agent Observability Platform UI, future) | `first_audio_latency` p50/p95, `barge_in_recovery_rate`, `tts_latency` per voice, audio playback success |

All dashboards are templated by `agent_id`, `tenant_id`, `version` so a single dashboard shows all agents but can be filtered to one. Cross-agent dashboards reuse the same templates with no agent filter.

### 13.2 Default alert templates

| Alert | Layer | Source | Threshold | Routes to |
|---|---|---|---|---|
| Error rate spike | Platform | Datadog APM monitor | > 1% sustained 5 min | AI Platform oncall (PagerDuty) |
| p95 latency regression | Platform | Datadog APM monitor | p95 > 5s sustained 10 min | AI Platform oncall |
| Cost budget exceeded | Platform | Datadog metric monitor on `llm_cost_per_request × volume` | configured per agent | AI Platform + Finance Slack |
| Quality regression | Product | LangSmith feedback alert | task_success drops > 5pp WoW | Product team Slack (NOT oncall) |
| Audio upload failure | Platform | Datadog metric on `audio_upload_failure_rate` | > 0.1% sustained 15 min | AI Platform oncall |
| Evaluator failure rate | Quality infra | LangSmith alert | evaluator errors > 5% | AI Platform Slack |

Note the deliberate split between **oncall-paging** (platform, actionable in minutes) vs **Slack-notification** (product, actionable in days). See §11.5.

### 13.3 Provisioning APIs

**Datadog** — `POST /api/v1/dashboard` and `POST /api/v1/monitor` with JSON templates parameterized by `agent_id`. SDK keeps templates in `propio_obs/templates/datadog/*.json`.

**LangSmith** — `langsmith.Client.create_project(project_name=cfg.langsmith.project, metadata=...)`; evaluator definitions registered per `quality_metric` listed in config. Alerts via LangSmith's project-level alert API (with webhook into our incident channel).

**Agent Observability Platform UI** *(future)* — config row inserted into our admin DB; UI auto-discovers new agents from there. v1 does not ship this UI; the SDK still writes the admin DB row so the future UI has data when it arrives.

### 13.4 Why deferred to Phase 6+

- Templates evolve as we learn what to monitor. Locking them in v1 means rewriting them later anyway.
- Each backend's admin API is non-trivial; budget 2-3 weeks per backend to do it properly.
- Not a blocker for SDK adoption — manual dashboard setup for the first 2-3 agents is fine and informs the templates.

When ready, implement as `obs.bootstrap_backends(config, force=False)` — opt-in, idempotent. Running it on an existing agent updates dashboards in place (template versioning via dashboard tags).

---

## 14. Risks & Mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| **SDK becomes critical path** — backend latency affects agent | Med | High | Async queue with hard timeouts; never `await` exporter calls in verb path. Drop on backpressure. |
| **PII leakage** — audio / transcript / user inputs sent to SaaS | Med | High | `behavior.redaction.pii_fields`; pre-export hook for custom scrubbing; legal review before adding new backends. |
| **Cost blow-up** — paid backends bill per trace/log | Med | Med | Sampling per channel; production sampling defaults to 0.1 for non-critical channels (review with team). Track `propio_obs.exports_total` metric. |
| **Version drift** — LangSmith / ddtrace ship breaking changes | High | Med | Pin minor versions; SDK CI runs against latest + pinned. Adapter changes go in patch releases. |
| **Adapter complexity grows** — N agents × M backends edge cases | Med | Med | Strict adapter interface (Protocol). Each adapter has its own test fixture. |
| **Audio attachment size** — large WAVs slow exports | Low | Low | Cap at 60s per attachment; truncate or drop above that. Already implemented in propio (`_USER_AUDIO_MAX_BYTES = 2MB`). |
| **Misconfigured routing** — agent loses observability silently | Med | Med | `init_agent()` warns when a channel routes to no enabled backends. Health-check verb in SDK. |
| **Concurrent agent writes to Propio DB** — SQLite contention | Low | Low | Move Propio DB to Postgres in Phase 3+. SQLite OK for v0.1 prototype. |

---

## 15. Resolved Decisions & v2 Roadmap

The questions originally raised during design have been resolved. Recording the decisions + their rationale below so future contributors don't relitigate them.

### 15.1 Wire layer — **OpenTelemetry (resolved: yes, v1)**

**Decision**: v1 ships with OTel as the underlying transport. Agent emits OTel spans / logs / metrics; an OTel Collector fans out to LangSmith, Datadog, etc.

**Why**: Backend list is expected to grow (LangSmith + Datadog APM + Datadog Logs + Datadog Metrics + Propio DB already, and more on the horizon). Native adapters per backend would multiply maintenance. OTel + Collector keeps the SDK adapter count to 1, with backend choice deferred to ops config. LangSmith, Datadog, Langfuse, New Relic all natively accept OTLP.

**Trade-off accepted**: OTel's span model doesn't 1:1 match LangSmith's run tree; minor translation handled in Collector / our LangSmith viewer. Audio attachments aren't a standard OTel feature — solved by §8.4 (out-of-band S3 path).

### 15.2 Sampling — **100% in v1 (resolved)**

**Decision**: All channels sampled at 1.0. No production tuning yet.

**Why**: We don't have a cost model. The cheapest way to build one is to run unsampled for a month and see real numbers. Sampling now would obscure the data we need to make the sampling decision later.

**Future trigger for change**: when monthly LangSmith / Datadog bills exceed a threshold to be set after first month of prod, OR when `voice_event` channel exceeds X spans / day. Tail-sampling at the Collector for non-error / non-slow requests will be the first lever (keep everything that errored, sample successes).

### 15.3 Quality scoring — **LangSmith scheduled evaluators (resolved)**

**Decision**: Agents do NOT compute quality scores. The SDK's `record_quality()` exists for the rare case where a deterministic check yields a value (e.g. "tool returned non-null"), but the primary path is:

1. Agent emits the LLM trace (input + output) via OTel → LangSmith.
2. **LangSmith runs the evaluator on a schedule** (configured per LangSmith project — e.g. hourly, with model-as-judge or custom Python).
3. The observability platform pulls evaluator scores back via `langsmith.Client.list_feedback(...)` on a schedule.
4. Scores land in our analytics layer for dashboards / alerts.

**Why**: Three reasons.

1. **Decoupling**. Evaluators are slow (LLM-as-judge can take seconds). Running them inline blocks `finish_request` or competes for resources with the agent. Running them on LangSmith's schedule moves cost off the critical path.
2. **Centralized definitions**. "task_success" should mean the same thing across agents. Defining it once in a LangSmith project (vs once per agent codebase) reduces drift.
3. **No agent code changes when evaluator changes**. Tweaking an evaluator's prompt is a LangSmith UI change; agents keep running.

**Implication**: SDK has a small `quality_score_puller` component in v1 that periodically pulls feedback from LangSmith into our analytics DB. Not in critical path of agent execution.

### 15.4 Audio retention — **S3 lifecycle, NOT LangSmith (resolved)**

**Decision**: Audio is uploaded to S3 (per §8.4), not attached to LangSmith. Retention controlled by S3 Lifecycle policy (default: 30d Standard → 90d IA → 1y Glacier → delete).

**Why**: 

- LangSmith retention is controlled by LangChain's terms (and is unclear / variable). S3 lifecycle is fully owned by us.
- Privacy: audio is PII. With S3 we control deletion timing for GDPR / customer right-to-be-forgotten requests via S3 object delete.
- Cost: S3 Glacier is ~$0.004 / GB-month vs LangSmith bundled storage (priced per trace, not per byte).
- Auditability: who accessed the audio? S3 access logs + CloudTrail. LangSmith audit is more limited.

LangSmith only sees the **S3 key** as a span attribute (`audio.s3_key`), not the bytes. Our observability frontend signs presigned URLs on demand for playback.

### 15.5 Multi-tenant `customer_id` — **session-scoped, propagated by SDK (resolved)**

**Decision** (per Propio data team): customer is fixed per session and changes only across sessions. SDK accepts `customer_id` once via `start_request(metadata={"customer_id": X})` for the first call of a session and **propagates it automatically** to all subsequent verbs in that session.

**Why session-scoped, not per-event**:

- Propio confirmed sessions don't switch customers mid-flight. Asking the agent to repeat `customer_id` on every verb call is ceremony.
- A simple `{session_id: customer_id}` cache inside the SDK handles propagation. v1 implementation.

**Schema changes**: `customer_id` is added as a column in `audio_recordings`, as an index in the Propio DB `sessions` table, and as an OTel resource attribute on every span. See §9.

**Why not a `with obs.tags(customer_id=...)` block in v1**: That's a contextvar-based propagation pattern and is more flexible (works for any tag, not just customer_id). It's the right long-term design but adds context-management complexity that's not justified when v1 only has one tag of this kind. **Deferred to v2** — see 15.7.

### 15.6 Sync vs async API — **sync verbs + atexit (resolved)**

**Decision**: All verbs are sync, return immediately. SDK auto-registers an `atexit` hook to flush at process shutdown. `obs.flush(timeout_ms)` exposed as an explicit escape hatch.

**Why**: Propio's agents are all long-running servers (the voice gateway is a FastAPI process; future scheduling/support agents will be similar). Long-running processes go through `atexit` cleanly on `SIGTERM` / graceful shutdown — the hook fires and the OTel batch processor + S3 queue flush.

**When this fails**: SIGKILL, OOM kills, Lambda timeout. atexit doesn't run in those cases. The OTel BatchSpanProcessor flushes every 5s by default, so worst-case data loss is bounded to ~5s. Not perfect but acceptable for v1.

**Why not add async variants now**:

- Doubling the API surface (`record_tool` + `record_tool_async`) for a use case nobody currently has.
- Async-correctness is invasive — every helper, every test, every example doubles.
- Pythonic teams that need true async behavior can `await asyncio.to_thread(obs.flush)` if they really need it.

**See §15.7 for v2 plan if this assumption breaks.**

---

### 15.7 Deferred to v2 — items recorded with rationale

These are not problems for v1, but they will likely come up. Documented now so v1 isn't designed to make them harder.

#### 15.7.1 v2: async verb variants for high-QPS FastAPI agents

**What**: Add `record_tool_async`, `start_request_async`, etc. — coroutines that genuinely don't block any thread (vs the v1 sync verbs which are non-blocking but technically run in the calling thread).

**Why deferred**:

- v1's sync verbs return in microseconds (just append to in-memory queue). For typical agent workloads (≤100 RPS per process), that's not measurable.
- True async variants would need async OTel exporters end-to-end and async S3 uploads — `boto3` is sync, so we'd need `aioboto3`, which adds a dependency and another set of bugs.
- Doubles the API surface and the documentation. Premature.

**Trigger to revisit**: a team running an agent at >500 RPS per process reports SDK overhead in flame graphs. Until then, v1's "fast sync, batched async export" is correct.

**Implementation when needed**: The verb layer is thin (just dataclass construction + queue push). Async equivalents are mechanical to add. Won't break existing callers — v1 sync verbs stay.

#### 15.7.2 v2: contextvars for automatic tag propagation

**What**: Replace the explicit `metadata={"customer_id": X}` pattern with implicit propagation via Python `contextvars` (the same mechanism OTel uses for context):

```python
# v2 sketch
with obs.tags(customer_id="hospital_a", request_priority="high"):
    req = obs.start_request(...)
    obs.record_tool(req, ...)   # automatically tagged with customer_id + request_priority
    # ... all nested calls inherit
```

**Why deferred**:

- v1 only has one such tag (`customer_id`), and it's already propagated by the SDK's session cache. The full contextvar machinery is overkill for one tag.
- contextvars need careful integration with asyncio task spawning (each `asyncio.create_task` should copy the context — the stdlib does this, but third-party libraries sometimes break it).
- Teams building agents prefer explicitness in v1 — they know exactly what tags are on each event because they passed them in.

**Trigger to revisit**: when 3+ tags need cross-verb propagation, OR when a team complains about repetitive `metadata=` parameters.

**Implementation when needed**: 

1. Add `obs.tags(**kwargs) -> ContextManager` based on `contextvars.ContextVar`.
2. Each verb reads the current context and merges into the event before emit.
3. Existing explicit `metadata={...}` calls still work (they override / augment context).

Backward-compatible. v1 sets the foundation by giving the SDK a clean event-construction layer.

---

### 15.8 Items genuinely still open

Only two questions remain genuinely unresolved as of this revision:

1. **Conversation rotation policy** — when does `conversation_id` reset within a session? Options: (a) caller controls explicitly; (b) auto-reset after N minutes of inactivity; (c) auto-reset on agent context-clear. v1 ships with (a) only; (b) and (c) follow from product input.
2. **First evaluator definitions** — which evaluators do we configure on the first LangSmith project? (`task_success` LLM-judge, `answer_grounded` retrieval check, etc.) — needs product/AI team input on what to actually measure. Doesn't block SDK shipping; SDK accepts whatever scores LangSmith returns.

---

## 16. Repository Layout

```
propio-obs-sdk/
├── pyproject.toml
├── README.md
├── CHANGELOG.md
├── src/
│   └── propio_obs/
│       ├── __init__.py            # exports verbs + helpers
│       ├── api.py                 # the 6 verbs
│       ├── config.py              # AgentConfig (pydantic models)
│       ├── ids.py                 # request_id / trace_id minting
│       ├── request.py             # Request handle dataclass
│       ├── router.py              # channel → exporters dispatch
│       ├── queue.py               # async export queue worker
│       ├── redaction.py           # PII scrubber
│       ├── helpers/
│       │   ├── openai_wrap.py     # wrap_llm_client
│       │   ├── langchain_cb.py    # langchain_callback
│       │   └── openai_realtime.py # attach_openai_realtime
│       └── exporters/
│           ├── base.py            # Exporter Protocol
│           ├── langsmith.py
│           ├── datadog_apm.py
│           ├── datadog_logs.py
│           ├── datadog_metrics.py
│           ├── datadog_llm_obs.py # optional alternative to LangSmith
│           └── propio_db.py
└── tests/
    ├── unit/
    │   ├── test_config.py
    │   ├── test_router.py
    │   ├── test_queue.py
    │   └── exporters/
    │       ├── test_langsmith.py
    │       ├── test_datadog_*.py
    │       └── test_propio_db.py
    ├── integration/               # uses dev backends
    │   ├── test_voice_agent.py
    │   └── test_chat_agent.py
    └── fixtures/
        └── observability.example.yml
```

---

## 17. End-to-End Walkthrough

A new team builds a chat agent. Here's their full integration:

### Step 1 — Install

```bash
pip install propio-obs-sdk
```

### Step 2 — Author `observability.yml`

```yaml
agent:
  agent_id: docs_chat
  agent_type: chat_agent
  modality: text
  service: docs-bot
  default_tags:
    team: docs
    env: prod

quality_metrics: [task_success, answer_helpful]   # evaluated by LangSmith on schedule

otel:
  endpoint: http://localhost:4317
  service_name: docs-bot

backends:
  langsmith:
    enabled: true
    api_key_env: LANGSMITH_API_KEY
    project: docs-chat-prod
    fetch_evaluator_scores: true                  # pull scores back via REST

routing:
  llm_trace: [otel]
  tool_call: [otel]
  log:       [otel]
  # No voice_event / audio_s3 — text agent.
```

### Step 3 — Set env

```bash
export LANGSMITH_API_KEY=lsv2_...
export DD_API_KEY=dd_...
```

### Step 4 — Wire SDK in code

```python
import propio_obs as obs
from openai import AsyncOpenAI
import logging

obs.init_agent("observability.yml")
logger = logging.getLogger(__name__)
client = obs.wrap_llm_client(AsyncOpenAI())

@app.post("/ask")
async def ask(body: AskRequest):
    req = obs.start_request(
        request_type="docs_query",
        session_id=body.session_id,
        inputs={"question": body.question},
    )
    try:
        logger.info(f"answering {body.question[:50]}")  # → Datadog Logs

        # Tool: search docs
        results = search_docs(body.question)
        obs.record_tool(req, "search_docs",
                        input={"q": body.question},
                        output={"hits": len(results)})

        # LLM
        resp = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "Answer using these docs:\n" + str(results)},
                {"role": "user", "content": body.question},
            ],
        )
        text = resp.choices[0].message.content

        obs.record_quality(req, "task_success", 1.0)
        obs.finish_request(req, outputs={"answer": text})
        return {"answer": text}
    except Exception as e:
        logger.exception("ask failed")
        obs.finish_request(req, status="error", error=str(e))
        raise
```

### Step 5 — Observe

- **LangSmith** → `docs-chat-prod` project (via OTel Collector → OTLP): every request is a parent span with child spans for `search_docs` (tool) and the LLM call. Tokens, latency, prompt all visible. Scheduled evaluators populate `task_success` / `answer_helpful` scores hourly.
- **Datadog Logs** → search `service:docs-bot env:prod` (via OTel Collector → Datadog): every `logger.info` and `logger.exception` indexed with `request_id`, `agent_id`, `customer_id`, `trace_id` tags.
- **Agent Observability Platform** *(future, post-v1)* → will pull scores from LangSmith Feedback API on a schedule and surface in our internal analytics dashboard. In v1, score consumption happens directly in LangSmith UI.

Total integration: **1 yaml + 1 init + 4 verb calls**. No vendor SDK imports, no manual span management, no boilerplate.

---

## Appendix A — Verb Signatures (Type Reference)

```python
# src/propio_obs/api.py

from __future__ import annotations
from typing import Any, Optional, Union
from pathlib import Path

def init_agent(config: Union[str, Path, dict]) -> None: ...

def start_request(
    request_type: str,
    *,
    agent_id: Optional[str] = None,
    session_id: Optional[str] = None,
    user_id: Optional[str] = None,
    inputs: Optional[dict[str, Any]] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> Request: ...

def record_tool(
    request: Request,
    name: str,
    *,
    input: Optional[dict[str, Any]] = None,
    output: Optional[Any] = None,
    error: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> None: ...

def record_quality(
    request: Request,
    metric: str,
    value: Union[float, bool],
    *,
    comment: Optional[str] = None,
) -> None: ...

def record_voice_event(
    request: Request,
    event: str,
    *,
    metrics: Optional[dict[str, float]] = None,
    audio_wav: Optional[bytes] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> None: ...

def finish_request(
    request: Request,
    *,
    status: str = "success",      # "success" | "error" | "interrupted"
    outputs: Optional[dict[str, Any]] = None,
    error: Optional[str] = None,
) -> None: ...

# Helpers
def wrap_llm_client(client: Any) -> Any: ...
def langchain_callback() -> Any: ...      # returns BaseCallbackHandler
def attach_openai_realtime(request: Request, ws: Any) -> None: ...
def log(request: Request, level: str, message: str, **kwargs: Any) -> None: ...
def flush(timeout_ms: int = 5000) -> None: ...
```

---

## Appendix B — Event Schema (Internal)

Events flow from verbs → router → exporters. Internal canonical shape:

```python
@dataclass
class Event:
    channel: str                          # e.g. "llm_trace", "voice_event"
    event_type: str                       # e.g. "request_start", "tool_call", "speech_end"
    timestamp_ns: int                     # monotonic-ish, UTC ns
    agent_id: str
    request_id: str
    session_id: Optional[str]
    turn_id: Optional[str]
    trace_id: str                         # OTel-compatible 128-bit hex
    parent_id: Optional[str]              # for nested runs
    payload: dict[str, Any]               # channel-specific data
    attachments: dict[str, "Attachment"]  # name → (mime, bytes)
    tags: dict[str, str]                  # default_tags + per-event additions
```

Each exporter knows how to map this to its native format.

---

## Appendix C — Standardization Checklist

Five things every new agent inherits from the SDK without thinking about them. If any of these is missing, the SDK is mis-configured.

### ✅ 1. Unified naming
The SDK enforces these field names — agents cannot rename them:
- `agent_id`, `agent_name`, `agent_version`, `agent_type`, `modality`
- `service`, `version`, `env`, `region`, `team`
- `tenant_id`, `session_id`, `conversation_id`, `request_id`, `turn_id`, `trace_id`
- `model_provider`, `model_name`, `model_version`
- `tool_name`, `workflow_name`
- `user_id_hash` (never raw user_id)

(Full table in §9.)

### ✅ 2. Unified event schema
Fixed event taxonomy across all agents:
- Common: `request_started`, `model_started`, `model_finished`, `tool_started`, `tool_finished`, `quality_scored`, `request_finished`
- Voice add-ons: `asr_started`, `asr_partial`, `asr_finalized`, `barge_in_detected`, `tts_started`, `audio_first_byte`, `audio_playback_finished`, `tts_finished`

(Full table in §10.5.)

### ✅ 3. Default dashboard templates
Every agent auto-provisions 5 dashboards on `init_agent()`:
- Reliability (Datadog)
- Latency (Datadog)
- Quality (LangSmith + summary store)
- Cost (Datadog + summary store)
- Voice — only if `modality: voice` (mixed)

(Full spec in §13.1.)

### ✅ 4. Default alert templates
Split by audience and urgency:
- **Oncall-paging** (Platform): error rate, p95 latency, audio upload failure
- **Slack-notification** (Product): quality regression, evaluator failure, cost budget

(Full spec in §13.2.)

### ✅ 5. Platform vs Product metric separation
- Platform metrics = tech health → Datadog, oncall, minute granularity
- Product metrics = business outcomes → LangSmith + summary store, product team, daily/weekly cadence
- `record_quality()` accepts only product metrics; platform metrics emitted automatically by SDK

(Full rationale in §11.5.)

### How to verify a new agent passes the standardization bar

```bash
# After `init_agent()` runs in a new agent, run:
python -m propio_obs.lint observability.yml
```

Expected output:

```
✓ All required correlation keys present in default_tags
✓ quality_metrics list contains only product metrics (no platform metrics)
✓ Provisioned 5 Datadog dashboards (reliability, latency, cost, voice, custom)
✓ Provisioned LangSmith project with evaluators: task_success, answer_grounded
✓ Standard alert templates registered (3 platform, 2 product)
✓ Schema lint clean
```

If any check fails, the agent is not production-eligible.

---

## Appendix D — Glossary

| Term | Meaning |
|---|---|
| **Verb** | A high-level action function exposed by the SDK (`init_agent`, `start_request`, etc.). Named "verb" because they are imperative actions, by analogy with REST HTTP verbs. |
| **Channel** | A logical category of event (`llm_trace`, `tool_call`, `log`, ...). The routing config maps channels to backends. |
| **Backend** | A destination for events (LangSmith, Datadog APM, Datadog Logs, Propio DB, ...). |
| **Exporter** | The SDK adapter for one backend. One exporter = one backend. |
| **Fan-out** | Sending one event to multiple backends in parallel. |
| **OpenTelemetry (OTel)** | A vendor-neutral observability spec + SDK. Many backends accept its OTLP protocol. We may adopt it as the SDK's transport layer in v2. |
| **OTLP** | OpenTelemetry Protocol — the wire format for OTel data. |
| **Run tree** | LangSmith concept — hierarchical record of an LLM application run, with parent / child runs for nested calls. |
| **Span** | OTel / APM concept — a unit of timed work. Has parent / child relationships forming a trace. |
| **Trace** | The complete tree of spans for one logical request. |
| **Attachment** | Binary blob (audio, image, file) attached to a trace/run, viewable inline in the backend UI. |
