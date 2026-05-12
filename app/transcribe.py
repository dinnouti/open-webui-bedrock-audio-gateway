import asyncio
import random
from typing import Optional

import structlog
from amazon_transcribe.client import TranscribeStreamingClient
from amazon_transcribe.handlers import TranscriptResultStreamHandler
from amazon_transcribe.model import TranscriptEvent

from app.config import settings

log = structlog.get_logger()

CHUNK_SIZE = 8 * 1024  # 8KB chunks for streaming

# ISO-639-1 to BCP-47 mapping (default dialect when only short code is provided)
LANGUAGE_MAP = {
    "af": "af-ZA", "ar": "ar-SA", "ca": "ca-ES", "cs": "cs-CZ",
    "da": "da-DK", "de": "de-DE", "el": "el-GR", "en": "en-US",
    "es": "es-US", "eu": "eu-ES", "fa": "fa-IR", "fi": "fi-FI",
    "fr": "fr-FR", "gl": "gl-ES", "he": "he-IL", "hi": "hi-IN",
    "hr": "hr-HR", "id": "id-ID", "it": "it-IT", "ja": "ja-JP",
    "ko": "ko-KR", "lv": "lv-LV", "ms": "ms-MY", "nl": "nl-NL",
    "no": "no-NO", "pl": "pl-PL", "pt": "pt-BR", "ro": "ro-RO",
    "ru": "ru-RU", "sk": "sk-SK", "so": "so-SO", "sr": "sr-RS",
    "sv": "sv-SE", "th": "th-TH", "tl": "tl-PH", "uk": "uk-UA",
    "vi": "vi-VN", "zh": "zh-CN", "zu": "zu-ZA",
}

# Languages for auto-detect mode (one dialect per language, per AWS requirement).
# Source: https://docs.aws.amazon.com/transcribe/latest/dg/supported-languages.html
_STREAMING_LANGUAGE_OPTIONS = [
    "af-ZA", "ar-SA", "ca-ES", "cs-CZ", "da-DK", "de-DE", "el-GR",
    "en-US", "es-US", "eu-ES", "fa-IR", "fi-FI", "fr-FR", "gl-ES",
    "he-IL", "hi-IN", "hr-HR", "id-ID", "it-IT", "ja-JP", "ko-KR",
    "lv-LV", "ms-MY", "nl-NL", "no-NO", "pl-PL", "pt-BR", "ro-RO",
    "ru-RU", "sk-SK", "so-SO", "sr-RS", "sv-SE", "th-TH", "tl-PH",
    "uk-UA", "vi-VN", "zh-CN", "zu-ZA",
]

# Transient errors worth retrying
_RETRYABLE_ERRORS = (
    ConnectionError,
    OSError,
)


class _TranscriptCollector(TranscriptResultStreamHandler):
    """Collects final (non-partial) transcript segments."""

    def __init__(self, output_stream):
        super().__init__(output_stream)
        self.parts: list[str] = []

    async def handle_transcript_event(self, transcript_event: TranscriptEvent):
        for result in transcript_event.transcript.results:
            if not result.is_partial and result.alternatives:
                self.parts.append(result.alternatives[0].transcript)

    @property
    def text(self) -> str:
        return " ".join(self.parts).strip()


def _resolve_language(language: Optional[str]) -> Optional[str]:
    """Resolve language: request field → .env default → None (auto-detect)."""
    code = language or settings.transcribe_language_code
    if not code:
        return None
    if len(code) <= 3 and code.lower() in LANGUAGE_MAP:
        return LANGUAGE_MAP[code.lower()]
    return code


async def _transcribe_once(pcm_audio: bytes, lang_code: Optional[str]) -> str:
    """Single transcription attempt with timeout."""
    client = TranscribeStreamingClient(region=settings.aws_region)

    start_kwargs = {
        "media_sample_rate_hz": 16000,
        "media_encoding": "pcm",
    }
    if lang_code:
        start_kwargs["language_code"] = lang_code
    else:
        start_kwargs["language_code"] = None
        start_kwargs["identify_language"] = True
        start_kwargs["language_options"] = _STREAMING_LANGUAGE_OPTIONS

    stream = await client.start_stream_transcription(**start_kwargs)

    async def _feed_audio():
        for offset in range(0, len(pcm_audio), CHUNK_SIZE):
            chunk = pcm_audio[offset : offset + CHUNK_SIZE]
            await stream.input_stream.send_audio_event(audio_chunk=chunk)
        await stream.input_stream.end_stream()

    collector = _TranscriptCollector(stream.output_stream)

    try:
        await asyncio.wait_for(
            asyncio.gather(_feed_audio(), collector.handle_events()),
            timeout=settings.transcribe_timeout_seconds,
        )
    except Exception:
        try:
            await stream.input_stream.end_stream()
        except Exception:
            pass
        raise

    return collector.text


async def transcribe_audio(pcm_audio: bytes, language: Optional[str] = None) -> str:
    """Stream PCM audio to Amazon Transcribe and return the transcript.

    Args:
        pcm_audio: Raw PCM bytes (16kHz, 16-bit, mono).
        language: Optional ISO-639-1 code (e.g. "en") or BCP-47 code (e.g. "en-US").
                  Falls back to TRANSCRIBE_LANGUAGE_CODE, then auto-detect.

    Returns:
        Transcribed text. Empty string if audio is silent.

    Raises:
        TimeoutError: If transcription exceeds TRANSCRIBE_TIMEOUT_SECONDS.
        ConnectionError: If all retry attempts fail.
    """
    lang_code = _resolve_language(language)
    last_error: Exception | None = None
    max_attempts = settings.max_retries + 1

    for attempt in range(1, max_attempts + 1):
        try:
            text = await _transcribe_once(pcm_audio, lang_code)
            log.info("transcribe.done", chars=len(text), attempt=attempt)
            log.debug("transcribe.output_text", text=text)
            return text

        except asyncio.TimeoutError as e:
            last_error = TimeoutError(
                f"Transcription timed out after {settings.transcribe_timeout_seconds}s"
            )
            last_error.__cause__ = e
            log.warning("transcribe.timeout", attempt=attempt, max_attempts=max_attempts)

        except _RETRYABLE_ERRORS as e:
            last_error = e
            log.warning("transcribe.retryable_error", attempt=attempt, max_attempts=max_attempts, error=str(e))

        if attempt < max_attempts:
            delay = settings.retry_base_delay * (2 ** (attempt - 1))
            delay += random.uniform(0, delay * 0.5)
            log.info("transcribe.retry", delay=round(delay, 3))
            await asyncio.sleep(delay)

    log.error("transcribe.all_retries_exhausted", attempts=max_attempts, error=str(last_error))
    if last_error is None:
        raise RuntimeError("Retries exhausted with no recorded error")
    raise last_error
