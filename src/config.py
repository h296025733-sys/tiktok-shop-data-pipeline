from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parents[1]
OFFICIAL_TTS_BASE_URL = "https://open-api.tiktokglobalshop.com"
OFFICIAL_TTS_TOKEN_ENDPOINT = "https://auth.tiktok-shops.com/api/v2/token/get"
OFFICIAL_TTS_REFRESH_ENDPOINT = "https://auth.tiktok-shops.com/api/v2/token/refresh"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    tts_app_key: str = ""
    tts_app_secret: str = ""
    tts_redirect_uri: str = (
        "https://tiktok-shop-oauth-callback.linglily135.workers.dev/tiktok/callback"
    )
    tts_region: str = ""
    tts_credential_profile: str = "default"
    tts_shop_id: str = ""
    tts_shop_name: str = ""
    tts_base_url: str = ""
    tts_token_endpoint: str = ""

    feishu_app_id: str = ""
    feishu_app_secret: str = ""
    feishu_bitable_app_token: str = ""
    feishu_bitable_test_table_id: str = ""

    deepseek_api_key: str = ""
    deepseek_base_url: str = ""
    deepseek_model: str = ""

    safe_mode: bool = True
    tiktok_write_actions_enabled: bool = False
    feishu_production_write_enabled: bool = False
    deepseek_enabled: bool = False
    browser_automation_enabled: bool = False
    allow_pii: bool = False
    log_level: str = Field(default="INFO", pattern=r"^(DEBUG|INFO|WARNING|ERROR|CRITICAL)$")

    @property
    def external_credentials_configured(self) -> bool:
        return bool(self.tts_app_key and self.tts_app_secret)

    @property
    def effective_tts_base_url(self) -> str:
        return self.tts_base_url.strip() or OFFICIAL_TTS_BASE_URL

    @property
    def effective_tts_token_endpoint(self) -> str:
        return self.tts_token_endpoint.strip() or OFFICIAL_TTS_TOKEN_ENDPOINT

    @property
    def local_configuration_valid(self) -> bool:
        return (
            self.safe_mode
            and not self.tiktok_write_actions_enabled
            and not self.feishu_production_write_enabled
            and not self.deepseek_enabled
            and not self.browser_automation_enabled
            and not self.allow_pii
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
