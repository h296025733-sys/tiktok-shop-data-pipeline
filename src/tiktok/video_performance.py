from __future__ import annotations

import csv
import hashlib
import json
import logging
import sqlite3
import time
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import keyring

from src.config import PROJECT_ROOT, Settings
from src.state import STATE_DB, initialize_state_db
from src.logging_config import redact_text
from src.tiktok.oauth import keyring_service, refresh_token
from src.tiktok.safety import EndpointPolicy
from src.tiktok.signing import generate_signature


VIDEO_PERFORMANCE_PATHS = {
    "202605": "/analytics/202605/shop_videos/performance",
    "202509": "/analytics/202509/shop_videos/performance",
}
REQUIRED_SCOPE = "data.shop_analytics.public.read"
LOGGER = logging.getLogger(__name__)
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


class VideoPerformanceError(RuntimeError):
    pass


def _parse_iso_date(value: str, label: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise VideoPerformanceError(f"{label} 必须是 YYYY-MM-DD") from exc


def _validate_window(start_date: str, end_date_exclusive: str) -> None:
    start = _parse_iso_date(start_date, "start_date")
    end = _parse_iso_date(end_date_exclusive, "end_date_exclusive")
    days = (end - start).days
    if days <= 0:
        raise VideoPerformanceError("end_date_exclusive 必须晚于 start_date")
    if days > 365:
        raise VideoPerformanceError("单次查询窗口不能超过 365 天")


def _token_metadata(settings: Settings | None = None) -> dict[str, Any]:
    resolved_settings = settings or Settings()
    raw = keyring.get_password(
        keyring_service(resolved_settings), "token_metadata"
    ) or "{}"
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise VideoPerformanceError("本地 Token 元数据损坏，请重新授权") from exc
    return payload if isinstance(payload, dict) else {}


def _assert_scope(settings: Settings | None = None) -> None:
    metadata = _token_metadata(settings) if settings is not None else _token_metadata()
    scopes = metadata.get("granted_scopes") or []
    if REQUIRED_SCOPE not in scopes:
        raise VideoPerformanceError(
            f"当前 seller token 不包含必需 Scope：{REQUIRED_SCOPE}"
        )


def granted_scopes(settings: Settings | None = None) -> list[str]:
    metadata = _token_metadata(settings) if settings is not None else _token_metadata()
    scopes = metadata.get("granted_scopes") or []
    return sorted(str(scope) for scope in scopes if scope)


def _latest_shop_context(settings: Settings | None = None) -> dict[str, str]:
    resolved_settings = settings or Settings()
    profile = (resolved_settings.tts_credential_profile or "default").strip()
    authorized_root = PROJECT_ROOT / "data" / "normalized" / "authorized_shops"
    candidates = list((authorized_root / profile).glob("*/shops.csv"))
    # The original single-store installation wrote date folders directly under
    # authorized_shops. Only the default credential profile may read that
    # legacy layout; named profiles never cross this boundary.
    if profile == "default":
        candidates.extend(authorized_root.glob("*/shops.csv"))
    candidates = sorted(set(candidates), reverse=True)
    wanted_id = resolved_settings.tts_shop_id.strip()
    wanted_name = resolved_settings.tts_shop_name.strip().casefold()
    for path in candidates:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        matches = [
            row
            for row in rows
            if row.get("cipher")
            and (
                (wanted_id and str(row.get("id") or "") == wanted_id)
                or (
                    not wanted_id
                    and wanted_name
                    and str(row.get("name") or "").strip().casefold()
                    == wanted_name
                )
                or (not wanted_id and not wanted_name and len(rows) == 1)
            )
        ]
        if len(matches) == 1:
            return {
                "shop_id": str(matches[0].get("id") or ""),
                "shop_name": str(matches[0].get("name") or ""),
                "shop_cipher": str(matches[0].get("cipher") or ""),
            }
    raise VideoPerformanceError(
        "未找到当前凭证配置唯一匹配的授权店铺；请先运行 tiktok shops，"
        "并配置 TTS_SHOP_ID（推荐）或 TTS_SHOP_NAME"
    )


def _flatten(prefix: str, value: Any, output: dict[str, Any]) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            child_prefix = f"{prefix}_{key}" if prefix else str(key)
            _flatten(child_prefix, child, output)
        return
    if isinstance(value, list):
        output[prefix] = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        return
    output[prefix] = value


def flatten_video(
    video: dict[str, Any],
    *,
    shop_id: str,
    start_date: str,
    end_date_exclusive: str,
    fetched_at: str,
) -> dict[str, Any]:
    row: dict[str, Any] = {}
    _flatten("", video, row)
    row["shop_id"] = shop_id
    row["query_start_date"] = start_date
    row["query_end_date_exclusive"] = end_date_exclusive
    row["fetched_at_utc"] = fetched_at
    return row


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def canonicalize_videos(
    videos: list[Any],
) -> tuple[list[dict[str, Any]], int, list[str]]:
    """Keep one exact duplicate and reject metric-conflicting duplicate IDs."""
    groups: dict[str, list[dict[str, Any]]] = {}
    anonymous: list[dict[str, Any]] = []
    for item in videos:
        if not isinstance(item, dict):
            continue
        video_id = str(item.get("id") or "").strip()
        if not video_id:
            anonymous.append(item)
            continue
        groups.setdefault(video_id, []).append(item)

    canonical: list[dict[str, Any]] = list(anonymous)
    exact_duplicate_count = 0
    conflicting_ids: list[str] = []
    for video_id, items in groups.items():
        variants: dict[str, dict[str, Any]] = {}
        for item in items:
            variants.setdefault(_canonical_json(item), item)
        exact_duplicate_count += len(items) - len(variants)
        if len(variants) > 1:
            conflicting_ids.append(video_id)
            continue
        canonical.append(next(iter(variants.values())))
    return canonical, exact_duplicate_count, sorted(conflicting_ids)


def _write_immutable_json(path: Path, payload: dict[str, Any]) -> str:
    encoded = (
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    ).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    if path.exists():
        existing = path.read_bytes()
        if existing != encoded:
            raise VideoPerformanceError(
                f"原始响应路径已存在且内容不同，拒绝覆盖：{path}"
            )
    else:
        path.write_bytes(encoded)
    sha_path = path.with_suffix(path.suffix + ".sha256")
    sha_line = f"{digest}  {path.name}\n"
    if sha_path.exists() and sha_path.read_text(encoding="ascii") != sha_line:
        raise VideoPerformanceError(f"原始响应 SHA256 旁车文件冲突：{sha_path}")
    if not sha_path.exists():
        sha_path.write_text(sha_line, encoding="ascii")
    return digest


def _request_page(
    client: httpx.Client,
    *,
    settings: Settings,
    url: str,
    base_query: dict[str, str | int],
    page_token: str,
) -> dict[str, Any]:
    refreshed = False
    last_status = 0
    for attempt in range(1, 5):
        access_token = keyring.get_password(
            keyring_service(settings), "access_token"
        ) or ""
        query = dict(base_query)
        query["timestamp"] = int(time.time())
        if page_token:
            query["page_token"] = page_token
        query["sign"] = generate_signature(
            url=url,
            query=query,
            app_secret=settings.tts_app_secret,
        )
        headers = {
            "content-type": "application/json",
            "x-tts-access-token": access_token,
        }
        try:
            response = client.get(url, params=query, headers=headers)
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            if attempt == 4:
                raise VideoPerformanceError(
                    f"TikTok API 网络/超时错误：{redact_text(exc)}"
                ) from exc
            time.sleep(min(2 ** (attempt - 1), 4))
            continue

        last_status = response.status_code
        try:
            payload = response.json()
        except ValueError:
            payload = {}

        if response.status_code == 401 and not refreshed:
            try:
                refresh_token(settings)
            except Exception as exc:
                raise VideoPerformanceError(
                    f"access_token 失效且自动刷新失败：{redact_text(exc)}"
                ) from exc
            refreshed = True
            continue
        if response.status_code in RETRYABLE_STATUS_CODES and attempt < 4:
            retry_after = response.headers.get("retry-after", "")
            try:
                delay = min(max(float(retry_after), 0.0), 10.0)
            except ValueError:
                delay = min(2 ** (attempt - 1), 4)
            time.sleep(delay)
            continue
        if response.status_code >= 400:
            code = payload.get("code", "unknown") if isinstance(payload, dict) else "unknown"
            message = (
                payload.get("message", "no JSON error message")
                if isinstance(payload, dict)
                else "no JSON error message"
            )
            request_id = (
                str(payload.get("request_id") or "")
                if isinstance(payload, dict)
                else ""
            )
            raise VideoPerformanceError(
                f"HTTP {response.status_code}; TikTok API 错误 "
                f"{redact_text(code)}: {redact_text(message)}"
                f"; request_id={redact_text(request_id) if request_id else 'missing'}"
            )
        if not isinstance(payload, dict) or payload.get("code") != 0:
            code = payload.get("code", "unknown") if isinstance(payload, dict) else "unknown"
            message = (
                payload.get("message", "unknown response")
                if isinstance(payload, dict)
                else "unknown response"
            )
            raise VideoPerformanceError(
                f"TikTok API 错误 {redact_text(code)}: {redact_text(message)}"
            )
        return payload
    raise VideoPerformanceError(f"TikTok API 重试耗尽（HTTP {last_status}）")


def _extract_page_videos(data: Any) -> list[Any]:
    if not isinstance(data, dict):
        raise VideoPerformanceError("视频表现响应缺少 data 对象")
    page_videos = data.get("videos")
    if page_videos is None and data.get("total_count") == 0:
        return []
    if not isinstance(page_videos, list):
        raise VideoPerformanceError("视频表现响应缺少 data.videos 数组")
    return page_videos


def fetch_shop_video_performance(
    settings: Settings,
    *,
    start_date: str,
    end_date_exclusive: str,
    page_size: int = 10,
    currency: str = "USD",
    account_type: str = "ALL",
    api_version: str = "202605",
    max_pages: int = 100,
) -> dict[str, Any]:
    _validate_window(start_date, end_date_exclusive)
    if not 1 <= page_size <= 100:
        raise VideoPerformanceError("page_size 必须在 1 到 100 之间")
    if currency not in {"USD", "LOCAL"}:
        raise VideoPerformanceError("currency 只能是 USD 或 LOCAL")
    if account_type not in {
        "ALL",
        "OFFICIAL_ACCOUNTS",
        "MARKETING_ACCOUNTS",
        "AFFILIATE_ACCOUNTS",
    }:
        raise VideoPerformanceError("account_type 值无效")
    if api_version not in VIDEO_PERFORMANCE_PATHS:
        raise VideoPerformanceError("api_version 只能是 202605 或 202509")
    if not 1 <= max_pages <= 100:
        raise VideoPerformanceError("max_pages 必须在 1 到 100 之间")
    _assert_scope(settings)

    access_token = keyring.get_password(
        keyring_service(settings), "access_token"
    ) or ""
    if not access_token:
        raise VideoPerformanceError("尚无 seller access_token，请先完成 OAuth 授权")
    if not settings.tts_app_key or not settings.tts_app_secret:
        raise VideoPerformanceError("TTS_APP_KEY/TTS_APP_SECRET 未配置")

    shop = _latest_shop_context(settings)
    api_path = VIDEO_PERFORMANCE_PATHS[api_version]
    url = f"{settings.effective_tts_base_url.rstrip('/')}{api_path}"
    EndpointPolicy.from_project_config(settings).assert_allowed(
        capability=(
            "shop_video_performance"
            if api_version == "202605"
            else "shop_video_performance_v202509"
        ),
        method="GET",
        url=url,
        mutating=False,
    )

    base_query: dict[str, str | int] = {
        "app_key": settings.tts_app_key,
        "shop_cipher": shop["shop_cipher"],
        "start_date_ge": start_date,
        "end_date_lt": end_date_exclusive,
        "page_size": page_size,
        "sort_field": "gmv",
        "sort_order": "DESC",
        "currency": currency,
        "account_type": account_type,
    }
    payloads: list[dict[str, Any]] = []
    videos: list[Any] = []
    next_page_token = ""
    latest_available_date: str | None = None
    with httpx.Client(timeout=30) as client:
        for _page_number in range(1, max_pages + 1):
            try:
                payload = _request_page(
                    client,
                    settings=settings,
                    url=url,
                    base_query=base_query,
                    page_token=next_page_token,
                )
            except VideoPerformanceError as exc:
                raise VideoPerformanceError(
                    f"视频表现第 {_page_number} 页失败：{exc}"
                ) from exc
            data = payload.get("data") or {}
            page_videos = _extract_page_videos(data)
            payloads.append(payload)
            videos.extend(page_videos)
            latest_available_date = (
                str(data.get("latest_available_date") or "")
                if isinstance(data, dict)
                else ""
            ) or latest_available_date
            next_page_token = (
                str(data.get("next_page_token") or "")
                if isinstance(data, dict)
                else ""
            )
            if not next_page_token:
                break

    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    canonical_videos, exact_duplicate_count, conflicting_duplicate_ids = (
        canonicalize_videos(videos)
    )
    rows = [
        flatten_video(
            video,
            shop_id=shop["shop_id"],
            start_date=start_date,
            end_date_exclusive=end_date_exclusive,
            fetched_at=fetched_at,
        )
        for video in canonical_videos
    ]
    for row in rows:
        row["api_version"] = api_version
    preferred = [
        "id",
        "title",
        "username",
        "creator_open_id",
        "creator_user_name",
        "creator_nick_name",
        "creator_author_type",
        "video_post_time",
        "duration",
        "hash_tags",
        "gmv_amount",
        "gmv_currency",
        "gpm_amount",
        "gpm_currency",
        "sku_orders",
        "items_sold",
        "views",
        "click_through_rate",
        "shop_id",
        "query_start_date",
        "query_end_date_exclusive",
        "fetched_at_utc",
        "api_version",
    ]
    discovered = sorted({key for row in rows for key in row})
    columns = [key for key in preferred if key in discovered]
    columns.extend(key for key in discovered if key not in columns)

    date_key = fetched_at[:10]
    raw_dir = (
        PROJECT_ROOT
        / "data"
        / "raw"
        / "tiktok"
        / date_key
        / "seller"
        / "shop_video_performance"
    )
    raw_dir.mkdir(parents=True, exist_ok=True)
    request_ids: list[str] = []
    raw_paths: list[str] = []
    raw_sha256: dict[str, str] = {}
    for page_number, payload in enumerate(payloads, start=1):
        request_id = str(payload.get("request_id") or uuid.uuid4())
        request_ids.append(request_id)
        raw_path = raw_dir / f"{request_id}_page_{page_number}.json"
        digest = _write_immutable_json(raw_path, payload)
        raw_paths.append(str(raw_path))
        raw_sha256[str(raw_path)] = digest

    normalized_path = (
        PROJECT_ROOT
        / "data"
        / "normalized"
        / "shop_video_performance"
        / shop["shop_id"]
        / date_key
        / f"videos_v{api_version}_{start_date}_{end_date_exclusive}.csv"
    )
    normalized_path.parent.mkdir(parents=True, exist_ok=True)
    with normalized_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    initialize_state_db(STATE_DB)
    with sqlite3.connect(STATE_DB) as connection:
        connection.execute(
            """
            INSERT INTO sync_jobs (
                job_id, shop_id, dataset, window_start, window_end,
                last_success_at, rows_received, rows_written, rows_skipped,
                status, source_path
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid.uuid4()),
                shop["shop_id"],
                "shop_video_performance",
                start_date,
                end_date_exclusive,
                fetched_at,
                len(videos),
                len(rows),
                len(videos) - len(rows),
                "success",
                str(raw_dir),
            ),
        )

    LOGGER.info(
        "shop_video_performance succeeded pages=%d raw_rows=%d rows=%d duplicates=%d conflicts=%d",
        len(payloads),
        len(videos),
        len(rows),
        exact_duplicate_count,
        len(conflicting_duplicate_ids),
    )
    return {
        "ok": True,
        "dataset": "shop_video_performance",
        "shop": {
            "id": shop["shop_id"],
            "name": shop["shop_name"],
        },
        "window_start": start_date,
        "window_end_exclusive": end_date_exclusive,
        "fetched_at": fetched_at,
        "rows": rows,
        "row_count": len(rows),
        "raw_row_count": len(videos),
        "exact_duplicate_count": exact_duplicate_count,
        "conflicting_duplicate_ids": conflicting_duplicate_ids,
        "request_ids": request_ids,
        "raw_source_paths": raw_paths,
        "raw_source_sha256": raw_sha256,
        "normalized_source_path": str(normalized_path),
        "required_scope": [REQUIRED_SCOPE],
        "granted_scope": granted_scopes(settings),
        "missing_capabilities": [],
        "errors": [],
        "api_version": api_version,
        "page_count": len(payloads),
        "columns": columns,
        "latest_available_date": latest_available_date,
        "next_page_token_present": bool(next_page_token),
        "pagination_truncated": bool(next_page_token),
    }


def failed_contract(
    *,
    start_date: str,
    end_date_exclusive: str,
    error: Exception,
    settings: Settings | None = None,
) -> dict[str, Any]:
    scopes = granted_scopes(settings)
    missing = [REQUIRED_SCOPE] if REQUIRED_SCOPE not in scopes else []
    return {
        "ok": False,
        "dataset": "shop_video_performance",
        "shop": None,
        "window_start": start_date,
        "window_end_exclusive": end_date_exclusive,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "rows": [],
        "row_count": 0,
        "exact_duplicate_count": 0,
        "conflicting_duplicate_ids": [],
        "request_ids": [],
        "raw_source_paths": [],
        "normalized_source_path": None,
        "required_scope": [REQUIRED_SCOPE],
        "granted_scope": scopes,
        "missing_capabilities": missing,
        "errors": [redact_text(error)],
    }
