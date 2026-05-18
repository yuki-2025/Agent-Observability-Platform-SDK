# Propio Observability SDK — 实施方案

> 状态：**草案 / 实施前**，团队评审稿。
> 目标包名：`propio-obs-sdk`
> 作者：AI Platform / 由 propio voice agent 现有 observability 代码作为骨架。

---

## 1. 总览

### 1.1 v1 交付物 — `propio-obs-sdk`

**v1 只交付一个 Python SDK。** 没有新平台、没有新 UI、没有新后端服务。

```bash
pip install propio-obs-sdk
```

每个新 agent import 这个 SDK，写一份 YAML，调用 6 个 verb。剩下的事 SDK 全包：

- LLM trace 发到 **LangSmith**
- APM span / 日志 / metric 发到 **Datadog**
- 语音音频上传到 **S3**（metadata index 在 **Postgres**）
- 事件镜像到 **propio 现有的 monitor DB**

**Agent 不再 import `langsmith` / `ddtrace` / `boto3` / `asyncpg`**。只有 SDK 接触这些库。一个依赖、一份配置、三个后端全覆盖。

### 1.2 设计原则 — Thin Orchestration，不重复造轮子

SDK 是**管道**。不去重做 LangSmith / Datadog 已经做好的功能。具体来说：

- ❌ SDK **不**自带 trace UI（用 LangSmith 自带的，比我们 6 个月做的都好）
- ❌ SDK **不**自带 APM viewer（用 Datadog 的）
- ❌ SDK **不**自带 LLM evaluator 引擎（LangSmith 自带 scheduled evaluator；SDK 通过 API 把分数拉回来即可）
- ❌ SDK **不**自带 alert 引擎（LangSmith / Datadog 各有；告警走同一个 incident channel）
- ✅ SDK **强制统一 schema**，让三个后端都带相同的关联键（`request_id`、`tenant_id`、`agent_id`、`version` 等）
- ✅ SDK **路由数据**到对的后端（按 per-agent YAML）
- ✅ SDK **自动 provision** 默认 dashboard / alert（Phase 6+）

就这些。SDK 是**跨 agent 标准化的执行机制**，不是新产品线。

### 1.3 为什么这件事重要

今天每个 agent team 各自接 LangSmith / Datadog / 内部 DB，**ID 不一致、metadata 不一致、dashboard 不一致**。结果是「过去一周 hospital_a 在所有 agent 上的 p95 first-audio 延迟」这种跨 agent / 跨 tenant 的问题**根本没法回答**——数据在每个后端 shape 都不一样。SDK 通过**强制所有 agent 用同一条路径**发数据来解决这个问题。

### 1.4 Verb 接口

一个 Python 包，~6 个 high-level verb：`init_agent`、`start_request`、`record_tool`、`record_quality`、`record_voice_event`、`finish_request`，外加 helper（`wrap_llm_client`、`langchain_callback`、`attach_openai_realtime`）。内部用 **OpenTelemetry** 发 spans / logs / metrics，OTel Collector 做 fan-out。音频走 out-of-band 路径直接传 S3，metadata 索引存 Postgres。

**Selective routing 是核心特性**：每个 agent 可以决定 LLM trace **只**发 LangSmith、log **只**发 Datadog、voice event 同时发 LangSmith + Propio DB。SDK 不强制每个后端都收所有事件。

### 1.5 第一阶段交付

先在 propio voice agent repo 内验证设计（现在 `backend/app/services/tracing.py` 已经做了 ~30% 的 LangSmith 部分），然后抽取成独立 package，第二个 agent 接入。

### 1.6 v1 不做的事（Future Work — **不在 v1 范围**）

以下都属于**远期**「Agent Observability Platform」愿景，等 SDK 在 2-3 个 agent 上跑稳之后再考虑：

- **跨 agent / 跨 tenant 的汇总分析存储**（Postgres）—— LangSmith 和 Datadog 都没法便宜地回答这种查询
- **统一钻取 hub UI** —— 一个 URL（按 `request_id`）跳进 LangSmith trace + Datadog APM trace + 音频回放 + 评估分
- **统一 incident 收件箱** —— 把 LangSmith 和 Datadog 的告警合到一处

这些有意思，但**v1 不做**。v1 只是 SDK。

---

## 2. 目标 / 不做的事

### 目标

1. **统一接口**：任何新 agent 都调用相同的 6 个 verb，不管用什么 LLM provider、什么 framework、什么 agent 类型（voice / chat / multi-agent）
2. **多后端 fan-out**：写一遍代码，N 个后端都看到。加新后端是改 config，不是改代码
3. **Selective routing**：per-agent config 决定每个 channel 发到哪些后端
4. **零侵入默认值**：`init_agent()` 读 YAML 后自动配 OTel auto-patch、桥接 Python `logging`、设置 LangSmith env vars。原有的 `logger.info()` 和 `httpx`/`openai` 调用自动有观测，不改业务代码
5. **失败隔离**：某个后端挂掉不能影响 agent。后台队列异步发，有超时
6. **标准 metadata**：每个事件都带 `agent_id`、`agent_type`、`service`、`env`、`team`、`request_id`、`session_id`、`turn_id`。跨 agent dashboard 才能成立
7. **音频支持**：voice agent 把 user / agent 音频直接传 S3，OTel span 里只放 S3 key 引用

### 不做（v1）

- **不**替代 LangChain / OpenAI SDK / Pipecat —— SDK 是放在它们旁边的
- **不**做 vendor lock-in。v1 用 OpenTelemetry，换后端只改 Collector config
- **不**自带 evaluator engine。`record_quality()` 接受预先算好的分数，不跑评估
- **不**自动建 dashboard（v1 延后，见 §13）
- **不**多语言。Python only for v1

---

## 3. 整体架构

**v1 wire format：OpenTelemetry**。Agent 发 OTel spans / logs / metrics；OTel Collector（in-process 或 sidecar）做 fan-out 到各后端。**音频走 out-of-band**——直接传 S3，metadata 存 Postgres，OTel span 里只放 S3 key。

```
┌─────────────────────────────────────────────────────────────┐
│              Agent code（任何 framework）                    │
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
                  │  verb 层                 │
                  │  ↓                      │
                  │  channel router         │
                  │  ├─→ OTel emit          │
                  │  └─→ S3 audio uploader  │ (仅 audio channel)
                  └─┬───────────────────┬───┘
                    │                   │
       ┌────────────▼─────────┐    ┌────▼──────────────┐
       │   OTel Collector     │    │  S3（音频 blob）   │
       │  (per-process 或     │    │  + Postgres       │
       │   sidecar)           │    │  (metadata index) │
       └─┬─────┬─────┬────────┘    └───────────────────┘
         │     │     │
   ┌─────▼┐ ┌──▼──┐ ┌▼──────────┐
   │Lang- │ │ DD  │ │ Propio DB │
   │Smith │ │     │ │ (events)  │
   │OTLP  │ │OTLP │ │ Postgres  │
   └──────┘ └─────┘ └───────────┘
```

**关键设计决定：**

- **OTel 是 wire 格式**。SDK 产生 OTel `Span` / `Log` / `Metric`，Collector 决定每个发哪
- **音频不走 OTel attachment**。OTel 没成熟的二进制附件方案。音频直接传 S3，OTel span 只带 `audio.s3_key` 属性
- **Postgres 做音频 metadata 索引**。observability 平台查 PG（快、有索引）；只在用户点「播放」时才从 S3 拿 WAV（presigned URL）
- **In-process SDK**。Agent 直接 import。OTel Collector 可以 in-process 也可以 sidecar，v1 默认 in-process
- **Verb 层是唯一公开 API**。Channel router 和 OTel 内部不暴露
- **异步导出队列**。Verb 立即返回；OTel batch span processor + S3 upload 都在后台跑
- **Channel-based routing**。事件按 *channel* 打标（`llm_trace`、`tool_call`、`log` 等）；router 决定要不要发 OTel / S3 / PG

---

## 3.5 分层架构 & 分工

> **v1 范围说明**：v1 **只**交付 Layer A（`propio-obs-sdk` 这个 package）。Layer B 是 SDK 在 emit 时强制执行的数据合约——不是单独组件。Layer C（**Agent Observability Platform** UI，做跨 agent 分析、钻取、音频回放）是**未来工作**，**不在 v1 范围**。v1 阶段的「消费」就是直接用 LangSmith UI + Datadog UI。

长期愿景下系统是 **3 层**。每层一个职责，已有工具承担大部分工作。

```
                     ┌──────────────────────────────────────┐
                     │  Layer C: 消费 / UI                   │
                     │  ────────────────────────────────    │
                     │  • LangSmith UI    (LLM trace 深挖, 
                     │                     evaluator scores,
                     │                     thread view)
                     │  • Datadog UI      (APM, logs, infra,
                     │                     service map)
                     │  • Agent Observability Platform UI   ← 未来，不在 v1
                     │      (跨 agent / 跨 tenant 汇总,
                     │       钻取 hub, 音频回放)
                     └──────────────────────────────────────┘
                                       ▲
                                       │
                     ┌──────────────────────────────────────┐
                     │  Layer B: 统一数据模型                 │
                     │  ────────────────────────────────    │
                     │  • 关联键                             │
                     │  • 标准事件名                          │
                     │  • 标准 metric 定义                    │
                     │  • Platform vs Product metric 分层    │
                     └──────────────────────────────────────┘
                                       ▲
                                       │
                     ┌──────────────────────────────────────┐
                     │  Layer A: 采集                        │
                     │  ────────────────────────────────    │
                     │  • propio-obs-sdk（本 package）       │
                     │  • OTel Collector → fan-out           │
                     │  • S3 音频上传                        │
                     │  • Postgres metadata index            │
                     └──────────────────────────────────────┘
                                       ▲
                                       │
                     ┌──────────────────────────────────────┐
                     │  Agent code（任何 framework）          │
                     └──────────────────────────────────────┘
```

### 谁负责什么

