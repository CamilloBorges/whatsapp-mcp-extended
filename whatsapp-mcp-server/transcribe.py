"""Local speech-to-text for WhatsApp voice messages, using faster-whisper (CPU-only).

Runs in-process in the whatsapp-mcp container, against files already on disk
in the shared `store` volume (populated by the whatsapp-bridge service) — no
network hop needed. Model is loaded lazily on first use and kept in memory
for the life of the process.
"""

import logging
import os
import threading

logger = logging.getLogger("whatsapp-mcp")

AUDIO_EXTS = {".ogg", ".opus", ".m4a", ".mp3", ".wav", ".aac", ".amr"}

_model = None
_model_lock = threading.Lock()


def _get_model():
    global _model
    if _model is None:
        with _model_lock:
            if _model is None:
                from faster_whisper import WhisperModel

                model_size = os.environ.get("WHISPER_MODEL_SIZE", "base")
                logger.info(f"Loading faster-whisper model '{model_size}' (CPU, int8)...")
                _model = WhisperModel(model_size, device="cpu", compute_type="int8")
    return _model


def transcribe_audio(file_path: str) -> dict:
    """Transcribe an audio file to text.

    Returns {"text": str, "language": str} on success, or {"error": str} on
    failure. Never raises — callers should check for the "error" key instead
    of wrapping this in try/except.
    """
    try:
        model = _get_model()
        language = os.environ.get("WHISPER_LANGUAGE", "pt") or None
        segments, info = model.transcribe(file_path, language=language, vad_filter=True)
        text = " ".join(segment.text.strip() for segment in segments).strip()
        return {"text": text, "language": info.language}
    except Exception as e:
        logger.exception(f"Failed to transcribe {file_path}")
        return {"error": str(e)}
