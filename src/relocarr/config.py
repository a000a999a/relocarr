from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Container-level settings, set via environment variables.

    Per-app, per-destination, and per-client config (added through the setup
    wizard) lives in the database instead — see db.py. This class only covers
    things that must be known before the database can even be opened.
    """

    model_config = SettingsConfigDict(env_prefix="RELOCARR_")

    config_dir: Path = Path("/config")
    host: str = "0.0.0.0"
    port: int = 8686
    auth_disabled: bool = False
    log_level: str = "INFO"

    @property
    def db_path(self) -> Path:
        return self.config_dir / "relocarr.db"

    @property
    def log_path(self) -> Path:
        return self.config_dir / "logs" / "relocarr.log"


settings = Settings()