| 关注点 | 负责方 | 为什么 |
|---|---|---|
| LLM run tree、prompt/response、threads、online evaluators | **LangSmith** | 它原生功能。我们付钱用就好 |
| APM span、infra metrics、structured logs、log/trace 关联、env/service/version tagging | **Datadog** | 同上 |
| Service map、infra alert、runbook 集成 | **Datadog** | 同上 |
| Online evaluator 调度、feedback API、evaluator UI | **LangSmith** | 同上 |
| **统一 schema 强制**（所有人用同一套字段名） | **propio-obs-sdk** | 只有我们能强制。Vendor 没法 |
| **跨 agent / 跨 tenant 聚合**（如：按 tenant + model 看 p95 latency） | **Agent Observability Platform**（PG summary store）—— *future, not v1* | LangSmith 和 Datadog 不互通；跨平台查询需要公共存储。v1 SDK 把 schema 准备好，平台后做 |
| **统一钻取 URL**（一键 → LangSmith trace + Datadog APM + 音频） | **Agent Observability Platform** —— *future, not v1* | 一个小 web 层，按 `request_id` 跳进各后端 |
| **新 agent 默认 dashboard / alert** | **propio-obs-sdk**（provisioning） | 有 agent config；通过 Datadog Dashboard API + LangSmith project API 自动建 |
| **音频 blob 存储** | **S3** | 便宜、可审计、retention 可控 |
| **音频 metadata 索引** | **Postgres** | 按 session/tenant 快速查；不能 LIST S3 |
| **Summary 事件**（每 request 一行带关键 metric） | **Postgres summary store** | 我们自己 UI 做跨 agent 分析的支撑 |

### 一次 request 流向

**v1（只有 SDK）：**
```
Agent 处理一个 request
    │
    ├─→ Datadog: APM trace + logs + metrics  (运维视角)
    ├─→ LangSmith: root trace + child runs + thread (LLM 视角)
    └─→ Propio 内部 monitor DB: 事件镜像  (实时监控 — 已存在)
```

**未来（Agent Observability Platform — 不在 v1）：**

加第 4 个去向 —— Postgres summary store —— 做跨 agent / 跨 tenant 分析。SDK schema 已经设计好支持，加它的时候只需改 config 不改代码。

所有去向都带**相同关联键**（`request_id`、`session_id`、`conversation_id`、`agent_id`、`version`、`env`、`tenant_id`）。这就是 Layer B 统一数据模型存在的全部理由。

### v1 明确**不**做

- **不做** LLM trace UI —— LangSmith 6 个月内我们做不出来更好的
- **不做** log 搜索引擎 —— Datadog Logs 更好
- **不做** APM / span viewer —— Datadog APM 更好
- **不做** evaluator 引擎 —— LangSmith online evaluator 已经定时跑出分；我们 API 拿就好
- **不做** trace/log/metric 的告警引擎 —— LangSmith / Datadog 各有；告警都路由到同一个 incident channel
- **不做** Agent Observability Platform UI —— 等 SDK 在 2-3 个 agent 跑稳之后再说

### v1 SDK **要**做

- **Verb 层**（`init_agent`、`start_request` 等）—— agent 唯一调用面
- **Schema 强制** —— 每个事件都带统一关联键
- **OTel emit + Collector config** —— fan-out 到 LangSmith / Datadog
- **音频存储路径**（S3 + PG metadata 索引）—— vendor 都不擅长
- **Provisioning glue** —— `init_agent()` 调 Datadog Dashboard API + LangSmith project API（Phase 6+）

---

## 4. 打包 & 发布

### Package metadata

```toml
# pyproject.toml
[project]
name = "propio-obs-sdk"
version = "0.1.0"
description = "Propio agent observability SDK — LangSmith + Datadog + Propio DB fan-out"
requires-python = ">=3.11"

dependencies = [
    "pyyaml>=6.0",
    "pydantic>=2.7",
    "httpx>=0.28",
    # OpenTelemetry — wire 层
    "opentelemetry-api>=1.25",
    "opentelemetry-sdk>=1.25",
    "opentelemetry-exporter-otlp>=1.25",
    "opentelemetry-instrumentation-httpx>=0.46b0",
    "opentelemetry-instrumentation-fastapi>=0.46b0",
    # OpenLLMetry — auto-instrument OpenAI/Anthropic 等
    "traceloop-sdk>=0.30",
    # S3 + Postgres，音频 out-of-band
    "boto3>=1.34",
    "asyncpg>=0.29",
    # 可选（按 config.backends 懒加载）
    "langsmith>=0.7",
    "ddtrace>=2.0",
    "openai>=2.14",
]

[project.optional-dependencies]
langchain = ["langchain-core>=0.2"]
realtime  = ["websockets>=14"]
dev       = ["pytest", "pytest-asyncio", "ruff", "mypy"]
```

### 发布

- v0.x：发到内部 pypi（CodeArtifact / Artifactory / Nexus）
- Agent 接入：`pip install propio-obs-sdk`
- 启动 fallback：`pip install git+ssh://git@github.com/propio/propio-obs-sdk.git@v0.1.0`
- **版本约束**：agent 必须 pin `propio-obs-sdk==0.1.x`（兼容版本号），patch 自动升级，minor 升级要测

### Versioning

Semver。Verb signature 改动 → major bump。加新 exporter → minor。Bug fix → patch。

---

## 5. 配置 schema（observability.yml）

每个 agent 配一份 YAML，`init_agent()` 启动时读一次。

### 5.0 平台常量 vs agent 配置（关键设计）

Propio 平台级常量（LangSmith endpoint、API-key env-var 名、Datadog site、S3 region、per-env PG URL env-var 名等）**全部住在 SDK 的 `propio_obs/platform_defaults.py` 模块**，不要求 agent 配置里重复写。Agent 配置只声明**自己的身份**和**哪些 backend 启用**。

三层更新通道，按变化频率：

| 变化频率 | 改哪 | 怎么生效 |
|---|---|---|
| **几年一次** —— LangSmith 换 host、Datadog 换 site、迁 region | 改 `propio_obs/platform_defaults.py`，bump SDK 版本 | 每个 agent `uv sync` + 重启 |
| **几个月一次** —— API key 轮换、DB 实例更换 | 改 env vars（`.env` / k8s secret） | 重启 agent，SDK 不动 |
| **per-agent 例外** —— 某 agent 走自建 LangSmith / 自定 S3 bucket | 在 `init_agent({...})` dict 里写覆盖字段 | 重启 agent，SDK 不动 |

`init_agent({...})` dict 本身就是 override 通道：agent 写的字段覆盖 SDK 默认；不写就用 SDK 默认。

### 5.1 `environment` 必填

每个 agent 必须声明自己跑在哪个 env（决定哪个 LangSmith project、哪个 PG URL、Datadog `env:` tag 等）。来源（按优先级）：

1. `config.agent.environment` 显式传
2. `PROPIO_ENV` env var 兜底
3. 都没有 → **`init_agent()` 抛 `ValueError`**（故意挡住，避免 prod 数据落到 dev 后端）

合法值：`dev` / `qa` / `staging` / `prod`（定义在 `platform_defaults.Environment`）。

### 5.2 后端字段兜底规则

| 字段 | Agent 不传时 SDK 怎么取 |
|---|---|
| `backends.langsmith.endpoint` | `platform_defaults.LANGSMITH_ENDPOINT` |
| `backends.langsmith.api_key_env` | `platform_defaults.LANGSMITH_API_KEY_ENV` (`"LANGSMITH_API_KEY"`) |
| `backends.langsmith.project` | `LANGSMITH_PROJECT` env → 兜底 `agent.agent_id` |
| `backends.postgres_db.enabled` | `True`（默认开 —— Propio 自有 infra，catch-all 事件镜像 DB） |
| `backends.postgres_db.url_env` | `platform_defaults.POSTGRES_DB_URL_ENV_BY_ENV[environment]` |
| `backends.audio_index_pg.url_env` *(v0.2+)* | `platform_defaults.AUDIO_INDEX_PG_URL_ENV_BY_ENV[environment]` |
| `backends.audio_s3.region` *(v0.2+)* | `platform_defaults.AUDIO_S3_REGION` (`"us-east-1"`) |
| `backends.audio_s3.bucket` *(v0.2+)* | **不兜底** —— 每个 agent 必须自己声明（agent 拥有自己的存储） |
| `backends.datadog.enabled` | `False`（外部 SaaS，opt-in） |
| `backends.datadog.site` | `platform_defaults.DATADOG_SITE` (`"datadoghq.com"`) |
| `backends.datadog.api_key_env` | `platform_defaults.DATADOG_API_KEY_ENV` (`"DD_API_KEY"`) |
| `backends.datadog.service` | `agent.service`（fall through） |
| `backends.datadog.env_tag` | `agent.environment`（fall through） |
| `backends.datadog.version` | `agent.version`（fall through） |
| `backends.datadog.agent_url` | None → ddtrace 用 `localhost:8126`（DD Agent）；override 走 OTel Collector 或 dd-edge endpoint |
| `backends.datadog_logs.enabled` | `False`（独立 toggle，跟 APM 分开） |
| `backends.datadog_logs.api_key_env` / `site` | 复用 APM 的 `DATADOG_API_KEY_ENV` / `DATADOG_SITE` —— 同一个 key |
| `backends.datadog_logs.service` / `env_tag` / `version` | 兜底 `agent.*`（同 APM） |
| `backends.datadog_logs.min_level` | `"DEBUG"`（发全部）；调成 `"INFO"` 收紧 |
| `backends.datadog_logs.exclude_loggers` | `["ddtrace", "urllib3", "datadog", "httpx"]` |



### 完整示例

```yaml
# ─── Agent 身份（必填）──────────────────────────
agent:
  agent_id: support_voice              # propio 内全局唯一
  agent_name: Support Voice Agent      # 人读
  agent_type: realtime_agent           # realtime_agent | chat_agent | tool_agent | batch
  modality: voice                      # voice | text | multimodal
  service: agent-gateway               # 部署服务名
  default_tags:                        # 每个事件都打
    team: ai-platform
    env: prod
    domain: support

# ─── 自定义 metric 定义 ────────────────────────
quality_metrics:                       # 产品 metric（LangSmith 定时算）
  - task_success
  - answer_grounded
  - escalation_avoided

voice_metrics:                         # 数值 metric，每个 voice event 带
  - first_audio_ms
  - barge_in_success
  - asr_latency_ms

# ─── OTel Collector（传输层）────────────────────
otel:
  endpoint: http://localhost:4317
  protocol: grpc
  service_name: agent-gateway
  resource_attributes:
    deployment.environment: prod

# ─── 后端配置（仅当 SDK 需要原生 client 时）────
# 主要数据通路是上面的 OTel。这些 backend 配置是 OTel 做不了的事：
#   - LangSmith：通过 REST 拉 evaluator score
#   - S3：音频 blob 上传
#   - Postgres：音频 metadata 索引
backends:
  langsmith:
    enabled: true
    api_key_env: LANGSMITH_API_KEY
    project: customer-support-prod
    fetch_evaluator_scores: true       # 定时拉分

  audio_s3:
    enabled: true
    bucket: agent-recordings
    region: us-east-1

  audio_index_pg:
    enabled: true
    url_env: AUDIO_INDEX_PG_URL
    table: audio_recordings

  postgres_db:                         # 事件镜像 DB（跟音频索引分开）—— 默认开
    enabled: true
    url_env: POSTGRES_DB_URL             # 不写 → SDK 用 per-env 表

# ─── Channel routing（fan-out map）─────────────
# Channel 声明它**是什么**。OTel Collector config 决定 channel 落到哪个后端。
# 这里只控制 SDK 端：发 OTel? 上传音频? 写 PG?
routing:
  llm_trace:    [otel]                            # → Collector → LangSmith
  tool_call:    [otel]
  voice_event:  [otel, audio_s3, audio_index_pg]  # OTel span + 音频 blob + PG 行
  quality:      [otel]                            # OTel attrs；LangSmith 定时 evaluator 算分
  apm_span:     [otel]                            # → Collector → Datadog APM
  log:          [otel]                            # → Collector → Datadog Logs
  metric:       [otel]                            # → Collector → Datadog Metrics

# ─── 行为调优 ──────────────────────────────────
behavior:
  async_export: true                   # 后台队列
  export_queue_size: 1000              # 满了丢最老的
  export_timeout_ms: 5000
  sampling:                            # v1 全 100%（见 §11）
    llm_trace: 1.0
    voice_event: 1.0
    log: 1.0
  redaction:
    pii_fields: [email, phone, ssn]
```

