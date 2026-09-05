from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    warmer_api_token: SecretStr
    warmer_state_db: Path = Path("data/warmer.db")
    stellate_url: str = "https://livia.stellate.sh"
    globalping_token: SecretStr | None = None
    warmer_concurrency: int = Field(default=8, ge=1, le=32)
    warmer_request_timeout: float = Field(default=20, ge=1, le=60)
    warmer_job_timeout: int = Field(default=900, ge=10, le=3600)
    warmer_mapping_max_age: int = Field(default=90000, ge=60, le=172800)
    warmer_probes_per_city: int = Field(default=3, ge=1, le=5)

    @field_validator("warmer_api_token")
    @classmethod
    def valid_token(cls, value: SecretStr) -> SecretStr:
        token = value.get_secret_value()
        if len(token) < 32 or not token.isascii() or any(c.isspace() for c in token):
            raise ValueError("WARMER_API_TOKEN must be at least 32 non-whitespace ASCII characters")
        return value

    @field_validator("stellate_url")
    @classmethod
    def valid_url(cls, value: str) -> str:
        parts = urlsplit(value)
        if (parts.scheme != "https" or parts.port not in (None, 443)
                or parts.username or parts.password or parts.query or parts.fragment
                or not parts.hostname or not parts.hostname.endswith(".stellate.sh")
                or parts.path not in ("", "/", "/graphql")):
            raise ValueError("Use an HTTPS Stellate service hostname on port 443, without credentials or query parameters")
        return value
