from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import keyring

from src.config import PROJECT_ROOT, Settings
from src.logging_config import redact_text
from src.tiktok.oauth import keyring_service
from src.tiktok.safety import EndpointPolicy
from src.tiktok.video_performance import (
    REQUIRED_SCOPE,
    VideoPerformanceError,
    _assert_scope,
    _latest_shop_context,
    _request_page,
    _validate_window,
    _write_immutable_json,
    granted_scopes,
)


class AnalyticsError(RuntimeError):
    pass


@dataclass(frozen=True)
class AnalyticsDataset:
    capability: str
    path: str
    list_key: str = ""
    paginated: bool = False


DATASETS = {
    "shop_product_performance": AnalyticsDataset(
        capability="shop_product_performance",
        path="/analytics/202605/shop_products/performance",
        list_key="products",
        paginated=True,
    ),
    "shop_sku_performance": AnalyticsDataset(
        capability="shop_sku_performance",
        path="/analytics/202509/shop_skus/performance",
        list_key="skus",
        paginated=True,
    ),
    "product_performance_detail": AnalyticsDataset(
        capability="product_performance_detail",
        path="/analytics/202509/shop_products/{resource_id}/performance",
    ),
    "shop_video_performance_detail": AnalyticsDataset(
        capability="shop_video_performance_detail_v202509",
        path="/analytics/202509/shop_videos/{resource_id}/performance",
    ),
    "shop_video_performance_overview": AnalyticsDataset(
        capability="shop_video_performance_overview",
        path="/analytics/202509/shop_videos/overview_performance",
    ),
    "shop_video_product_performance": AnalyticsDataset(
        capability="shop_video_product_performance_v202409",
        path="/analytics/202409/shop_videos/{resource_id}/products/performance",
        list_key="products",
        paginated=True,
    ),
    "shop_live_performance_list": AnalyticsDataset(
        capability="shop_live_performance_list",
        path="/analytics/202509/shop_lives/performance",
        list_key="live_stream_sessions",
        paginated=True,
    ),
    "shop_live_performance_overview": AnalyticsDataset(
        capability="shop_live_performance_overview",
        path="/analytics/202509/shop_lives/overview_performance",
    ),
    "shop_live_product_performance": AnalyticsDataset(
        capability="shop_live_product_performance_v202512",
        path="/analytics/202512/shop/{resource_id}/products_performance",
        list_key="products",
    ),
    "shop_performance_hourly": AnalyticsDataset(
        capability="shop_performance_hourly",
        path="/analytics/202510/shop/performance/{date}/performance_per_hour",
    ),
}


