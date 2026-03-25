from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    service_name: str = "DeviceBridgeService"
    api_prefix: str = "/api"
    host: str = "0.0.0.0"
    port: int = 8011

    database_url: str = "sqlite+aiosqlite:///./data/devicebridge.db"

    agentmanager_url: str = "http://localhost:8003"
    usermanager_url: str = "http://localhost:8005"
    usermanager_service_key: str = "change-me-service-key"
    require_auth: bool = False

    device_session_timeout_s: int = 120
    command_ack_timeout_s: float = 3.0

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", case_sensitive=False)


settings = Settings()
