# Changelog

## 0.1.0

Initial release.

- OpenAI-compatible STT endpoint via Amazon Transcribe streaming
- OpenAI-compatible TTS endpoint via Amazon Polly (generative engine)
- Discovery endpoints for models and voices
- Bearer token authentication with HMAC-safe comparison
- Structured logging (console/JSON) with configurable log level
- ffmpeg audio transcoding with timeout protection
- Retry logic with exponential backoff for transient AWS errors
- Refreshable Polly boto3 client (supports credential rotation)
- TTS text length validation (3000 char Polly limit)
- Rate limiting per client IP via slowapi
- Request size limit middleware
- Versioned API routes under `/v1`
- Global exception handlers with OpenAI-compatible error format
- FastAPI lifespan handler for startup/shutdown
- SSH deployment support
