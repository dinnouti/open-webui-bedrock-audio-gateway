import hmac
import time
from contextlib import asynccontextmanager
from typing import Optional

import structlog
from fastapi import APIRouter, Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from app.audio import transcode_to_pcm
from app.config import settings
from app.logging_config import new_request_id, setup_logging
from app.polly import get_voices, synthesize_speech
from app.transcribe import transcribe_audio

setup_logging()
log = structlog.get_logger()

limiter = Limiter(key_func=get_remote_address, default_limits=[settings.rate_limit])


# ---------------------------------------------------------------------------
# Lifespan — startup / shutdown
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.export_aws_env()
    log.info("gateway.started", host=settings.gateway_host, port=settings.gateway_port)
    yield
    log.info("gateway.shutdown")


# ---------------------------------------------------------------------------
# App + router
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Open WebUI Audio Gateway",
    description="OpenAI-compatible audio API backed by Amazon Transcribe (STT) and Amazon Polly (TTS).",
    version="0.1.0",
    lifespan=lifespan,
)

app.state.limiter = limiter

v1 = APIRouter(prefix="/v1", tags=["v1"])


# ---------------------------------------------------------------------------
# Error helpers
# ---------------------------------------------------------------------------

def _error_body(message: str, code: str, error_type: str = "invalid_request_error") -> dict:
    """OpenAI-compatible error envelope."""
    return {"error": {"message": message, "type": error_type, "code": code}}


# ---------------------------------------------------------------------------
# Global exception handlers
# ---------------------------------------------------------------------------

@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    """Preserve FastAPI HTTPExceptions as-is (must be registered before the catch-all)."""
    content = exc.detail if isinstance(exc.detail, dict) else _error_body(str(exc.detail), "http_error")
    return JSONResponse(status_code=exc.status_code, content=content)


@app.exception_handler(ValueError)
async def value_error_handler(request: Request, exc: ValueError):
    return JSONResponse(status_code=400, content=_error_body(str(exc), "invalid_value"))


@app.exception_handler(Exception)
async def unhandled_error_handler(request: Request, exc: Exception):
    log.error("unhandled_error", error=str(exc), exc_info=(type(exc), exc, exc.__traceback__))
    return JSONResponse(status_code=500, content=_error_body("Internal server error.", "internal_error"))


@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request: Request, exc: RateLimitExceeded):
    log.warning("rate_limit.exceeded", client=get_remote_address(request))
    return JSONResponse(status_code=429, content=_error_body("Rate limit exceeded. Try again later.", "rate_limit"))


# ---------------------------------------------------------------------------
# Middleware — request size limit
# ---------------------------------------------------------------------------

@app.middleware("http")
async def request_size_limit_middleware(request: Request, call_next):
    if settings.max_request_bytes > 0:
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > settings.max_request_bytes:
                    return JSONResponse(
                        status_code=413,
                        content=_error_body(
                            f"Request too large. Maximum is {settings.max_request_bytes} bytes.",
                            "request_too_large",
                        ),
                    )
            except ValueError:
                pass
    return await call_next(request)


# ---------------------------------------------------------------------------
# Middleware — request ID + access log
# ---------------------------------------------------------------------------

@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    rid = new_request_id()
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(request_id=rid)

    start = time.monotonic()
    log.info("request.start", method=request.method, path=request.url.path)

    response = await call_next(request)

    elapsed_ms = (time.monotonic() - start) * 1000
    log.info("request.end", status=response.status_code, elapsed_ms=round(elapsed_ms, 1))

    response.headers["X-Request-ID"] = rid
    return response


# ---------------------------------------------------------------------------
# Auth — dependency-based
# ---------------------------------------------------------------------------

_bearer_scheme = HTTPBearer()


async def verify_token(
    credentials: HTTPAuthorizationCredentials = Depends(_bearer_scheme),
):
    if not hmac.compare_digest(credentials.credentials, settings.gateway_api_key):
        raise HTTPException(status_code=401, detail=_error_body("Invalid API key", "invalid_api_key"))


