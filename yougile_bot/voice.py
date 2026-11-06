"""Расшифровка голосовых для «Ввода»: faster-whisper прямо на сервере, без внешних ключей.
Модель скачивается при первом голосовом (~150 МБ для base; small точнее, но берёт ~500 МБ памяти — на сервере с 2 ГБ рискованно). Размер — YG_WHISPER_MODEL."""
import asyncio
import io
import os

MODEL = os.environ.get("YG_WHISPER_MODEL", "base")
_model = None


def enabled():
    try:
        import faster_whisper  # noqa: F401
        return True
    except ImportError:
        return False


def _run(data: bytes):
    global _model
    from faster_whisper import WhisperModel
    if _model is None:
        _model = WhisperModel(MODEL, device="cpu", compute_type="int8")
    segments, _ = _model.transcribe(io.BytesIO(data), language="ru", vad_filter=True)
    return " ".join(s.text.strip() for s in segments).strip()


async def transcribe(data: bytes):
    return await asyncio.to_thread(_run, data)
