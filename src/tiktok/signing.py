from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse


EXCLUDED_QUERY_KEYS = {"access_token", "sign"}


def generate_signature(
    *,
    url: str,
    query: Mapping[str, Any],
    app_secret: str,
    body: Mapping[str, Any] | None = None,
    content_type: str = "application/json",
) -> str:
    """Generate the TikTok Shop official HMAC-SHA256 request signature."""
    path = urlparse(url).path
    parameter_string = "".join(
        f"{key}{query[key]}"
        for key in sorted(query)
        if key not in EXCLUDED_QUERY_KEYS
    )
    sign_string = f"{path}{parameter_string}"
    if content_type.lower() != "multipart/form-data" and body:
        sign_string += json.dumps(
            body, ensure_ascii=False, separators=(",", ":"), sort_keys=False
        )
    wrapped = f"{app_secret}{sign_string}{app_secret}"
    return hmac.new(
        app_secret.encode("utf-8"),
        wrapped.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

