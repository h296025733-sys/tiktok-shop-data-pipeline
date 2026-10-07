from __future__ import annotations

import platform
import socket
from pathlib import Path
from urllib.parse import urlparse

import httpx

from src.config import PROJECT_ROOT, Settings
from src.state import state_db_is_healthy


def _official_domain_check(settings: Settings) -> dict[str, object]:
    base_url = settings.effective_tts_base_url
    parsed = urlparse(base_url)
    if parsed.scheme != "https" or not parsed.hostname:
        return {"status": "失败", "detail": "TTS_BASE_URL 不是有效 HTTPS 地址"}
    try:
        socket.getaddrinfo(parsed.hostname, 443)
        with httpx.Client(timeout=5, follow_redirects=False) as client:
            response = client.get(base_url)
        return {"status": "可访问", "http_status": response.status_code}
    except (OSError, httpx.HTTPError) as exc:
        return {"status": "不可访问", "detail": type(exc).__name__}


def run_doctor(settings: Settings) -> dict[str, object]:
    allowlist_path = (
        PROJECT_ROOT / "config" / "datasets" / "tiktok_read_endpoints.json"
    )
    return {
        "python_version": platform.python_version(),
        "project_path": str(PROJECT_ROOT),
        "configuration_valid_for_local_read_only": settings.local_configuration_valid,
        "external_credentials_configured": settings.external_credentials_configured,
        "safe_mode": settings.safe_mode,
        "tiktok_write_actions_enabled": settings.tiktok_write_actions_enabled,
        "feishu_production_write_enabled": settings.feishu_production_write_enabled,
        "deepseek_enabled": settings.deepseek_enabled,
        "browser_automation_enabled": settings.browser_automation_enabled,
        "allow_pii": settings.allow_pii,
        "sqlite_healthy": state_db_is_healthy(),
        "endpoint_allowlist_present": allowlist_path.is_file(),
        "tiktok_official_domain": _official_domain_check(settings),
    }
