from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from src.config import PROJECT_ROOT, Settings


class SafetyViolation(RuntimeError):
    """Raised before an unsafe or unapproved network operation."""


@dataclass(frozen=True)
class AllowlistEntry:
    capability: str
    method: str
    host: str
    path: str = ""
    path_regex: str = ""


class EndpointPolicy:
    def __init__(self, settings: Settings, entries: list[AllowlistEntry]):
        self.settings = settings
        self.entries = entries

    @classmethod
    def from_project_config(cls, settings: Settings) -> "EndpointPolicy":
        config_path = PROJECT_ROOT / "config" / "datasets" / "tiktok_read_endpoints.json"
        payload = json.loads(config_path.read_text(encoding="utf-8"))
        entries = [AllowlistEntry(**item) for item in payload.get("entries", [])]
        return cls(settings=settings, entries=entries)

    def assert_allowed(
        self,
        *,
        capability: str,
        method: str,
        url: str,
        mutating: bool = False,
    ) -> None:
        if mutating:
            if self.settings.safe_mode or not self.settings.tiktok_write_actions_enabled:
                raise SafetyViolation("TikTok 写操作已被 SAFE_MODE 拦截")
            raise SafetyViolation("当前版本不实现 TikTok 写操作")

        parsed = urlparse(url)
        if parsed.scheme.lower() != "https":
            raise SafetyViolation("只允许 HTTPS endpoint")

        approved = any(
            entry.capability == capability
            and entry.method.upper() == method.upper()
            and entry.host == (parsed.hostname or "")
            and (
                (bool(entry.path) and entry.path == parsed.path)
                or (
                    bool(entry.path_regex)
                    and entry.path_regex.startswith("^")
                    and entry.path_regex.endswith("$")
                    and re.fullmatch(entry.path_regex, parsed.path) is not None
                )
            )
            for entry in self.entries
        )
        if not approved:
            raise SafetyViolation("endpoint 未进入精确只读白名单")