### Channel（固定列表）

| Channel | 干嘛 | OTel 信号 | 下游（在 Collector 决定） |
|---|---|---|---|
| `llm_trace` | LLM completion 调用、prompt/response、token | OTel Span (kind=client) | LangSmith |
| `tool_call` | 函数/tool 调用 | OTel Span (kind=internal) | LangSmith |
| `voice_event` | STT / TTS / VAD / barge-in | OTel Span + audio→S3 + metadata→PG | LangSmith（仅 span attrs）；播放走 S3 presigned URL |
| `quality` | per-request 质量分（原始 I/O；分由 LangSmith 定时算） | OTel Span 属性 | LangSmith |
| `apm_span` | HTTP / DB / 外部 API 时延 | OTel Span（auto-instrument） | Datadog APM |
| `log` | 结构化服务日志 | OTel Log | Datadog Logs |
| `metric` | 自定义 counter / histogram | OTel Metric | Datadog Metrics |

Channel 在 v1 是**固定**的——SDK 定义这套，agent 不能自创。

---

## 6. 公共 API — Verbs

全部在 `import propio_obs as obs` 下。

### 6.1 `obs.init_agent(config)`

进程启动时**调一次**。副作用：

1. 读 + 校验 YAML
2. 读环境变量（`*_env` 字段）
3. 实例化每个 enabled exporter；某个 backend 配错只 warn 不 crash
4. 注册 OTel auto-instrumentation（FastAPI、httpx、OpenAI 通过 OpenLLMetry）
5. 桥接 Python `logging` → OTel Logs
6. 启异步导出队列 worker

幂等：调两次第二次 warn 后 no-op。

### 6.2 `obs.start_request(...)`

```python
def start_request(
    request_type: str,                       # "voice_turn" | "chat" | "tool_run" | ...
    *,
    agent_id: str | None = None,
    session_id: str | None = None,
    user_id: str | None = None,
    inputs: dict | None = None,
    metadata: dict | None = None,            # 比如 {"tenant_id": "hospital_a"}
) -> Request:
```

- 生成 `request_id`（UUID4）
- 开 OTel root span（kind=server，name=`agent.request`）
- 把 span context 设为当前 active context（后续子 verb 自动嵌套）
- 返回 `Request` opaque handle

### 6.3 `obs.record_tool(req, name, input, output, *, error=None, metadata=None)`

- 发 `tool_call` channel
- OTel 子 span（name=`tool.<name>`，kind=internal）
- 错误时 set status

### 6.4 `obs.record_quality(req, metric, value=None, *, comment=None)`

- 发 `quality` channel —— 写到 **OTel span 属性**，不是单独事件
- `metric` 必须在 `config.quality_metrics` 里（不在就 warn）
- **`value` 可选**。Agent 不需要自己算分
- **分从哪来**：LangSmith 定时 evaluator 读 trace 的 input/output 算分写回。Observability 平台从 LangSmith Feedback API 定时拉（如每小时）。
- 如果传了 `value`，作为 fast-path 直接记（适合「tool 是否返回非空」这种确定性检查）。预算分数和 LangSmith 分数共存（不同 key：`metric` vs `metric.evaluated`）

### 6.5 `obs.record_voice_event(req, event, *, metrics=None, audio_wav=None)`

```python
# event: "speech_start" | "asr_finalized" | "tts_first_byte" | "barge_in_detected" | ...
# audio_wav: 可选 WAV bytes —— 上传 S3，**不**发 LangSmith
```

- OTel span（kind=internal）作为 parent request span 的子
- 如果传了 `audio_wav` 且 routing 含 `audio_s3`：
  1. 算确定性 S3 key（`agent_id / 日期 / session_id / turn_id / event`，见 §8.4.4）
  2. **异步**上传 WAV 到 S3
  3. **同步**写 PG `audio_recordings` 一行
  4. 把 `audio.s3_key` + `audio.duration_ms` 作为 OTel span 属性，LangSmith / Datadog UI 能看到引用

音频 blob **从不**发到 LangSmith ——只通过 S3 key 引用。

### 6.6 `obs.finish_request(req, *, status="success", outputs=None, error=None)`

- 关 OTel root span，set status
- 写 `summary` 事件：`request_duration_ms` / `tool_calls_count` / `voice_events_count` / `llm_tokens_total` / 收集到的 quality 分

### 6.7 Helpers

```python
client = obs.wrap_llm_client(AsyncOpenAI())          # 自动 trace OpenAI 调用
agent  = ...with_config({"callbacks": [obs.langchain_callback()]})  # LangChain 自动接入
obs.attach_openai_realtime(req, openai_ws)           # 订阅 Realtime API events 自动转 verb
obs.log(req, level="info", message="...", **kw)      # 显式结构化日志
obs.flush(timeout_ms=5000)                            # 短任务退出前刷队列
```

---

## 7. Channel Routing

### 概念

每个 SDK 内部事件带一个 `channel` string。Router 查 `config.routing[channel]` 找一个后端列表，分发给每个后端的 exporter。

```python
# Router（内部，简化）
class Router:
    def emit(self, channel: str, event: Event) -> None:
        targets = self.routing.get(channel, [])
        for name in targets:
            exporter = self.exporters.get(name)
            if exporter is None:
                continue
            self._submit(exporter, event)  # → 异步队列
```

### Selective routing 例子

只发 LangSmith 的 LLM、只发 Datadog 的 log、两个都发的 voice：

```yaml
routing:
  llm_trace:    [otel]                              # → LangSmith
  tool_call:    [otel]
  voice_event:  [otel, audio_s3, audio_index_pg]    # 多目的
  log:          [otel]                              # → Datadog
```

### 不做的事

- Router 不做格式转换 —— 每个 exporter 自己翻译
- Router 不聚合 / rollup —— 后端的事
- Router 不在 emit 之外重试 —— 假设后端有自己 buffering

---

## 8. Backend Adapters

v1 有**两类** exporter：

1. **OTel exporter**（默认，绝大多数 channel 走这条）—— 发 OTel spans / logs / metrics 通过 OTLP，Collector 决定下游
2. **Out-of-band exporters** —— OTel 装不下的：音频 blob (`audio_s3`)、metadata index (`audio_index_pg`)、事件镜像 DB (`postgres_db`)

### 8.1 OTel exporter（主要）

`init_agent()` 时：
1. `Resource` 带：`service.name`、`agent.id`、`agent.type`、`deployment.environment`、`team`、`default_tags`
2. `TracerProvider` + `BatchSpanProcessor(OTLPSpanExporter)`
3. `LoggerProvider` + `BatchLogRecordProcessor` —— stdlib logging 桥接到 OTel logs
4. `MeterProvider` + 周期 metric exporter
5. **Auto-instrumentation**：httpx / FastAPI / OpenAI（通过 `traceloop-sdk` 的 OpenLLMetry）

Verb → OTel 映射：

| Verb / Channel | OTel 信号 |
|---|---|
| `start_request()` | 开 `Span`（kind=server，name=`agent.request`） |
| `record_tool()` | 子 `Span`（name=`tool.<name>`，kind=internal） |
| `record_voice_event()` | 子 `Span`（name=`voice.<event>`，kind=internal）；`audio.s3_key` 属性 |
| `record_quality()` | parent span 属性 `quality.<metric>` |
| `obs.log(...)` | OTel `LogRecord` 带 `request_id` / `trace_id` |
| `metric` | OTel `Counter` / `Histogram` |
| `finish_request()` | Span 关闭（status=OK/ERROR，outputs 作为属性） |

每个 span 标准属性：`agent.id`、`agent.type`、`service.name`、`deployment.environment`、`team`、`request_id`、`session_id`、`tenant_id`（适用时）、`turn_id`（适用时）。

**OTel Collector 配置**（运维侧，跟 agent 分离）：

```yaml
receivers:
  otlp:
    protocols: { grpc: { endpoint: 0.0.0.0:4317 } }

processors:
  batch:
  attributes/redact:
    actions:
      - { key: prompt, action: hash }
      - { key: response, action: hash }

exporters:
  otlp/langsmith:
    endpoint: api.smith.langchain.com:443
    headers: { x-api-key: "${LANGSMITH_API_KEY}" }
  datadog:
    api: { key: "${DD_API_KEY}", site: datadoghq.com }

service:
  pipelines:
    traces:  { receivers: [otlp], processors: [batch, attributes/redact], exporters: [otlp/langsmith, datadog] }
    logs:    { receivers: [otlp], processors: [batch], exporters: [datadog] }
    metrics: { receivers: [otlp], processors: [batch], exporters: [datadog] }
```

这是运维 config —— 不是 agent 团队的事。加新后端（Langfuse / New Relic）改 Collector，**agent 代码不动**。

### 8.2 LangSmith —— OTel 之外还需要 native client

LangSmith 原生支持 OTLP，所以 OTel exporter 直接负责发。但 SDK 还要 native LangSmith client 做 2 件事：

1. **拉 evaluator scores**（定时 evaluator 模式 —— 见 §15.3）。Observability 平台定时调 `Client.list_runs(...)` + `Client.list_feedback(...)` 把分数拉回我们 analytics
2. **创建 project**（首次 init_agent 时，延后到 §13）

