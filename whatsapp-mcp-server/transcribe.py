"""Speech-to-text for WhatsApp voice messages, delegated to the whisper-mcp service.

The audio file is already on disk in the shared `store` volume (populated by
the whatsapp-bridge service); we POST it to whisper-mcp's OpenAI-compatible
REST route (`/v1/audio/transcriptions`, multipart field `file`) and return the
text. Transcription itself runs on our own infrastructure (faster-whisper in
the whisper-mcp container) — no third-party API involved.

Config (env):
    WHISPER_URL               full URL of the REST route; transcription is
                              disabled (returns an error) if unset.
    WHISPER_CF_CLIENT_ID      optional Cloudflare Access service-token headers,
    WHISPER_CF_CLIENT_SECRET  needed when WHISPER_URL is the public hostname.
    WHISPER_LANGUAGE          defaults to "pt"; empty = auto-detect.
    WHISPER_TIMEOUT           seconds, defaults to 300.
"""

import logging
import os
from pathlib import Path

import httpx

logger = logging.getLogger("whatsapp-mcp")

AUDIO_EXTS = {".ogg", ".opus", ".m4a", ".mp3", ".wav", ".aac", ".amr"}


def transcribe_audio(file_path: str) -> dict:
    """Transcribe an audio file to text.

    Returns {"text": str, "language": str} on success, or {"error": str} on
    failure. Never raises — callers should check for the "error" key instead
    of wrapping this in try/except.
    """
    url = os.environ.get("WHISPER_URL", "").strip()
    if not url:
        return {"error": "transcription disabled: WHISPER_URL is not set"}

    headers = {}
    client_id = os.environ.get("WHISPER_CF_CLIENT_ID", "").strip()
    client_secret = os.environ.get("WHISPER_CF_CLIENT_SECRET", "").strip()
    if client_id and client_secret:
        headers["CF-Access-Client-Id"] = client_id
        headers["CF-Access-Client-Secret"] = client_secret

    data = {}
    language = os.environ.get("WHISPER_LANGUAGE", "pt").strip()
    if language:
        data["language"] = language

    try:
        with open(file_path, "rb") as f:
            response = httpx.post(
                url,
                headers=headers,
                data=data,
                files={"file": (Path(file_path).name, f)},
                timeout=float(os.environ.get("WHISPER_TIMEOUT", "300")),
            )
        response.raise_for_status()
        body = response.json()
        return {"text": (body.get("text") or "").strip(), "language": body.get("language")}
    except Exception as e:
        logger.exception(f"Failed to transcribe {file_path}")
        return {"error": str(e)}
