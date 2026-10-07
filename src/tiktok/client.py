from __future__ import annotations

import csv
import json
import logging
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx
import keyring

from src.config import PROJECT_ROOT, Settings
from src.state import STATE_DB, initialize_state_db
from src.tiktok.oauth import keyring_service
from src.tiktok.safety import EndpointPolicy
from src.tiktok.signing import generate_signature
from src.xlsx_utils import write_rows_to_workbook


LOGGER = logging.getLogger(__name__)
AUTHORIZED_SHOPS_PATH = "/authorization/202309/shops"


class TikTokAPIError(RuntimeError):
    pass


def fetch_authorized_shops(settings: Settings) -> dict[str, object]:
    access_token = keyring.get_password(keyring_service(settings), "access_token") or ""
    if not access_token:
        raise TikTokAPIError("尚无 seller access_token；请先完成 OAuth 授权")
    if not settings.tts_app_key or not settings.tts_app_secret:
        raise TikTokAPIError("TTS_APP_KEY/TTS_APP_SECRET 未配置")

    url = f"{settings.effective_tts_base_url.rstrip('/')}{AUTHORIZED_SHOPS_PATH}"
    policy = EndpointPolicy.from_project_config(settings)
    policy.assert_allowed(
        capability="authorized_shops",
        method="GET",
        url=url,
        mutating=False,
    )

    timestamp = int(time.time())
    query: dict[str, str | int] = {
        "app_key": settings.tts_app_key,
        "timestamp": timestamp,
    }
    query["sign"] = generate_signature(
        url=url,
        query=query,
        app_secret=settings.tts_app_secret,
    )
    headers = {
        "content-type": "application/json",
        "x-tts-access-token": access_token,
    }
    with httpx.Client(timeout=20) as client:
        response = client.get(url, params=query, headers=headers)
        response.raise_for_status()
        payload = response.json()
    if not isinstance(payload, dict) or payload.get("code") != 0:
        message = payload.get("message", "unknown response") if isinstance(payload, dict) else ""
        code = payload.get("code", "unknown") if isinstance(payload, dict) else "unknown"
        raise TikTokAPIError(f"TikTok API 错误 {code}: {message}")

    shops = payload.get("data", {}).get("shops", [])
    if not isinstance(shops, list):
        raise TikTokAPIError("授权店铺响应结构无法识别")

    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    safe_rows = [
        {
            "id": str(shop.get("id") or ""),
            "name": str(shop.get("name") or ""),
            "region": str(shop.get("region") or ""),
            "seller_type": str(shop.get("seller_type") or ""),
            "cipher": str(shop.get("cipher") or ""),
            "code": str(shop.get("code") or ""),
            "fetched_at": fetched_at,
        }
        for shop in shops
        if isinstance(shop, dict)
    ]

    date_key = fetched_at[:10]
    raw_dir = (
        PROJECT_ROOT
        / "data"
        / "raw"
        / "tiktok"
        / date_key
        / "seller"
        / "authorized_shops"
    )
    raw_dir.mkdir(parents=True, exist_ok=True)
    request_id = str(payload.get("request_id") or uuid.uuid4())
    raw_path = raw_dir / f"{request_id}.json"
    raw_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    normalized_path = (
        PROJECT_ROOT
        / "data"
        / "normalized"
        / "authorized_shops"
        / (settings.tts_credential_profile or "default")
        / date_key
        / "shops.csv"
    )
    normalized_path.parent.mkdir(parents=True, exist_ok=True)
    headers_out = ["id", "name", "region", "seller_type", "cipher", "code", "fetched_at"]
    with normalized_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers_out)
        writer.writeheader()
        writer.writerows(safe_rows)

    export_path = PROJECT_ROOT / "exports" / "xlsx" / date_key / "authorized_shops.xlsx"
    write_rows_to_workbook(
        export_path,
        sheet_name="authorized_shops",
        headers=headers_out,
        rows=([row[field] for field in headers_out] for row in safe_rows),
        table_name="AuthorizedShops",
    )

    job_id = str(uuid.uuid4())
    initialize_state_db(STATE_DB)
    with sqlite3.connect(STATE_DB) as connection:
        connection.execute(
            """
            INSERT INTO sync_jobs (
                job_id, dataset, last_success_at, rows_received, rows_written,
                rows_skipped, status, source_path, export_path
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                "authorized_shops",
                fetched_at,
                len(safe_rows),
                len(safe_rows),
                0,
                "success",
                str(raw_path),
                str(export_path),
            ),
        )
    LOGGER.info(
        "Get Authorized Shops succeeded request_id=%s rows=%d",
        request_id,
        len(safe_rows),
    )
    return {
        "shop_count": len(safe_rows),
        "shops": safe_rows,
        "raw_json": str(raw_path),
        "normalized_csv": str(normalized_path),
        "export_xlsx": str(export_path),
        "request_id": request_id,
    }