音频**不**附加到 LangSmith run —— 只通过 OTel `audio.s3_key` 属性引用。LangSmith UI 看到 key，点击拿 presigned URL。

### 8.3 Datadog —— v0 用 `ddtrace`（未来切 OTel）

> **当前状态**（v0.0.2）：用 `ddtrace` 直接 emit APM spans 到 DD Agent。长期会切 OTel（见下方"未来切换"小节）。

#### 8.3.0 当前实现 —— `ddtrace` + DD Agent

`exporters/datadog.py` 用 `ddtrace` SDK，verb 层在 `start_request` / `record_*` / `finish_request` 里向 `dd.open_request_span` / `dd.emit_child_span` / `dd.close_request_span` 双发（跟 LangSmith RunTree 并行，互不依赖）。

**几个明确决定**：

- **不 `patch_all()`** —— 只 emit verb 层产生的 span。理由：(a) OpenAI 的 trace 由 LangSmith 主治，DD 自动 patch 会重复；(b) 干净 —— 业务代码外的延迟想看就显式 `record_tool`
- **DD Agent 传输** —— ddtrace 默认发到 `localhost:8126`。Local dev 装 DD Agent docker 就行：
  ```bash
  docker run -d --name dd-agent \
    -e DD_API_KEY=$DD_API_KEY \
    -e DD_SITE=datadoghq.com \
    -e DD_APM_ENABLED=true \
    -p 8126:8126 datadog/agent
  ```
  没 agent 时 ddtrace 静默 buffer + drop，不报错（agent shutdown 时会等 5s 试图 flush）
- **`error` 标志只给真异常** —— `status="interrupted"`（barge-in）作为 output tag 落下，**不**置 `span.error=1`。DD UI 想看 barge-in 率用 `output.status:interrupted` 过滤，不污染 error rate dashboard
- **标准 tag schema** —— 每个 span 自动带：`env`、`service`、`version`、`agent.id`、`agent.type`、`modality`、`request.id` / `session.id` / `tenant.id`

#### 8.3.1 未来切换 —— OTel + Datadog OTLP intake

切换是 `exporters/datadog.py` 内部重写 —— **verb 层、agent 配置都不动**。路径：

1. 把 `dd.open_request_span` 实现从 `ddtrace.tracer.trace(...)` 换成 `opentelemetry.trace.Tracer.start_span(...)`
2. OTLP HTTP exporter 指向 DD Agent 的 OTLP receiver（`localhost:4318`）或 Datadog cloud OTLP intake（如果该 site 启用了）
3. `pyproject.toml` 把 `ddtrace>=2.0` 换成 `opentelemetry-sdk + opentelemetry-exporter-otlp`

**收益**：同一 wire 格式，将来加 Langfuse / New Relic 等只改 Collector config 不改 SDK；摆脱 ddtrace 大依赖；跟 plan §3 的 OTel-as-wire 长期方向对齐。

**何时切**：当 (a) Datadog 账号确认开通 OTLP intake，或 (b) Propio 部署 OTel Collector 作为 fan-out 中枢。在那之前 ddtrace 路径更省事。

#### 8.3.2 Datadog Logs —— HTTPS 直发 intake API

> **当前状态**（v0.0.3）：`exporters/datadog_logs.py` 实现，独立于 APM toggle。

跟 APM 是两个独立的 DD feature，wire 路径不同：

- **APM**：ddtrace → DD Agent (localhost:8126) → DD
- **Logs**：Python `logging.Handler` → HTTPS POST `http-intake.logs.{site}/api/v2/logs` → DD（**不需要** DD Agent 介入）

为什么走 HTTPS 直发而不是 DD Agent log-tail：

- Windows 本地 dev 不用 mount log file volume / 配 Agent log source
- DD Agent 职责单一（只 APM）
- 跟未来 OTel 迁移路径一致（OTel 也是直发 OTLP）

实现细节：

- `_DatadogAsyncHandler`（私有，移植自 scheduling-agent）：batched async POST + 指数退避 retry，背景 thread 跑 `asyncio` event loop
- **`DD_LOGS_INJECTION=true`** 在 `configure()` 自动设：让 ddtrace 把 `dd.trace_id` / `dd.span_id` 注入到每条 LogRecord；handler 转给 DD UI，log 行可以一键跳到对应 APM trace
- **`exclude_loggers`**（默认 `["ddtrace", "urllib3", "datadog", "httpx"]`）：排除观测内部的 DEBUG 噪音 + 避免 log 通道自我观察循环
- **失败隔离**：`emit()` 永不 raise；DD 不可达时 batch 重试 3 次后丢弃

Agent 端只需一行 toggle：
```python
"backends": {"datadog_logs": {"enabled": settings.dd_log_enabled}}
```
其他字段（site / api_key / service / env / version / 排除 loggers）SDK 全兜底。

#### 8.3.3 LLM Observability UI

需要 `ddtrace.llmobs`。**v0 跳过** —— LangSmith 已覆盖；以后某 team 想要再加并行通路。

### 8.4 音频采集 & 上传 —— Out-of-band（S3 + Postgres）

v1 设计最特殊的部分。音频直接传 S3，metadata 存 PG，OTel span 只带 **S3 key**。

#### 8.4.1 为什么不 chunk-by-chunk 推 S3

- 音频 chunk 才 ~256ms / 8KB。一天几千 turn × 每 turn 几十 chunk = **几十万小文件**
- S3 PUT 按请求收钱（$0.005/1k），LIST 慢，metadata index 爆炸
- S3 Multipart Upload 的 part 最小 5MB（除最后一个），chunk 凑不到
- 录音已经在内存里 broadcast 给 monitor 了，持久化不需要 sub-second

#### 8.4.2 触发时机（每 voice turn）

```
turn_start  → buffer 清空
   ...      → chunk 边收边塞 buffer
turn_end    → user_audio_capture 封口          → ① 异步 upload user.wav 到 S3
LLM 出文本
TTS 流 chunks → agent_audio_parts 累积
audio_complete → 拼成完整 WAV                  → ② 异步 upload agent.wav 到 S3
                                              → ③ 同步 INSERT audio_recordings 行 (PG)
```

①② 用 `asyncio.create_task` 后台跑，**不阻塞**下一 turn。③ 是同步因为 PG 写很快（1-5ms），且要保证带 S3 key 的 OTel span flush 之前 PG 行已存在。PG 失败也照样设 OTel 属性，S3 LIST 可作为兜底恢复手段。

#### 8.4.3 三种持久化方案对比

| 方案 | 实时 chunk 推 | **每 turn 异步 upload ✅** | 攒一天 batch sync |
|---|---|---|---|
| 用户感知延迟 | 增加（堵 WS 写） | **0**（后台 task） | 0 |
| S3 object 数 | 几十万/天 💀 | 几千/天 ✅ | 一个大归档 |
| Crash 丢数据 | 最少 | 最多丢当前 turn | 最多丢一天 |
| 实时回放 | 可以但意义不大 | turn 结束 ~1s 内 | 只能次日 |
| 成本 | 高（PUT + 小文件） | 低 | 最低，运维复杂 |
| 实现复杂度 | 高（multipart 拼接） | **低**（一次 put_object） | 中（队列 + 调度） |

**v1 选中间。** Live monitor 走现有 WebSocket（不变）；S3 持久化在 turn_end / audio_complete 各触发一次后台 upload；PG 同步写一行 metadata。两条路径解耦——live UX 不依赖 S3，S3 也不阻塞 UX。

#### 8.4.4 S3 Layout

```
s3://agent-recordings/
└── {agent_id}/                       # 比如 propio-agent
    └── {YYYY-MM-DD}/                 # UTC 日期
        └── sessions/{session_id}/
            └── turns/{turn_id}/
                ├── user.wav          # ~1-30s, 32-960KB
                ├── agent.wav
                └── meta.json         # transcript + latency snapshot + provider 版本
```

- `{agent_id}` 前缀隔离不同 agent
- 日期分区 → Athena / S3 Select 查询 + 按日 Lifecycle policy（如「30天 IA → 90天 Glacier → 1年删除」）
- `turn_id` 嵌进 key → 重试幂等
- 可选 Opus 编码（小 ~10×），v1 先 WAV

#### 8.4.5 为什么 PG 索引 + S3 blob（不是只用 S3）

用户问：「先发给 postgresql？还是 batch 发给 s3？去 s3 取很慢的吧」

**两个都要 —— 各有职责。**

| 关注点 | Postgres (`audio_index_pg`) | S3 (`audio_s3`) |
|---|---|---|
| 查「session X 的所有音频」 | 索引 scan，<10ms | LIST 慢 + 分页，$0.005/1k 请求 |
| 每 turn 写 | 同步 INSERT，1-5ms | 异步 PUT，50-500ms |
| 存储成本 | $0.10/GB-月 | $0.023/GB-月 |
| 存 bytes | 不存 —— 只 metadata | 存 |
| Observability 平台主要读 | **是**（快） | 仅当用户点「播放」 |

模式：**PG = 索引（快、可查），S3 = blob（便宜、大、扫描慢）**。

PG schema：

```sql
CREATE TABLE audio_recordings (
    id BIGSERIAL PRIMARY KEY,
    request_id    TEXT NOT NULL,
    session_id    TEXT NOT NULL,
    turn_id       TEXT NOT NULL,
    customer_id   TEXT,
    agent_id      TEXT NOT NULL,
    role          TEXT NOT NULL,      -- 'user' | 'agent'
    s3_bucket     TEXT NOT NULL,
    s3_key        TEXT NOT NULL,
    duration_ms   INTEGER,
    bytes         INTEGER,
    sample_rate   INTEGER,
    transcript    TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_audio_session ON audio_recordings (session_id, created_at);
CREATE INDEX idx_audio_request ON audio_recordings (request_id);
CREATE INDEX idx_audio_customer ON audio_recordings (customer_id, created_at);
```

Observability 平台查询路径：
1. 按 `session_id` / `customer_id` / 时间范围查 PG → 拿到 S3 key 列表
2. UI 渲染 metadata（duration、transcript、latency）
3. 用户点「播放」→ 后端签 S3 URL（`generate_presigned_url`，TTL 60s）→ 浏览器播放

平台快（不 LIST S3），存储便宜。

### 8.5 Propio 内部 DB —— 事件镜像（跟音频索引分开）

不是 `audio_index_pg`。这是现有 `monitor_logs.db`，给现有实时 monitor 前端用的。Schema 复用：

