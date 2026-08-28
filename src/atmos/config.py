"""Runtime configuration. Everything comes from the environment."""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Sent with every outbound request so operators can see who we are and reach us.
# The repo URL is the contact point on purpose, so no personal address is exposed.
CONTACT_URL = "https://github.com/bewanderer/atmos"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ATMOS_", env_file=".env")

    database_url: str

    # Raw archive. S3 compatible, normally Cloudflare R2.
    archive_endpoint: str
    archive_bucket: str
    archive_access_key: str
    archive_secret_key: str

    # Politeness. Being blocked is the only failure we cannot recover from,
    # so these defaults are conservative on purpose.
    request_timeout_s: float = 30.0
    min_interval_s: float = 2.0
    max_retries: int = 4
    backoff_base_s: float = 2.0

    version: str = Field(default="0.1.0")

    @property
    def user_agent(self) -> str:
        return f"Atmos/{self.version} (+{CONTACT_URL})"


settings = Settings()  # type: ignore[call-arg]
