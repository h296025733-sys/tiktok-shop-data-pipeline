from __future__ import annotations

import logging
import json
import re
from urllib.parse import urlparse

import httpx
import keyring

from src.config import OFFICIAL_TTS_REFRESH_ENDPOINT, Settings
from src.logging_config import mask_secret


LOGGER = logging.getLogger(__name__)
KEYRING_SERVICE = "tech-wave-tiktok-data-automation"


def keyring_service(settings: Settings) -> str:
    profile = (settings.tts_credential_profile or "default").strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,39}", profile):
        raise OAuthConfigurationError("TTS_CREDENTIAL_PROFILE格式无效")
    return KEYRING_SERVICE if profile == "default" else f"{KEYRING_SERVICE}:{profile}"


class OAuthConfigurationError(RuntimeError):
    pass


def _validate_endpoint(settings: Settings) -> str:
    endpoint = settings.effective_tts_token_endpoint
    parsed = urlparse(endpoint)
    if parsed.scheme != "https" or not parsed.hostname:
        raise OAuthConfigurationError("TTS_TOKEN_ENDPOINT 必须是明确配置的 HTTPS 地址")
    return endpoint


def _store_tokens(settings: Settings, payload: dict[str, object]) -> None:
    access_token = str(payload.get("access_token") or "")
    refresh_token = str(payload.get("refresh_token") or "")
    if not access_token:
        raise RuntimeError("官方响应未包含 access_token；未保存任何 Token")
    service = keyring_service(settings)
    keyring.set_password(service, "access_token", access_token)
    if refresh_token:
        keyring.set_password(service, "refresh_token", refresh_token)
    metadata = {
        "access_token_expire_in": payload.get("access_token_expire_in"),
        "refresh_token_expire_in": payload.get("refresh_token_expire_in"),
        "open_id": payload.get("open_id"),
        "seller_name": payload.get("seller_name"),
        "seller_base_region": payload.get("seller_base_region"),
        "user_type": payload.get("user_type"),
        "granted_scopes": payload.get("granted_scopes") or [],
    }
    keyring.set_password(
        service,
        "token_metadata",
        json.dumps(metadata, ensure_ascii=False),
    )
    LOGGER.info("Token 已安全保存：access_token=%s", mask_secret(access_token))


def exchange_code(settings: Settings, code: str) -> None:
    endpoint = _validate_endpoint(settings)
    if not settings.tts_app_key or not settings.tts_app_secret:
        raise OAuthConfigurationError("请在本机 .env 配置 TTS_APP_KEY 和 TTS_APP_SECRET")
    LOGGER.info("准备交换 authorization_code=%s", mask_secret(code))
    payload = {
        "app_key": settings.tts_app_key,
        "app_secret": settings.tts_app_secret,
        "auth_code": code,
        "grant_type": "authorized_code",
    }
    with httpx.Client(timeout=20) as client:
        response = client.get(endpoint, params=payload)
        response.raise_for_status()
        body = response.json()
    if not isinstance(body, dict) or body.get("code") != 0:
        message = body.get("message", "unknown response") if isinstance(body, dict) else ""
        raise RuntimeError(f"Token 交换失败：{message}")
    token_payload = body.get("data", {})
    if not isinstance(token_payload, dict):
        raise RuntimeError("Token 响应结构无法识别；未保存任何 Token")
    if token_payload.get("user_type") != 0:
        raise RuntimeError("授权主体不是 Seller（user_type != 0）；未保存 Token")
    _store_tokens(settings, token_payload)


def refresh_token(settings: Settings) -> None:
    endpoint = OFFICIAL_TTS_REFRESH_ENDPOINT
    service = keyring_service(settings)
    stored_refresh = keyring.get_password(service, "refresh_token")
    if not stored_refresh:
        raise OAuthConfigurationError("Windows Credential Manager 中没有 refresh_token")
    payload = {
        "app_key": settings.tts_app_key,
        "app_secret": settings.tts_app_secret,
        "refresh_token": stored_refresh,
        "grant_type": "refresh_token",
    }
    with httpx.Client(timeout=20) as client:
        response = client.get(endpoint, params=payload)
        response.raise_for_status()
        body = response.json()
    if not isinstance(body, dict) or body.get("code") != 0:
        message = body.get("message", "unknown response") if isinstance(body, dict) else ""
        raise RuntimeError(f"Token 刷新失败：{message}")
    token_payload = body.get("data", {})
    if not isinstance(token_payload, dict):
        raise RuntimeError("Token 响应结构无法识别；未保存任何 Token")
    _store_tokens(settings, token_payload)


def token_status(settings: Settings) -> dict[str, str | bool]:
    service = keyring_service(settings)
    access = keyring.get_password(service, "access_token") or ""
    refresh = keyring.get_password(service, "refresh_token") or ""
    metadata_raw = keyring.get_password(service, "token_metadata") or "{}"
    try:
        metadata = json.loads(metadata_raw)
    except json.JSONDecodeError:
        metadata = {}
    return {
        "credential_profile": settings.tts_credential_profile,
        "access_token_present": bool(access),
        "access_token_masked": mask_secret(access),
        "refresh_token_present": bool(refresh),
        "refresh_token_masked": mask_secret(refresh),
        "seller_base_region": metadata.get("seller_base_region", ""),
        "user_type": metadata.get("user_type", ""),
        "granted_scopes": metadata.get("granted_scopes", []),
    }
