from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from src.config import PROJECT_ROOT


KEY_VALUE_SECRET_RE = re.compile(
    r"(?i)\b(app[_ -]?secret|access[_ -]?token|refresh[_ -]?token|auth(?:orization)?[_ -]?code|code)"
    r"(\s*[:=]\s*)([^\s,;&]+)"
)
BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")


def mask_secret(value: str) -> str:
    value = str(value)
    if not value:
        return ""
    if len(value) < 12:
        return "****"
    return f"{value[:4]}…{value[-4:]}"


def redact_text(value: Any) -> str:
    text = str(value)
    text = BEARER_RE.sub("Bearer ****", text)
    return KEY_VALUE_SECRET_RE.sub(
        lambda match: f"{match.group(1)}{match.group(2)}{mask_secret(match.group(3))}",
        text,
    )


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # Render first so redacting a format placeholder such as
        # ``authorization_code=%s`` cannot break logging interpolation.
        record.msg = redact_text(record.getMessage())
        record.args = ()
        return True


def configure_logging(level: str = "INFO") -> None:
    log_dir = PROJECT_ROOT / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [
        logging.StreamHandler(),
        logging.FileHandler(log_dir / "app.log", encoding="utf-8"),
    ]
    for handler in handlers:
        handler.addFilter(RedactingFilter())
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
        )
    logging.basicConfig(level=getattr(logging, level), handlers=handlers, force=True)
    # httpx logs full request URLs at INFO. TikTok's OAuth endpoint carries
    # credentials in query parameters, so do not emit those request records.
    logging.getLogger("httpx").setLevel(logging.WARNING)
