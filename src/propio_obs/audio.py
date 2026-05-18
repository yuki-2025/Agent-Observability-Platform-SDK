"""Audio helpers — backend-agnostic, used by exporters for upload payloads."""
import io
import wave


def pcm16_to_wav(pcm_bytes: bytes, sample_rate: int = 16000) -> bytes:
    """Wrap raw PCM16 mono audio in a WAV container so browsers / LangSmith can play it."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm_bytes)
    return buf.getvalue()