# ---------------------------------------------------------------------------
# STT — POST /v1/audio/transcriptions
# ---------------------------------------------------------------------------

@v1.post(
    "/audio/transcriptions",
    summary="Transcribe audio",
    description="Upload an audio file and receive a text transcription via Amazon Transcribe streaming.",
    dependencies=[Depends(verify_token)],
)
async def transcriptions(
    file: UploadFile = File(...),
    model: Optional[str] = Form(None),
    language: Optional[str] = Form(None),
):
    """Accept an audio file upload and return a text transcription.

    The file is transcoded to PCM via ffmpeg, then streamed to Amazon Transcribe.
    """
    audio_bytes = await file.read()
    if len(audio_bytes) > settings.max_file_size_bytes:
        raise HTTPException(
            status_code=400,
            detail=_error_body(f"File too large. Maximum size is {settings.max_file_size_mb}MB.", "file_too_large"),
        )
    if not audio_bytes:
        raise HTTPException(status_code=400, detail=_error_body("Empty audio file.", "missing_field"))

    log.info("stt.start", file=file.filename, size=len(audio_bytes), model=model, language=language)

    try:
        pcm_audio = await transcode_to_pcm(audio_bytes)
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=_error_body(str(e), "invalid_audio_format"))

    try:
        text = await transcribe_audio(pcm_audio, language=language)
    except Exception as e:
        log.exception("stt.error")
        raise HTTPException(status_code=502, detail=_error_body(f"Transcription failed: {e}", "transcribe_error"))

    return {"text": text}


# ---------------------------------------------------------------------------
# TTS — POST /v1/audio/speech
# ---------------------------------------------------------------------------

@v1.post(
    "/audio/speech",
    summary="Synthesize speech",
    description="Convert text to speech via Amazon Polly. Returns raw audio/mpeg bytes.",
    dependencies=[Depends(verify_token)],
)
async def speech(request: Request):
    """Accept a JSON body with text and return synthesized audio/mpeg bytes via Amazon Polly."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail=_error_body("Invalid JSON body.", "missing_field"))

    text = body.get("input", "")
    if not text:
        raise HTTPException(status_code=400, detail=_error_body("Missing 'input' field.", "missing_field"))

    voice_id = body.get("voice", settings.default_voice_id)
    log.info("tts.start", voice=voice_id, chars=len(text))

    try:
        audio_bytes = await synthesize_speech(text=text, voice_id=voice_id)
    except ValueError:
        raise  # re-raise for global ValueError handler
    except Exception as e:
        log.exception("tts.error")
        raise HTTPException(status_code=502, detail=_error_body(f"Speech synthesis failed: {e}", "polly_error"))

    log.info("tts.done", bytes=len(audio_bytes))
    return Response(content=audio_bytes, media_type="audio/mpeg")


# ---------------------------------------------------------------------------
# Discovery — GET /v1/audio/models, GET /v1/audio/voices
# No auth required — Open WebUI calls these to populate settings dropdowns.
# ---------------------------------------------------------------------------

@v1.get(
    "/audio/models",
    summary="List TTS models",
    description="Returns available TTS models for the settings dropdown.",
)
async def models():
    """Return the list of available TTS models (static)."""
    return {"models": [{"id": "polly-generative"}]}


@v1.get(
    "/audio/voices",
    summary="List voices",
    description="Returns available Amazon Polly generative voices.",
)
async def voices():
    """Return the list of available Polly generative voices (static)."""
    return {"voices": get_voices()}


app.include_router(v1)


# ---------------------------------------------------------------------------
# Health — outside versioned router
# ---------------------------------------------------------------------------

@app.get("/health", tags=["health"], summary="Health check")
async def health():
    """Liveness probe for load balancers."""
    return {"status": "ok"}
