from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    service_name: str = "DeviceBridgeService"
    api_prefix: str = "/api"
    host: str = "0.0.0.0"
    port: int = 8011
    public_base_url: str = "https://chip.iampc.uk:13382"

    database_url: str = "sqlite+aiosqlite:///./data/devicebridge.db"

    ai_gateway_url: str = "http://localhost:8001"
    system_basic_token: str = ""
    system_basic_model: str = "system_basic"
    mapping_llm_enabled: bool = True
    mapping_llm_on_miss: bool = True

    agentmanager_url: str = "http://localhost:8003"
    voiceservice_url: str = "http://localhost:8002"
    usermanager_url: str = "http://localhost:8005"
    usermanager_service_key: str = "change-me-service-key"
    require_auth: bool = False

    device_session_timeout_s: int = 120
    command_ack_timeout_s: float = 10.0
    device_inline_audio_max_bytes: int = 2000000

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", case_sensitive=False)


settings = Settings()
