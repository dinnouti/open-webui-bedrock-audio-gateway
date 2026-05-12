import os
from pathlib import Path
from typing import Optional

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parent.parent / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # AWS — blank values fall back to standard boto3 credential chain
    aws_access_key_id: Optional[str] = None
    aws_secret_access_key: Optional[str] = None
    aws_region: str = "us-east-1"

    # Gateway auth
    gateway_api_key: str

    # Server
    gateway_host: str = "0.0.0.0"
    gateway_port: int = 8000
    log_format: str = "console"  # "console" for dev, "json" for production
    log_level: str = "INFO"  # DEBUG, INFO, WARNING, ERROR

    # Transcribe
    transcribe_language_code: Optional[str] = "en-US"
    max_file_size_mb: int = 25

    # Polly
    default_voice_id: str = "Matthew"
    polly_engine: str = "generative"
    polly_client_refresh_seconds: int = 900

    # Reliability
    transcribe_timeout_seconds: int = 30
    polly_timeout_seconds: int = 15
    max_retries: int = 2
    retry_base_delay: float = 0.5

    # Rate limiting
    rate_limit: str = "60/minute"  # slowapi format, e.g. "60/minute", "10/second"

    # Request size limit (bytes) — rejects before buffering; 0 = disabled
    max_request_bytes: int = 26_214_400  # 25 MB + overhead

    @model_validator(mode="after")
    def _check_credentials_pair(self) -> "Settings":
        if bool(self.aws_access_key_id) != bool(self.aws_secret_access_key):
            raise ValueError(
                "Both aws_access_key_id and aws_secret_access_key must be set together, "
                "or both must be omitted (to use instance profile / environment credentials)."
            )
        return self

    @property
    def max_file_size_bytes(self) -> int:
        return self.max_file_size_mb * 1024 * 1024

    def boto_session_kwargs(self) -> dict:
        """Return kwargs for boto3.Session(), omitting empty credentials."""
        kwargs: dict = {}
        if self.aws_access_key_id:
            kwargs["aws_access_key_id"] = self.aws_access_key_id
        if self.aws_secret_access_key:
            kwargs["aws_secret_access_key"] = self.aws_secret_access_key
        if self.aws_region:
            kwargs["region_name"] = self.aws_region
        return kwargs

    def export_aws_env(self):
        """Export AWS credentials to os.environ for SDKs that don't accept explicit creds
        (e.g. amazon-transcribe streaming SDK uses CRT which reads env vars directly)."""
        if self.aws_access_key_id:
            os.environ.setdefault("AWS_ACCESS_KEY_ID", self.aws_access_key_id)
        if self.aws_secret_access_key:
            os.environ.setdefault("AWS_SECRET_ACCESS_KEY", self.aws_secret_access_key)
        if self.aws_region:
            os.environ.setdefault("AWS_DEFAULT_REGION", self.aws_region)


settings = Settings()  # type: ignore[call-arg]
