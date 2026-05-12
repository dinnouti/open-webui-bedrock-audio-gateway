import asyncio
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import boto3
import structlog
from botocore.config import Config as BotoConfig
from botocore.exceptions import (
    ClientError,
    ConnectionError as BotoConnectionError,
    EndpointConnectionError,
    ReadTimeoutError,
)

from app.config import settings

log = structlog.get_logger()

# Polly synthesize_speech accepts up to 3000 billed characters
POLLY_MAX_CHARS = 3000

# Fallback voice list used when describe_voices API is unavailable (e.g. missing IAM permission)
_FALLBACK_VOICES = [
    {"id": "Matthew", "name": "Matthew (en-US, Male)"},
    {"id": "Ruth", "name": "Ruth (en-US, Female)"},
    {"id": "Stephen", "name": "Stephen (en-US, Male)"},
    {"id": "Danielle", "name": "Danielle (en-US, Female)"},
    {"id": "Amy", "name": "Amy (en-GB, Female)"},
    {"id": "Brian", "name": "Brian (en-GB, Male)"},
]

# Transient Polly errors worth retrying
_RETRYABLE_ERRORS = (
    BotoConnectionError,
    EndpointConnectionError,
    ReadTimeoutError,
)


class _PollyClientHolder:
    """Lazy singleton that refreshes the boto3 client and voice list periodically
    so rotated credentials (e.g. instance profiles) are picked up."""

    def __init__(self):
        self._client = None
        self._created_at: float = 0
        self._lock = threading.Lock()
        self._refresh_seconds = settings.polly_client_refresh_seconds
        self._voices: list[dict] = []
        self._valid_voice_ids: set[str] = set()

    def get(self):
        now = time.monotonic()
        if self._client is not None and (now - self._created_at) < self._refresh_seconds:
            return self._client
        with self._lock:
            now = time.monotonic()
            if self._client is not None and (now - self._created_at) < self._refresh_seconds:
                return self._client
            boto_config = BotoConfig(
                connect_timeout=5,
                read_timeout=settings.polly_timeout_seconds,
                retries={"max_attempts": 0},
            )
            session = boto3.Session(**settings.boto_session_kwargs())
            self._client = session.client("polly", config=boto_config)
            self._created_at = time.monotonic()
            self._load_voices(self._client)
            log.debug("polly.client_refreshed")
            return self._client

    def _load_voices(self, client) -> None:
        try:
            voices = []
            paginator = client.get_paginator("describe_voices")
            for page in paginator.paginate(Engine=settings.polly_engine):
                for v in page.get("Voices", []):
                    voices.append({
                        "id": v["Id"],
                        "name": f"{v['Name']} ({v['LanguageCode']}, {v['Gender']})",
                    })
            voices.sort(key=lambda v: v["name"])
            self._voices = voices
            self._valid_voice_ids = {v["id"] for v in voices}
            log.info("polly.voices_loaded", count=len(voices))
        except Exception as e:
            log.warning("polly.voices_load_failed", error=str(e))
            if not self._voices:
                self._voices = _FALLBACK_VOICES
                self._valid_voice_ids = {v["id"] for v in _FALLBACK_VOICES}

    @property
    def voices(self) -> list[dict]:
        self.get()
        return self._voices

    @property
    def valid_voice_ids(self) -> set[str]:
        self.get()
        return self._valid_voice_ids


_polly_holder = _PollyClientHolder()
_polly_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="polly")


def _get_polly_client():
    return _polly_holder.get()


def _is_retryable_client_error(e: ClientError) -> bool:
    code = e.response.get("Error", {}).get("Code", "")
    status = e.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)
    return code == "Throttling" or status >= 500


def _synthesize_once(text: str, voice_id: str) -> bytes:
    polly = _get_polly_client()
    response = polly.synthesize_speech(
        Text=text,
        VoiceId=voice_id,
        OutputFormat="mp3",
        Engine=settings.polly_engine,
    )
    return response["AudioStream"].read()


def _synthesize_with_retry(text: str, voice_id: str) -> bytes:
    last_error: Exception | None = None
    max_attempts = settings.max_retries + 1

    for attempt in range(1, max_attempts + 1):
        try:
            data = _synthesize_once(text, voice_id)
            log.info("polly.done", voice=voice_id, bytes=len(data), attempt=attempt)
            return data

        except _RETRYABLE_ERRORS as e:
            last_error = e
            log.warning("polly.retryable_error", attempt=attempt, max_attempts=max_attempts, error=str(e))

        except ClientError as e:
            if _is_retryable_client_error(e):
                last_error = e
                log.warning("polly.throttle", attempt=attempt, max_attempts=max_attempts, error=str(e))
            else:
                raise

        if attempt < max_attempts:
            delay = settings.retry_base_delay * (2 ** (attempt - 1))
            delay += random.uniform(0, delay * 0.5)
            log.info("polly.retry", delay=round(delay, 3))
            time.sleep(delay)

    log.error("polly.all_retries_exhausted", attempts=max_attempts, error=str(last_error))
    if last_error is None:
        raise RuntimeError("Retries exhausted with no recorded error")
    raise last_error


def get_voices() -> list[dict]:
    return _polly_holder.voices


async def synthesize_speech(text: str, voice_id: str | None = None) -> bytes:
    """Convert text to speech using Amazon Polly.

    Args:
        text: Text to synthesize (max 3000 characters).
        voice_id: Polly voice ID (e.g. "Matthew"). Falls back to DEFAULT_VOICE_ID.

    Returns:
        MP3 audio bytes.

    Raises:
        ValueError: If text exceeds POLLY_MAX_CHARS.
    """
    if len(text) > POLLY_MAX_CHARS:
        raise ValueError(
            f"Text too long ({len(text)} chars). "
            f"Maximum is {POLLY_MAX_CHARS} characters."
        )
    voice = voice_id or settings.default_voice_id
    valid_ids = _polly_holder.valid_voice_ids
    if valid_ids and voice not in valid_ids:
        log.warning("polly.invalid_voice", requested=voice, fallback=settings.default_voice_id)
        voice = settings.default_voice_id
    log.info("polly.start", voice=voice, engine=settings.polly_engine, chars=len(text))
    log.debug("polly.input_text", text=text)
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_polly_executor, _synthesize_with_retry, text, voice)
