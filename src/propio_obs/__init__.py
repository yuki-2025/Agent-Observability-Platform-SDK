"""propio-obs-sdk — Propio agent observability SDK.

Public verb-level API:
    init_agent, start_request, record_tool, record_quality,
    record_voice_event, finish_request, flush

Helpers:
    wrap_llm_client, pcm16_to_wav

Backward-compat function-level API (mirrors backend/app/services/tracing.py):
    record_stt, record_tts, turn_trace, wrap_openai
"""
from .api import (
    init_agent,
    start_request,
    record_tool,
    record_quality,
    record_voice_event,
    finish_request,
    flush,
    wrap_llm_client,
    # PG-backed session lifecycle + event mirror (Phase 1 of SDK takeover
    # from monitor_service.py)
    start_session,
    end_session,
    broadcast_event,
)
from .audio import pcm16_to_wav
from .request import Request

# Function-level back-compat — existing tracing.py callers can drop-in import these.
from .exporters.langsmith import (
    record_stt,
    record_tts,
    turn_trace,
    wrap_openai,
    traceable,
    traceable_llm,
    traceable_stt,
    traceable_tts,
)

__version__ = "0.0.1"

__all__ = [
    # Verb layer
    "init_agent",
    "start_request",
    "record_tool",
    "record_quality",
    "record_voice_event",
    "finish_request",
    "flush",
    # PG event mirror + session lifecycle (replaces monitor_service)
    "start_session",
    "end_session",
    "broadcast_event",
    # Helpers
    "wrap_llm_client",
    "pcm16_to_wav",
    "Request",
    # Function-level back-compat
    "record_stt",
    "record_tts",
    "turn_trace",
    "wrap_openai",
    "traceable",
    "traceable_llm",
    "traceable_stt",
    "traceable_tts",
]