```sql
CREATE TABLE IF NOT EXISTS sessions (...);
CREATE TABLE IF NOT EXISTS logs (...);
```

这个 DB 是 agent 本地的（适合实时调试），**不是**分析 source of truth —— 那是 OTel pipeline。v1 保留是因为现有 monitor 依赖。

---

## 9. 标准 Schema（统一数据模型）

这是 Layer B 的核心（§3.5）。每个事件、span、log、summary 行都带相同字段名 + 相同语义，跨 agent 跨后端统一。**这是最重要的标准化**——没它跨 agent dashboard 不可能。

### 9.1 必填关联键（每个事件都带）

SDK mint 这些键，通过 OTel context、S3 路径、PG 行自动传播：

| 字段 | 来源 | 例 | 备注 |
|---|---|---|---|
| `agent_id` | config | `support_voice` | 机读、稳定 |
| `agent_name` | config | `Support Voice Agent` | 人读 |
| `agent_type` | config | `chat` / `voice` / `batch` / `workflow` | 过滤维度 |
| `modality` | config | `text` / `voice` / `multimodal` | 过滤维度 |
| `service` | config | `agent-gateway` | 对应 Datadog `service` tag |
| `version` | config 或 env | `2026.04.27-abc1234` | 部署版本 —— Datadog `version` tag |
| `env` | config | `prod` / `staging` / `dev` | Datadog `env` tag |
| `region` | config 或 env | `us-east-1` | infra 维度 |
| `team` | config | `ai-platform` | 归属 |
| `tenant_id` | runtime（`start_request(metadata={"tenant_id": ...})`） | `hospital_a` | SDK 自动传播；老文档叫 `customer_id` |
| `session_id` | runtime（auth 层） | uuid | 一个 user session |
| `conversation_id` | runtime（SDK mint） | uuid | session 内一个逻辑对话 |
| `request_id` | SDK mint | uuid | 一个 start_request → finish_request |
| `turn_id` | SDK 在第一个 voice/chat turn mint | uuid | 子 request 粒度 |
| `trace_id` | 由 `request_id` 派生 | 128-bit hex | OTel 兼容 —— 跨后端 join |
| `user_id_hash` | runtime（auth 层）；**已经哈希过** | sha256 前缀 | 永远不发原始 user_id —— 隐私 |

> `tenant_id` vs `customer_id`：同一个概念。文档之前用 `customer_id`，今后统一 `tenant_id`（更标准的多租户术语）。SDK 接受两个 alias `customer_id → tenant_id`。

### 9.2 领域特定字段（适用时记录）

| 字段 | 出现在 | 例 | 备注 |
|---|---|---|---|
| `model_provider` | LLM call span | `openai` / `anthropic` / `mistral` | 总配 `model_name` |
| `model_name` | LLM call span | `gpt-4o` / `claude-3-7-sonnet` | 用的具体模型 |
| `model_version` | LLM call span（provider 暴露时） | `gpt-4o-2024-08-06` | A/B 比较用 |
| `tool_name` | tool span | `search_docs` | 一次 tool 调用 = 一个 span |
| `workflow_name` | 多步 agent run | `appointment_booking_v2` | 有名字的 pipeline |
| `stt_provider` / `stt_model` | voice event | `deepgram` / `flux-general-en` | 镜像 propio 现有字段 |
| `tts_provider` / `tts_model` / `tts_voice` | voice event | `elevenlabs` / `eleven_multilingual_v2` / `Adam` | 同 |
| `audio.s3_key` | 带音频的 voice event | `propio-agent/2026-04-27/...` | 指针，永远不放 bytes |
| `audio.duration_ms` | 带音频的 voice event | `2300.0` | 快速过滤 |

### 9.3 ID 层级（外 → 内）

```
agent_id  (静态，从 config)
└── customer_id    (这条流量属于哪个 propio enterprise 客户)
    └── session_id  (一个 user session — 一次登录或一个 WS 连接)
        └── conversation_id  (session 内一个逻辑对话)
            └── request_id   (一次 start_request → finish_request)
                └── turn_id  (request 内一个 voice/chat turn)
                    └── trace_id (OTel 128-bit hex，从 request_id 派生)
```

**为什么 customer_id 是 session 级别，不是 per-event**

Propio 确认：**session 内永远不切换 customer**。Customer 在用户认证 / WebSocket 打开时确定。跨 session 同一个 user 可能关联不同 customer，所以必须每个 session 传 —— 但一旦设了就不变。

意味着：
- `customer_id` 在 session 第一次 `start_request` 时通过 metadata 传入，**SDK 自动**对该 session 后续所有 verb 传播
- v1 实现：SDK 内部一个 `session → customer_id` dict cache。第一次 `start_request(metadata={"customer_id": X})` 设，后续相同 `session_id` 的 verb 复用
- v2 用 OTel context propagation / Python `contextvars` 完全隐式（见 §15.6）

**为什么有 conversation_id**

session = 一个 user 在线。同一个 session 里 user 可能开好几个不同对话（不同话题、agent reset 等）。没 `conversation_id` 就算不出「平均每个对话多少 turn」或者「scope 单个连贯对话的 evaluator」。v1 在 `start_request` mint（如果调用方没传），agent 可显式 reset（`obs.start_conversation(req)` helper）。

**Schema 改动**

`audio_recordings`、OTel resource attribute 都带 `customer_id` 和 `conversation_id` 一等公民。Propio 内部 DB `sessions` 表加列：

```sql
ALTER TABLE sessions ADD COLUMN customer_id TEXT;
CREATE INDEX idx_sessions_customer ON sessions (customer_id);
```

**通过后端的路由**

- **LangSmith / Datadog**：`customer_id`、`conversation_id`、`request_id`、`turn_id`、`trace_id` 作为 OTel resource + span 属性。Dashboard 按 `customer_id` 过滤分组
- **Postgres `audio_recordings`**：`customer_id` 和 `conversation_id` 索引，便于「customer X 过去 24h 所有音频」查询
- **Propio DB**：`customer_id` 在 session 行

结果：在 LangSmith / Datadog / Propio dashboard 任一处一键 pivot 到「这个 customer 的所有数据」。

---

## 10. 用法示例（按 agent 类型）

### 10.1 Voice agent（propio realtime）

```python
import propio_obs as obs
from openai import AsyncOpenAI
obs.init_agent("observability.yml")

async def handle_voice_session(ws):
    req = obs.start_request(
        request_type="voice_turn",
        session_id=ws.session_id,
        metadata={
            "customer_id": ws.customer_id,   # SDK 传播到该 session 后续所有 event
            "caller_id": ws.caller_id,
        },
    )
    try:
        obs.record_voice_event(req, "speech_start")
        transcript, audio_wav = await stt.transcribe(audio_stream)
        obs.record_voice_event(
            req, "stt_complete",
            metrics={"asr_latency_ms": 280},
            audio_wav=audio_wav,                 # → 异步 upload S3，PG 索引
        )

        client = obs.wrap_llm_client(AsyncOpenAI())
        resp = await client.chat.completions.create(model="gpt-4o", messages=[...])

        obs.record_voice_event(req, "tts_first_byte", metrics={"first_audio_ms": 620})
        agent_audio = await tts.synth(resp.choices[0].message.content)
        obs.record_voice_event(req, "tts_complete", audio_wav=agent_audio)

        # 不在这算 task_success —— LangSmith 定时 evaluator 后填
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

agent = AgentExecutor(...).with_config({"callbacks": [obs.langchain_callback()]})

result = agent.invoke({"input": "Find me a flight to NYC"})
```

`obs.langchain_callback()` 返回 `BaseCallbackHandler`：
- `on_chain_start` → `start_request`
- `on_tool_start` / `on_tool_end` → `record_tool`
- `on_llm_start` / `on_llm_end` → emit `llm_trace`
- `on_chain_end` / `on_chain_error` → `finish_request`

### 10.3 OpenAI Realtime agent

```python
import propio_obs as obs
import websockets
obs.init_agent("observability.yml")

async def realtime_session():
    req = obs.start_request(request_type="realtime_session")
    async with websockets.connect("wss://api.openai.com/v1/realtime?...") as ws:
        # Listener 自动把 Realtime event 翻译成 verb
        obs.attach_openai_realtime(req, ws)
        await run_realtime_loop(ws)
    obs.finish_request(req)
```

### 10.4 普通 HTTP chatbot

```python
@app.post("/chat")
async def chat(body: ChatRequest):
    req = obs.start_request(
        request_type="chat",
        session_id=body.session_id,
        inputs={"message": body.message},
    )
    try:
        resp = await client.chat.completions.create(model="gpt-4o-mini", messages=[...])
        obs.record_quality(req, "task_success", 1.0)
        obs.finish_request(req, outputs={"response": resp.choices[0].message.content})
        return {"response": resp.choices[0].message.content}
    except Exception as e:
        obs.finish_request(req, status="error", error=str(e))
        raise
```

---

## 10.5 标准事件分类

每个 agent 发同一套命名事件。这是「跨 agent 平均 tool latency」或「voice agent 本周 barge-in 多少次」这种问题能从单一查询答的原因。

### 10.5.1 通用事件（所有 agent 类型）

| 事件 | 谁发 | 映射 | 必带属性 |
|---|---|---|---|
| `request_started` | `start_request()` | OTel root span open | `request_id`、`session_id`、`tenant_id`、`agent_id`、`version`、`env`、`request_type` |
| `model_started` | LLM call wrap（自动） | 子 OTel span open | `model_provider`、`model_name`、parent `request_id` |
| `model_finished` | LLM call wrap | 子 OTel span close | `completion_tokens`、`total_tokens`、`latency_ms`、`error?` |
| `tool_started` | `record_tool()`（开始） | 子 OTel span open | `tool_name`、`input` |
| `tool_finished` | `record_tool()`（结束） | 子 OTel span close | `output`、`latency_ms`、`error?` |
| `quality_scored` | `record_quality()` 或 LangSmith 拉分 | OTel span 属性 / PG summary | `metric`、`value`、`source`（`inline` / `langsmith_evaluator`） |
| `request_finished` | `finish_request()` | OTel root span close | `status`、`outputs`、`error?`、`duration_ms` |

这些名字 v1 **固定**。加新事件需要 SDK 发版。

### 10.5.2 Voice 专属事件（`modality: voice`）

