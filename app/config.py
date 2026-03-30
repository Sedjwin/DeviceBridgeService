from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    host: str = "127.0.0.1"
    port: int = 8010
    database_url: str = "sqlite+aiosqlite:///./data/devicebridge.db"

    toolgateway_url: str = "http://localhost:8006"
    toolgateway_service_key: str = ""
    usermanager_url: str = "http://localhost:8005"
    voiceservice_url: str = "http://localhost:8002"
    agentmanager_url: str = "http://localhost:8003"
    http_timeout_seconds: int = 30

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="DBS_",
        extra="ignore",
    )


settings = Settings()
