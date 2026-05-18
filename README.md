# propio-obs-sdk

Unified observability SDK for Propio agents. One Python package, one config file, one verb interface — fan-out to LangSmith / Datadog / Propio DB without each agent importing vendor SDKs directly.

> Status: **v0.0.1** — early prototype validated inside the propio voice agent. Backends supported: LangSmith. Datadog + S3 audio path coming in v0.1+.

## Install

```bash
pip install propio-obs-sdk
```

Inside the propio monorepo it's installed as an editable workspace dependency.

## Quick start

```python
import propio_obs as obs

obs.init_agent("observability.yml")  # once at process startup

req = obs.start_request(request_type="voice_turn", session_id=ws.session_id)
obs.record_voice_event(req, "stt_complete", audio_wav=user_wav)

client = obs.wrap_llm_client(AsyncOpenAI())
resp = await client.chat.completions.create(...)

obs.record_voice_event(req, "tts_complete", audio_wav=agent_wav)
obs.finish_request(req, status="success")
```

See `examples/observability.example.yml` for the config schema, and the top-level `OBS_SDK_IMPLEMENTATION_PLAN.md` for the full design doc.

## Supported in v0

- **LangSmith** — voice_turn parent run, child STT / LLM / TTS runs, audio attachments
- Function-level back-compat API (`record_stt`, `record_tts`, `turn_trace`, `wrap_openai`, `pcm16_to_wav`) so existing callers keep working

## Roadmap

- v0.1 — Datadog APM + Logs adapters, OTel emit
- v0.2 — S3 audio out-of-band path + Postgres metadata index
- v0.3 — Auto-dashboard + alert provisioning