| 事件 | 谁发 | 必带属性 |
|---|---|---|
| `asr_started` | STT 收到第一个音频 chunk | — |
| `asr_partial` | 每个 interim transcript | `transcript_partial`、`latency_since_audio_ms` |
| `asr_finalized` | 最终 transcript 出来 | `transcript`、`asr_latency_ms`、`audio.s3_key`、`audio.duration_ms` |
| `barge_in_detected` | agent 在说话时 user 插话 | `agent_speaking_ms_at_interrupt` |
| `tts_started` | TTS 请求发到 provider | `tts_provider`、`tts_model`、`tts_voice`、`text_chars` |
| `audio_first_byte` | 第一个 TTS audio chunk 发给 client | `first_audio_ms`（从 request_started 起的 TTFB） |
| `audio_playback_finished` | client 播完 agent 音频 | `audio.s3_key`、`audio.duration_ms` |
| `tts_finished` | TTS 生成完 | `tts_latency_ms`、`total_audio_bytes` |

`barge_in_detected` + `audio_playback_finished` 专门为 §11.5 的产品 metric 而设。

### 10.5.3 为什么固定分类

- **跨 agent dashboard** 需要可预测的事件名。voice A 叫 `voice_first_audio`、voice B 叫 `tts_started`，没法做共享 dashboard
- **默认告警阈值**（「`audio_first_byte` p95 > 1.5s 报警」）只在事件名通用时才行
- **新人入职成本低** —— 不用造名字，从列表挑

需要新事件？流程是「向 SDK 提议加」→ 下个 release。不要 ad-hoc 加自定义名字 —— OTel 允许，但平台不会在 summary 里识别。

---

## 11. 生命周期 & 线程

### 异步导出队列

- 所有 `emit()` 立即返回；事件入 `asyncio.Queue`（同步 agent 用 `queue.Queue`）
- 后台 worker 排空队列调每个 exporter
- 每个 exporter 调用带 per-event 超时（`export_timeout_ms`）
- 超时或异常：增 `propio_obs.export_errors` counter，丢事件，不传播

### Backpressure

- 队列大小封顶 `behavior.export_queue_size`（默认 1000）
- 满了**丢最老的**（FIFO），增 `propio_obs.dropped` counter
- 丢比阻塞好 —— agent 延迟不能依赖后端健康

### 失败隔离

- 一个 exporter 坏掉（如 Datadog 挂）**不影响**其他
- 每个 exporter 在 worker 里自己 try/except
- backend 在 `init_agent` 阶段配错 → SDK log 错误并禁用该 backend，其他正常

### 采样 —— **v1 全 100%**（决定）

- 所有 channel 都 1.0。v1 没采样逻辑
- 理由：成本模型未知，不确定时默认「全留」。一个月生产数据后再评估（见 §15.2）
- `behavior.sampling` config knob 留了向前兼容用，默认 `1.0`，不要碰
- 如果某 channel 在 prod 明显贵了（比如 `voice_event` 几百万 spans/天），第一步是部分采样 —— 但 `error` / `slow` request 通过 OTel tail-sampling 在 Collector 强制留全样本

### 关闭 —— atexit + 显式 flush

- **默认机制：atexit。** SDK 在 `init_agent()` 里 `atexit.register(flush)`。Long-running server（FastAPI、daemon、propio voice agent）正常关闭时 hook 触发，刷 OTel batch processor + S3 队列，5s 超时
- **为什么不给 verb 加 async 变体**：Propio 实际 agent 都是 long-running service。atexit 够用
- **逃生口**：`obs.flush(timeout_ms=5000)` 给短任务用（Lambda、batch script、SIGKILL 前调）。调用方知道自己要退出时显式调
- atexit handler 里并行调 exporter `shutdown()`。OTel `force_flush()` + S3 队列 `join()` 都有自己超时；一个慢 backend 不阻塞其他

---

## 11.5 Platform Metrics vs Product Metrics

常见反模式：把 infra 指标（错误率、p95 延迟）和产品指标（任务成功率、用户满意度）混在同一个 dashboard、用同一套告警阈值。两类有不同观众、不同节奏、不同行动。

### 11.5.1 Platform 指标

**是什么**：agent 技术健康
**Owner**：AI 平台 / SRE
**节奏**：实时告警；分钟级粒度
**后端**：Datadog（主），跨 agent rollup 进我们 PG summary

| Metric | 定义 | 告警示例 |
|---|---|---|
| `request_error_rate` | `count(status=error) / count(*)` 每分钟 | > 1% 持续 5 分 → page |
| `request_p50/p95/p99` | `request_finished.duration_ms` | p95 > 5s 持续 → warn |
| `model_p95_latency` | `model_finished.latency_ms` | 任意模型 p95 > 3s → warn |
| `tool_error_rate` | `count(tool_finished where error) / count(tool_finished)` | 单 tool > 5% → warn |
| `llm_cost_per_request` | 由 token + 价格推 | 超预算 → 通知 |
| `dropped_export_count` | SDK 内部 —— 因 backpressure 丢的事件 | > 0 → warn |
| `audio_upload_failure_rate` | S3 PUT 失败 / 总尝试 | > 0.1% → warn |

### 11.5.2 Product 指标

**是什么**：agent 干得好不好？
**Owner**：产品 / 业务 / AI 质量
**节奏**：小时 / 日；周复盘
**后端**：LangSmith evaluator（主），聚合到 PG summary

| Metric | 定义 | 来源 |
|---|---|---|
| `task_success_rate` | LLM-judge 或 rubric 评 `request_finished.outputs` | LangSmith scheduled evaluator → 拉到 PG |
| `escalation_rate` | `count(workflow_name=human_handoff) / count(*)` | span 里 `workflow_name` 推 |
| `user_satisfaction` | 通话后调查或情感分析 | 外部 → PG |
| `first_audio_latency_p50` | `audio_first_byte.first_audio_ms` p50 | voice event |
| `barge_in_recovery_rate` | `count(barge_in_detected 后跟 valid response) / count(barge_in_detected)` | voice event + LLM event 关联 |
| `answer_grounded_rate` | LLM-judge 评 grounding | LangSmith evaluator |
| `conversation_length_p50` | 每 `conversation_id` 的 turn 数 | summary store |

### 11.5.3 SDK 强制这个区分的原因

- `record_quality()` **只**收 product metric。Latency / error / cost 自动作为 platform metric，agent 不调 `record_quality(metric="latency", ...)`
- `observability.yml` 的 `quality_metrics` 字段是**该 agent 承诺产生的产品 metric 列表**。不在列表里的会被 `record_quality()` 拒（warn）
- 默认 dashboard 模板（§13）是**两套**：每 agent 一个 platform dashboard、一个 product dashboard。不同观众、不同阈值、不同节奏
- 默认告警同样分：platform 告警去 AI Platform / SRE oncall；product 告警（如「task_success 跌破 90%」）去产品 team Slack，**不**去 oncall

这避免了最常见的运维灾难：产品 metric 跌（15 分钟内不可行动）触发 page，oncall 忽略，结果错过下面真实平台 incident。

---

## 12. 迁移路线

### Phase 0 —— 设计评审（现在）
- 把这文档发给相关人
- Verb signature 和 channel 分类得到批准
- 内部 pypi host 决定

### Phase 1 —— Repo 内原型（1-2 周）
- 在本 propio repo 建 `backend/obs_sdk/`
- 实现：`init_agent` / `start_request` / `finish_request` / `record_voice_event` / `wrap_llm_client`
- Adapters：LangSmith、Propio DB（先不做 Datadog）
- 把 `backend/app/services/tracing.py` 迁到新 SDK
- 现有 voice agent 功能从用户角度不变

### Phase 2 —— Datadog adapter（1 周）
- 加 `datadog_apm` + `datadog_logs` exporter
- 加 `bridge_python_logging`
- 在 dev Datadog 账号测

### Phase 3 —— 抽出独立 package（1 周）
- 把 `backend/obs_sdk/` 移到新 repo `propio-obs-sdk/`
- CI：lint、typecheck、unit test
- v0.1.0 发到内部 pypi
- propio 改用 `pip install propio-obs-sdk`

### Phase 4 —— 第二个 agent 接入（看团队节奏）
- 选 scheduling agent（或风险最小的）
- 写它的 `observability.yml`
- 在生命周期点加 `obs.init_agent()` + verb
- 验证 dashboard / trace 正确出现

### Phase 5 —— 可选：加深 OTel 整合
- SDK 现在已经基于 OTel；这阶段做的是把 propio 自己平台的 summary store 也对接 OTel pipeline
- 让 cross-agent 分析有数据基础

### Phase 6 —— Auto-dashboard / alert 模板
- 见 §13
- 上面 Phase 1-5 跑稳后才做

---

## 13. 自动 Dashboard / Alert 模板（Phase 6+，延后）

**目标**：`init_agent(config)` 第一次为新 agent 跑时，SDK 调每个后端的 admin API 自动建 dashboard 和 alert。新 agent → 5 个 dashboard + N 个 alert，**不用手点 UI**。

### 13.1 5 个 dashboard 模板

| 模板 | 层 | 宿主 | 关键面板 |
|---|---|---|---|
| **Reliability** | Platform | Datadog | error rate、request rate、top errors、dropped exports、audio upload failures |
| **Latency** | Platform | Datadog | p50/p95/p99 of `request_finished.duration_ms`、`model_finished.latency_ms`、`tool_finished.latency_ms`、`audio_first_byte.first_audio_ms` |
| **Quality** | Product | LangSmith UI（+ Agent Observability Platform summary store，future） | `task_success_rate`、`answer_grounded_rate`、`escalation_rate`、evaluator score histogram |
| **Cost** | Platform/Product | Datadog（+ PG summary fallback，future） | tokens/request、$/request、$/tenant、$/model_provider |
| **Voice** *（仅 `modality: voice`）* | Product | Datadog（+ Agent Observability Platform UI，future） | `first_audio_latency` p50/p95、`barge_in_recovery_rate`、按 voice 的 `tts_latency`、播放成功率 |

所有 dashboard 用 `agent_id`、`tenant_id`、`version` 模板化 —— 一个 dashboard 默认显示所有 agent，可以过滤到一个。跨 agent dashboard 用同模板不加过滤。

### 13.2 默认告警模板

| 告警 | 层 | 来源 | 阈值 | 路由 |
|---|---|---|---|---|
| 错误率 spike | Platform | Datadog APM monitor | > 1% 持续 5 分 | AI Platform oncall (PagerDuty) |
| p95 延迟回归 | Platform | Datadog APM monitor | p95 > 5s 持续 10 分 | AI Platform oncall |
| 成本超预算 | Platform | Datadog metric monitor | per agent 配置 | AI Platform + Finance Slack |
| 质量回归 | Product | LangSmith feedback alert | task_success WoW 跌 > 5pp | 产品 team Slack（**不**去 oncall） |
| 音频上传失败 | Platform | Datadog metric on `audio_upload_failure_rate` | > 0.1% 持续 15 分 | AI Platform oncall |
| Evaluator 失败率 | Quality infra | LangSmith alert | evaluator error > 5% | AI Platform Slack |