def fetch_analytics(
    settings: Settings,
    *,
    dataset: str,
    start_date: str,
    end_date_exclusive: str,
    resource_id: str = "",
    account_type: str = "ALL",
    today: bool = False,
    page_size: int = 100,
    max_pages: int = 100,
) -> dict[str, Any]:
    definition = DATASETS.get(dataset)
    if definition is None:
        raise AnalyticsError(f"不支持的数据集：{dataset}")
    if today and dataset != "shop_video_performance_overview":
        raise AnalyticsError("today=true 仅允许用于店铺视频表现总览")
    _validate_window(start_date, end_date_exclusive)
    if not 1 <= page_size <= 100 or not 1 <= max_pages <= 100:
        raise AnalyticsError("page_size/max_pages 超出安全范围")
    try:
        _assert_scope(settings)
    except VideoPerformanceError as exc:
        raise AnalyticsError(str(exc)) from exc
    if not keyring.get_password(keyring_service(settings), "access_token"):
        raise AnalyticsError("尚无 seller access_token，请先完成 OAuth 授权")
    if not settings.tts_app_key or not settings.tts_app_secret:
        raise AnalyticsError("TTS_APP_KEY/TTS_APP_SECRET 未配置")
    shop = _latest_shop_context(settings)
    if "{resource_id}" in definition.path:
        if not resource_id.isdigit():
            raise AnalyticsError(f"{dataset} 需要纯数字 resource_id")
        api_path = definition.path.format(resource_id=resource_id)
    elif "{date}" in definition.path:
        api_path = definition.path.format(date=start_date)
    else:
        api_path = definition.path
    url = f"{settings.effective_tts_base_url.rstrip('/')}{api_path}"
    EndpointPolicy.from_project_config(settings).assert_allowed(
        capability=definition.capability,
        method="GET",
        url=url,
        mutating=False,
    )
    query: dict[str, str | int] = {
        "app_key": settings.tts_app_key,
        "shop_cipher": shop["shop_cipher"],
        "currency": "USD",
    }
    if dataset not in {
        "shop_performance_hourly",
        "shop_live_product_performance",
    }:
        query["start_date_ge"] = start_date
        query["end_date_lt"] = end_date_exclusive
    if today:
        query["today"] = "true"
    if definition.paginated:
        query.update(page_size=page_size, sort_field="gmv", sort_order="DESC")
    if dataset in {
        "shop_video_performance_overview",
        "shop_live_performance_overview",
        "product_performance_detail",
        "shop_video_performance_detail",
    }:
        query["granularity"] = "ALL"
    if dataset in {
        "shop_video_performance_overview",
        "shop_live_performance_overview",
        "shop_live_performance_list",
    }:
        allowed_account_types = {
            "ALL", "OFFICIAL_ACCOUNTS", "MARKETING_ACCOUNTS", "AFFILIATE_ACCOUNTS",
        }
        if account_type not in allowed_account_types:
            raise AnalyticsError(f"account_type 值无效：{account_type}")
        query["account_type"] = account_type
    if dataset in {"shop_product_performance", "shop_sku_performance"}:
        query["product_status_filter"] = "ALL"
    payloads: list[dict[str, Any]] = []
    rows: list[Any] = []
    next_page_token = ""
    latest_available_date: str | None = None
    with httpx.Client(timeout=30) as client:
        for _ in range(max_pages if definition.paginated else 1):
            payload = _request_page(
                client,
                settings=settings,
                url=url,
                base_query=query,
                page_token=next_page_token,
            )
            payloads.append(payload)
            data = payload.get("data") or {}
            latest_available_date = (
                str(data.get("latest_available_date") or "")
                if isinstance(data, dict)
                else ""
            ) or latest_available_date
            if definition.paginated:
                page_rows = data.get(definition.list_key) if isinstance(data, dict) else None
                if (
                    page_rows is None
                    and isinstance(data, dict)
                    and int(data.get("total_count") or 0) == 0
                ):
                    page_rows = []
                if not isinstance(page_rows, list):
                    available = (
                        ",".join(sorted(str(key) for key in data))
                        if isinstance(data, dict)
                        else type(data).__name__
                    )
                    raise AnalyticsError(
                        f"{dataset} 响应缺少 data.{definition.list_key} 数组；"
                        f"可用键：{available or '无'}"
                    )
                rows.extend(page_rows)
                next_page_token = str(data.get("next_page_token") or "")
                if not next_page_token:
                    break
            else:
                if definition.list_key:
                    page_rows = data.get(definition.list_key) if isinstance(data, dict) else None
                    if page_rows is None and isinstance(data, dict):
                        page_rows = []
                    if not isinstance(page_rows, list):
                        raise AnalyticsError(
                            f"{dataset} 响应缺少 data.{definition.list_key} 数组"
                        )
                    rows = page_rows
                else:
                    rows = [data]
                break

    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    date_key = fetched_at[:10]
    raw_dir = (
        PROJECT_ROOT / "data" / "raw" / "tiktok" / date_key / "seller" / dataset
    )
    raw_dir.mkdir(parents=True, exist_ok=True)
    request_ids: list[str] = []
    raw_paths: list[str] = []
    hashes: dict[str, str] = {}
    for page_number, payload in enumerate(payloads, start=1):
        request_id = str(payload.get("request_id") or uuid.uuid4())
        raw_path = raw_dir / f"{request_id}_page_{page_number}.json"
        request_ids.append(request_id)
        raw_paths.append(str(raw_path))
        hashes[str(raw_path)] = _write_immutable_json(raw_path, payload)
    normalized_path = (
        PROJECT_ROOT
        / "data"
        / "normalized"
        / dataset
        / shop["shop_id"]
        / date_key
        / (
            f"{resource_id + '_' if resource_id else ''}{start_date}_{end_date_exclusive}"
            f"{'_' + account_type.lower() if dataset in {'shop_video_performance_overview', 'shop_live_performance_list'} else ''}"
            f"{'_today' if today else ''}.json"
        )
    )
    normalized_path.parent.mkdir(parents=True, exist_ok=True)
    normalized_path.write_text(
        json.dumps(rows, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return {
        "ok": True,
        "dataset": dataset,
        "shop": {"id": shop["shop_id"], "name": shop["shop_name"]},
        "window_start": start_date,
        "window_end_exclusive": end_date_exclusive,
        "fetched_at": fetched_at,
        "rows": rows,
        "row_count": len(rows),
        "exact_duplicate_count": 0,
        "conflicting_duplicate_ids": [],
        "request_ids": request_ids,
        "raw_source_paths": raw_paths,
        "raw_source_sha256": hashes,
        "normalized_source_path": str(normalized_path),
        "required_scope": [REQUIRED_SCOPE],
        "granted_scope": granted_scopes(settings),
        "missing_capabilities": [],
        "errors": [],
        "pagination_truncated": bool(next_page_token),
        "latest_available_date": latest_available_date,
        "account_type": (
            account_type
            if dataset in {
                "shop_video_performance_overview",
                "shop_live_performance_overview",
                "shop_live_performance_list",
            }
            else None
        ),
        "today": today,
    }


def failed_analytics_contract(
    *,
    dataset: str,
    start_date: str,
    end_date_exclusive: str,
    error: Exception,
    settings: Settings | None = None,
) -> dict[str, Any]:
    scopes = granted_scopes(settings)
    return {
        "ok": False,
        "dataset": dataset,
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
        "missing_capabilities": (
            [REQUIRED_SCOPE] if REQUIRED_SCOPE not in scopes else []
        ),
        "errors": [redact_text(error)],
    }
