"""Application settings (environment-driven; no secrets in source)."""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings. Dev defaults are local-only; deploy overrides via env/secrets."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    env: str = "dev"
    app_name: str = "inventory-platform"
    api_prefix: str = "/api/v1"
    log_level: str = "INFO"

    database_url: str = "postgresql+asyncpg://inventory:inventory@localhost:5432/inventory"
    redis_url: str = "redis://localhost:6379/0"

    jwt_secret: str = "dev-only-change-me-32-bytes-minimum!!"
    jwt_issuer: str = "inventory-platform"
    jwt_audience: str = "inventory-platform-api"
    access_token_ttl_seconds: int = 900
    refresh_token_ttl_seconds: int = 1209600

    dex_issuer: str = "http://localhost:5556/dex"
    dex_client_id: str = "inventory-platform"
    dex_client_secret: str = "dev-only-change-me"

    s3_endpoint: str = "http://localhost:9000"
    s3_access_key: str = "minio"
    s3_secret_key: str = "minio123"
    s3_bucket: str = "inventory"

    def public_dict(self) -> dict[str, str]:
        """Non-secret settings suitable for diagnostics."""
        return {
            "env": self.env,
            "app_name": self.app_name,
            "api_prefix": self.api_prefix,
            "database_url": _redact_url(self.database_url),
            "redis_url": _redact_url(self.redis_url),
            "dex_issuer": self.dex_issuer,
            "s3_endpoint": self.s3_endpoint,
        }


def _redact_url(url: str) -> str:
    """Hide credentials embedded in a URL."""
    if "://" not in url:
        return url
    scheme, rest = url.split("://", 1)
    if "@" in rest:
        credentials, host = rest.split("@", 1)
        user = credentials.split(":", 1)[0]
        return f"{scheme}://{user}:***@{host}"
    return url


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