注意 **oncall-paging**（platform，分钟级行动）vs **Slack-notification**（product，天级行动）的故意区分。见 §11.5。

### 13.3 Provisioning API

**Datadog** —— `POST /api/v1/dashboard` 和 `POST /api/v1/monitor`，JSON 模板按 `agent_id` 参数化。SDK 模板放在 `propio_obs/templates/datadog/*.json`。

**LangSmith** —— `langsmith.Client.create_project(...)`；按 config 里 `quality_metric` 注册 evaluator 定义。Alert 通过 LangSmith project alert API（webhook 进我们 incident channel）。

**Agent Observability Platform UI** *（future）* —— 写一行配置进 admin DB；UI 自动发现新 agent。v1 不交付 UI，但 SDK 仍写 admin DB 行，让未来 UI 上线时已有数据。

### 13.4 为什么延后到 Phase 6+

- 模板会随我们了解什么该监控演进。v1 锁死 = 后面重写
- 每个后端 admin API 不简单；每后端 2-3 周
- 不是 SDK adoption 阻塞项 —— 前 2-3 个 agent 手动配 dashboard 也行，反而能告诉我们模板该长什么样

到时实现 `obs.bootstrap_backends(config, force=False)` —— opt-in、幂等。已有 agent 跑可原地更新 dashboard（模板版本通过 dashboard tag 管）。

---

## 14. 风险 & 缓解

| 风险 | 概率 | 影响 | 缓解 |
|---|---|---|---|
| **SDK 成关键路径** —— 后端延迟影响 agent | 中 | 高 | 异步队列+硬超时；verb 路径绝不 `await` exporter；满了丢 |
| **PII 泄露** —— 音频/transcript/user 输入发到 SaaS | 中 | 高 | `behavior.redaction.pii_fields`；pre-export hook 自定义脱敏；新后端要 legal review |
| **成本爆** —— 付费后端按 trace/log 计 | 中 | 中 | 按 channel 采样；非关键 channel 生产采 0.1（跟 team review）；track `propio_obs.exports_total` |
| **版本漂** —— LangSmith / ddtrace 出 breaking change | 高 | 中 | Pin minor；SDK CI 跑最新 + pinned。Adapter 改动走 patch |
| **Adapter 复杂度涨** —— N agent × M backend edge case | 中 | 中 | 严格 adapter Protocol 接口；每 adapter 自己 fixture |
| **音频 attachment 大** —— 大 WAV 拖慢上传 | 低 | 低 | 每 attachment 上限 60s；超了截或丢（propio 已实现 `_USER_AUDIO_MAX_BYTES = 2MB`） |
| **Routing 配错** —— agent 静默丢观测 | 中 | 中 | `init_agent()` 在某 channel 没任何 enabled backend 时 warn；SDK 提供 health-check verb |
| **PG SQLite 并发** | 低 | 低 | Phase 3+ 移 PG。SQLite 够 v0.1 |

---

## 15. 已决决策 & v2 路线

设计阶段提的问题已解决。记录决定 + 理由，避免后人重新争论。

### 15.1 Wire 层 —— **OpenTelemetry**（已定，v1）

**决定**：v1 用 OTel 作为底层传输。Agent 发 OTel spans / logs / metrics；OTel Collector fan-out 到 LangSmith、Datadog 等。

**理由**：后端列表会涨（LangSmith + Datadog APM + Logs + Metrics + Propio DB 已经，更多在路上）。每后端一个 native adapter 维护成本翻倍。OTel + Collector 让 SDK adapter 数=1，后端选择交给运维。LangSmith、Datadog、Langfuse、New Relic 都原生收 OTLP。

**接受的取舍**：OTel span model 跟 LangSmith run tree 不 1:1；轻微翻译在 Collector / 我们 LangSmith viewer 处理。音频 attachment 不是标准 OTel 特性 —— §8.4 的 out-of-band S3 路径解决。

### 15.2 采样 —— **v1 全 100%**（已定）

**决定**：所有 channel 1.0。生产暂不调。

**理由**：没成本模型。最便宜的建模法是先无采样跑一个月看真实数字。现在采样反而遮住做采样决定所需的数据。

**触发改变**：月度账单超过某个阈值（一个月生产后定）；或 `voice_event` channel 超过 X spans/天。Collector 端 tail-sampling 给 non-error / non-slow 是第一手段（错误全留，成功采样）。

### 15.3 质量评分 —— **LangSmith scheduled evaluator**（已定）

**决定**：Agent **不**算 quality 分。`record_quality()` 留给 deterministic 检查（如「tool 是否返回非空」）的快路，但主路径是：
1. Agent 通过 OTel 把 LLM trace（input + output）发到 LangSmith
2. **LangSmith 定时跑 evaluator**（在 LangSmith project 配 —— 每小时，model-as-judge 或自定义 Python）
3. Observability 平台通过 `langsmith.Client.list_feedback(...)` 定时拉分回来
4. 分进我们 analytics 做 dashboard / 告警

**理由**：

1. **解耦**：Evaluator 慢（LLM-as-judge 几秒）。Inline 跑会 block `finish_request` 或抢 agent 资源。LangSmith 调度把它移出关键路径
2. **集中定义**：「task_success」在跨 agent 应该一个意思。在 LangSmith project 里定义一次（vs 每个 agent codebase 一次）减少漂移
3. **改 evaluator 不改 agent 代码**：调 evaluator prompt 是 LangSmith UI 改动；agent 照常跑

**意味着**：SDK v1 有个小 `quality_score_puller` 组件，定时从 LangSmith 拉 feedback 进我们 analytics DB。**不**在 agent 关键路径。

### 15.4 音频保留 —— **S3 lifecycle，不在 LangSmith**（已定）

**决定**：音频上 S3（按 §8.4），不附到 LangSmith。保留期由 S3 Lifecycle policy 管（默认：30d Standard → 90d IA → 1y Glacier → 删）。

**理由**：
- LangSmith 保留受 LangChain 条款管（不清晰、可变）。S3 lifecycle 完全自己掌控
- 隐私：音频是 PII。S3 我们自己控删除时机（GDPR / customer right-to-be-forgotten）
- 成本：S3 Glacier ~$0.004/GB-月 vs LangSmith bundled storage（按 trace 收，不按 byte）
- 可审计：谁访问的音频？S3 access log + CloudTrail。LangSmith audit 有限

LangSmith 只看到 OTel span 上的 **S3 key**（`audio.s3_key`），不看到 bytes。我们 observability 前端按需签 presigned URL 给播放。

### 15.5 多租户 `customer_id` —— **session 级别，SDK 自动传播**（已定）

**决定**（按 propio 数据 team）：customer 每 session 固定，跨 session 才变。SDK 在该 session 第一次 `start_request(metadata={"customer_id": X})` 接受 customer_id，**自动**传播到该 session 后续所有 verb。

**为什么 session 级别不是 per-event**：
- Propio 确认 session 不会中途切 customer。Agent 每个 verb 都重传是 ceremony
- SDK 内一个 `{session_id: customer_id}` cache 就行。v1 实现

**Schema 改动**：`customer_id` 加为 `audio_recordings` 列、Propio DB `sessions` 表索引、每 OTel span resource 属性。见 §9。

**v1 不上 `with obs.tags(customer_id=...)` block 的原因**：那是 contextvar-based 传播（更灵活，对任何 tag 都行，不只 customer_id）。**v2** 做 —— 见 15.7。

### 15.6 Sync vs async API —— **sync verb + atexit**（已定）

**决定**：所有 verb 同步立即返回。SDK auto-register `atexit` hook 进程关时刷。`obs.flush(timeout_ms)` 显式逃生口。

**理由**：Propio agent 都是 long-running server（voice gateway 是 FastAPI；将来 scheduling/support 类似）。Long-running process 走 atexit cleanly（SIGTERM / 优雅关闭）—— hook 触发，OTel batch processor + S3 队列 flush。

**什么时候不行**：SIGKILL、OOM、Lambda timeout。这种 atexit 不跑。OTel BatchSpanProcessor 默认每 5s flush，最坏丢 ~5s 数据。不完美但 v1 接受。

**v1 不加 async 变体的原因**：
- 没人用得上的 case 双倍 API surface
- Async 正确性侵入性强 —— 每个 helper / test / example 都要双倍
- Pythonic team 真要 await，可以 `await asyncio.to_thread(obs.flush)`

### 15.7 延后到 v2（记录原因）

非 v1 问题但很可能出现。记下来让 v1 不挡未来路。

#### 15.7.1 v2：高 QPS FastAPI 用的 async verb 变体

**做啥**：加 `record_tool_async`、`start_request_async` 等 —— 真正的 coroutine（v1 sync verb 虽非阻塞但理论上跑在 calling thread）。

**为什么延后**：

- v1 sync verb 微秒级返回（就 dataclass + queue push）。普通 agent workload（≤100 RPS/process）测不出来
- 真 async 要端到端 async OTel exporter + async S3 upload —— `boto3` 是 sync 的，要 `aioboto3`，多依赖多 bug
- API surface 翻倍、文档翻倍。早熟优化

**重审触发**：某 team 跑 >500 RPS/process 报告 SDK overhead 出现在 flame graph。在那之前 v1「快 sync + batched async export」是对的。

**到时怎么实现**：Verb 层很薄（dataclass + queue push）。Async 等价物机械加上去。不破坏 caller —— v1 sync verb 留着。

#### 15.7.2 v2：contextvars 自动 tag 传播

**做啥**：把显式 `metadata={"customer_id": X}` 替换成隐式 contextvars 传播（OTel context 用的同一种机制）：

```python
# v2 草图
with obs.tags(customer_id="hospital_a", request_priority="high"):
    req = obs.start_request(...)
    obs.record_tool(req, ...)   # 自动带 customer_id + request_priority
    # ... 所有嵌套调用继承
```

**为什么延后**：
- v1 只有一个这种 tag（`customer_id`），SDK session cache 已经搞定。整套 contextvar 机制对一个 tag 太重
- contextvars 跟 asyncio task spawning 的整合要小心（每个 `asyncio.create_task` 应当复制 context —— stdlib 这么做，第三方库有时坏掉）
- v1 团队偏好显式 —— 知道事件上每个 tag 是什么因为自己传的

**重审触发**：3+ 个 tag 需要跨 verb 传播；或 team 抱怨 `metadata=` 参数烦

**到时怎么实现**：
1. 加 `obs.tags(**kwargs) -> ContextManager` 基于 `contextvars.ContextVar`
2. 每 verb 读当前 context 合并到 event 后 emit
3. 现有显式 `metadata={...}` 仍然 work（覆盖 / 增强 context）

向后兼容。v1 通过给 SDK 干净的 event 构造层奠基。

#### 15.7.3 propio_one：`record_quality()` 评分钩子（Stage B+ 跟进）

**做啥**：在 propio voice agent 的 `_process_turn` 里加 `obs.record_quality(req, metric, value=...)` 调用，给确定性 / 快路评分（fast-path deterministic checks）。

**为什么延后**：

- Stage B 只做 verb 迁移，不引入新的业务语义
- 主路径还是 §15.3 的「LangSmith 定时 evaluator」—— evaluator 读 trace 的 input/output 自己算分，不需要 agent 主动调
- `record_quality()` 是补充快路，适合「tool 是否返回非空」「response 是否解析成功」等**不需要 LLM-judge** 的确定性检查

**重审触发**：产品 / AI team 列出第一批 propio voice agent 想盯的「快路」product metric（比如 `bootstrap_success` —— any-mode 是否成功解析出目标语言；`response_parseable` —— 是否拿到合法 JSON）。

**到时怎么实现**：

1. 在 `observability.yml`（或当前的 inline dict）的 `quality_metrics` 列表登记这些 metric 名
2. 在 `_process_turn` 适当点位调 `obs.record_quality(req, "bootstrap_success", value=...)`
3. LangSmith UI 里这些分会作为 `quality.<metric>` 属性挂在 parent run 上，加上 evaluator 算出来的分（`metric.evaluated` key），形成完整产品质量视图

**与 evaluator 定义的关系**：跟 §15.8 第 2 项（第一批 evaluator 定义）是同一波产品输入触发的工作 —— 决定「我们关心哪些 product metric、哪些靠快路、哪些靠 evaluator」。

---

### 15.8 真正还 open 的项

到本版本只剩 2 个真正未决：

1. **Conversation 滚动策略** —— `conversation_id` 在 session 内何时 reset？(a) caller 显式控制；(b) 不活跃 N 分钟自动 reset；(c) agent context-clear 时自动 reset。v1 只支持 (a)；(b)(c) 看产品输入
2. **第一批 evaluator 定义** —— 第一个 LangSmith project 配啥 evaluator？（`task_success` LLM-judge、`answer_grounded` retrieval check 等）—— 要产品 / AI team 输入到底测什么。**不**阻塞 SDK 上线；SDK 接受 LangSmith 返回的任何分

---

## 16. Repository Layout

```
propio-obs-sdk/
├── pyproject.toml
├── README.md
├── CHANGELOG.md
├── src/
│   └── propio_obs/
│       ├── __init__.py            # 暴露 verb + helper
│       ├── api.py                 # 6 个 verb
│       ├── config.py              # AgentConfig（pydantic）
│       ├── ids.py                 # request_id / trace_id mint
│       ├── request.py             # Request handle dataclass
│       ├── router.py              # channel → exporters 分发
│       ├── queue.py               # 异步导出 worker
│       ├── redaction.py           # PII 脱敏
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
│           └── postgres_db.py
└── tests/
    ├── unit/
    ├── integration/
    └── fixtures/
        └── observability.example.yml
```

---

## 17. 端到端演练

新 team 做一个 chat agent。完整接入：

### Step 1 —— 安装

```bash
pip install propio-obs-sdk
```

### Step 2 —— 写 `observability.yml`

```yaml
agent:
  agent_id: docs_chat
  agent_type: chat_agent
  modality: text
  service: docs-bot
  default_tags:
    team: docs
    env: prod

quality_metrics: [task_success, answer_helpful]   # LangSmith 定时算

otel:
  endpoint: http://localhost:4317
  service_name: docs-bot

backends:
  langsmith:
    enabled: true
    api_key_env: LANGSMITH_API_KEY
    project: docs-chat-prod
    fetch_evaluator_scores: true                  # 通过 REST 拉分

routing:
  llm_trace: [otel]
  tool_call: [otel]
  log:       [otel]
```

### Step 3 —— 设环境变量

```bash
export LANGSMITH_API_KEY=lsv2_...
export DD_API_KEY=dd_...
```

### Step 4 —— 业务代码接入

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

        results = search_docs(body.question)
        obs.record_tool(req, "search_docs",
                        input={"q": body.question},
                        output={"hits": len(results)})

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

### Step 5 —— 观测

- **LangSmith** → `docs-chat-prod` project（通过 OTel Collector → OTLP）：每 request 是一个 parent span，子 span 包 `search_docs`（tool）和 LLM call。Token、latency、prompt 全可见。Scheduled evaluator 每小时填 `task_success` / `answer_helpful` 分
- **Datadog Logs** → 搜 `service:docs-bot env:prod`（通过 OTel Collector → Datadog）：每个 `logger.info` 和 `logger.exception` 都索引，带 `request_id`、`agent_id`、`customer_id`、`trace_id` tag
- **Agent Observability Platform** *（future，post-v1）* → 将定时从 LangSmith Feedback API 拉分进我们 analytics dashboard。v1 阶段直接在 LangSmith UI 看分

接入总成本：**1 yaml + 1 init + 4 verb 调用**。零 vendor SDK import、零手动 span 管理、零 boilerplate。

---

## Appendix A —— Verb 类型签名

```python
# src/propio_obs/api.py

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
    value: Union[float, bool, None] = None,
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
def langchain_callback() -> Any: ...
def attach_openai_realtime(request: Request, ws: Any) -> None: ...
def log(request: Request, level: str, message: str, **kwargs: Any) -> None: ...
def flush(timeout_ms: int = 5000) -> None: ...
```

---

## Appendix B —— 内部 Event Schema

```python
@dataclass
class Event:
    channel: str                          # "llm_trace" / "voice_event" 等
    event_type: str                       # "request_start" / "tool_call" / "speech_end" 等
    timestamp_ns: int                     # monotonic-ish UTC ns
    agent_id: str
    request_id: str
    session_id: Optional[str]
    turn_id: Optional[str]
    trace_id: str                         # OTel 128-bit hex
    parent_id: Optional[str]              # 嵌套 run
    payload: dict[str, Any]               # channel 特定数据
    attachments: dict[str, "Attachment"]  # name → (mime, bytes)
    tags: dict[str, str]                  # default_tags + per-event 增量
```

每个 exporter 知道怎么映射到自己 native 格式。

---

## Appendix C —— 标准化 Checklist

5 件事每个新 agent 自动从 SDK 继承。任何一项缺失 = SDK 配错。

### ✅ 1. 统一命名
SDK 强制这些字段名，agent 不能改：
- `agent_id`、`agent_name`、`agent_version`、`agent_type`、`modality`
- `service`、`version`、`env`、`region`、`team`
- `tenant_id`、`session_id`、`conversation_id`、`request_id`、`turn_id`、`trace_id`
- `model_provider`、`model_name`、`model_version`
- `tool_name`、`workflow_name`
- `user_id`

（完整表见 §9。）

### ✅ 2. 统一事件 schema
固定事件分类：
- 通用：`request_started`、`model_started`、`model_finished`、`tool_started`、`tool_finished`、`quality_scored`、`request_finished`
- Voice：`asr_started`、`asr_partial`、`asr_finalized`、`barge_in_detected`、`tts_started`、`audio_first_byte`、`audio_playback_finished`、`tts_finished`

（完整表见 §10.5。）

### ✅ 3. 默认 dashboard 模板
每 agent 在 `init_agent()` 自动建 5 个 dashboard：
- Reliability（Datadog）
- Latency（Datadog）
- Quality（LangSmith + summary store）
- Cost（Datadog + summary store）
- Voice —— 仅 `modality: voice`

（完整 spec 见 §13.1。）

### ✅ 4. 默认告警模板
按受众和紧急性分：
- **Oncall-paging**（Platform）：错误率、p95 延迟、音频上传失败
- **Slack-notification**（Product）：质量回归、evaluator 失败、成本超预算

（完整 spec 见 §13.2。）

### ✅ 5. Platform vs Product metric 分层
- Platform metric = 技术健康 → Datadog、oncall、分钟级
- Product metric = 业务结果 → LangSmith + summary store、产品 team、日/周节奏
- `record_quality()` 只收 product metric；platform metric SDK 自动发

（完整理由见 §11.5。）

### 怎么验证新 agent 通过标准化

```bash
# init_agent() 跑过之后
python -m propio_obs.lint observability.yml
```

期望输出：

```
✓ 必填关联键全在 default_tags
✓ quality_metrics 列表只含 product metric（无 platform metric）
✓ Provisioned 5 个 Datadog dashboard
✓ Provisioned LangSmith project，evaluator: task_success, answer_grounded
✓ 标准告警模板注册（3 platform、2 product）
✓ Schema lint 干净
```

任一 check 失败 = agent 不可上 prod。

---

## Appendix D —— Glossary

| 术语 | 含义 |
|---|---|
| **Verb** | SDK 暴露的 high-level 动作函数（`init_agent`、`start_request` 等）。叫 "verb" 因为它们是命令式动作，类比 REST HTTP verb |
| **Channel** | 一种事件类别（`llm_trace`、`tool_call`、`log` 等）。Routing config 决定 channel → backend 映射 |
| **Backend** | 事件去向（LangSmith、Datadog APM、Datadog Logs、Propio DB 等）|
| **Exporter** | SDK 中负责一个 backend 的 adapter |
| **Fan-out** | 一个事件并行发到多个 backend |
| **OpenTelemetry (OTel)** | vendor-neutral observability 标准 + SDK。多个后端原生收 OTLP。v1 用作 wire 层 |
| **OTLP** | OpenTelemetry Protocol —— OTel 的 wire 格式 |
| **Run tree** | LangSmith 概念 —— LLM application run 的层级记录，parent / child run 表示嵌套调用 |
| **Span** | OTel / APM 概念 —— 一段计时工作单元。Parent / child 关系组成 trace |
| **Trace** | 一个逻辑 request 的完整 span 树 |
| **Attachment** | 二进制 blob（音频、图片、文件）附加到 trace/run，后端 UI 可 inline 看 |
| **Agent Observability Platform** | propio 的远期目标平台 —— SDK 之外的 cross-agent 汇总 + drill-down hub。**v1 不做** |
