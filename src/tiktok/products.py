from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx
import keyring

from src.config import PROJECT_ROOT, Settings
from src.logging_config import redact_text
from src.tiktok.oauth import keyring_service, refresh_token
from src.tiktok.safety import EndpointPolicy
from src.tiktok.signing import generate_signature
from src.tiktok.video_performance import (
    _latest_shop_context,
    _write_immutable_json,
    granted_scopes,
)


SEARCH_PRODUCTS_PATH = "/product/202502/products/search"
GET_PRODUCT_PATH = "/product/202309/products/{product_id}"
REQUIRED_SCOPE = "seller.product.basic"
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


class ProductCatalogError(RuntimeError):
    pass


def extract_product_page(
    data: Any,
    *,
    already_fetched: int,
) -> tuple[list[Any], str]:
    """Normalize TikTok's terminal product page without hiding truncated data.

    The search endpoint may return a non-empty ``next_page_token`` on the last
    populated page and then omit ``products`` on the following terminal page.
    That terminal response still repeats ``total_count``.  Accept it only when
    the rows already collected cover that total; otherwise fail closed.
    """
    if not isinstance(data, dict):
        raise ProductCatalogError("商品目录响应 data 不是对象")
    next_page_token = str(data.get("next_page_token") or "")
    page_products = data.get("products")
    if page_products is None:
        try:
            total_count = int(data.get("total_count") or 0)
        except (TypeError, ValueError) as exc:
            raise ProductCatalogError("商品目录响应 total_count 不是整数") from exc
        if already_fetched >= total_count and not next_page_token:
            return [], ""
    if not isinstance(page_products, list):
        available = ",".join(sorted(str(key) for key in data))
        raise ProductCatalogError(
            f"商品目录响应缺少 data.products 数组；可用字段：{available or '无'}"
        )
    return page_products, next_page_token


def canonicalize_products(
    products: list[Any],
) -> tuple[list[dict[str, Any]], int, list[str]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in products:
        if not isinstance(item, dict):
            continue
        product_id = str(item.get("id") or "").strip()
        if product_id:
            grouped.setdefault(product_id, []).append(item)

    canonical: list[dict[str, Any]] = []
    exact_duplicates = 0
    conflicting_ids: list[str] = []
    for product_id, items in grouped.items():
        variants = {
            json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")): item
            for item in items
        }
        exact_duplicates += len(items) - len(variants)
        if len(variants) > 1:
            conflicting_ids.append(product_id)
            continue
        canonical.append(next(iter(variants.values())))
    canonical.sort(key=lambda item: str(item.get("id") or ""))
    return canonical, exact_duplicates, sorted(conflicting_ids)


def fetch_product_catalog(
    settings: Settings,
    *,
    page_size: int = 100,
    max_pages: int = 100,
) -> dict[str, Any]:
    if not 1 <= page_size <= 100 or not 1 <= max_pages <= 100:
        raise ProductCatalogError("page_size/max_pages 超出安全范围")
    scopes = granted_scopes(settings)
    if REQUIRED_SCOPE not in scopes:
        raise ProductCatalogError(f"当前授权缺少商品目录权限：{REQUIRED_SCOPE}")
    if not keyring.get_password(keyring_service(settings), "access_token"):
        raise ProductCatalogError("尚无 seller access_token，请先完成 OAuth 授权")
    if not settings.tts_app_key or not settings.tts_app_secret:
        raise ProductCatalogError("TTS_APP_KEY/TTS_APP_SECRET 未配置")

    shop = _latest_shop_context(settings)
    url = f"{settings.effective_tts_base_url.rstrip('/')}{SEARCH_PRODUCTS_PATH}"
    EndpointPolicy.from_project_config(settings).assert_allowed(
        capability="search_products_v202502",
        method="POST",
        url=url,
        mutating=False,
    )

    payloads: list[dict[str, Any]] = []
    products: list[Any] = []
    next_page_token = ""
    body = {"status": "ALL"}
    with httpx.Client(timeout=30) as client:
        for _ in range(max_pages):
            payload = _request_page(
                client,
                settings=settings,
                url=url,
                shop_cipher=shop["shop_cipher"],
                page_size=page_size,
                page_token=next_page_token,
                body=body,
            )
            payloads.append(payload)
            data = payload.get("data") or {}
            page_products, next_page_token = extract_product_page(
                data,
                already_fetched=len(products),
            )
            products.extend(page_products)
            if not next_page_token:
                break

    canonical, exact_duplicates, conflicting_ids = canonicalize_products(products)
    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    date_key = fetched_at[:10]
    raw_dir = PROJECT_ROOT / "data" / "raw" / "tiktok" / date_key / "seller" / "product_catalog"
    raw_dir.mkdir(parents=True, exist_ok=True)
    raw_paths: list[str] = []
    hashes: dict[str, str] = {}
    for page_number, payload in enumerate(payloads, start=1):
        request_id = str(payload.get("request_id") or uuid.uuid4())
        raw_path = raw_dir / f"{request_id}_page_{page_number}.json"
        raw_paths.append(str(raw_path))
        hashes[str(raw_path)] = _write_immutable_json(raw_path, payload)

    normalized_path = (
        PROJECT_ROOT
        / "data"
        / "normalized"
        / "product_catalog"
        / shop["shop_id"]
        / date_key
        / "products.json"
    )
    normalized_path.parent.mkdir(parents=True, exist_ok=True)
    normalized_path.write_text(
        json.dumps(canonical, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return {
        "ok": True,
        "dataset": "product_catalog",
        "shop": {"id": shop["shop_id"], "name": shop["shop_name"]},
        "fetched_at": fetched_at,
        "rows": canonical,
        "row_count": len(canonical),
        "exact_duplicate_count": exact_duplicates,
        "conflicting_duplicate_ids": conflicting_ids,
        "raw_source_paths": raw_paths,
        "raw_source_sha256": hashes,
        "normalized_source_path": str(normalized_path),
        "required_scope": [REQUIRED_SCOPE],
        "granted_scope": scopes,
        "missing_capabilities": [],
        "errors": [],
        "pagination_truncated": bool(next_page_token),
    }


def fetch_product_detail(settings: Settings, *, product_id: str) -> dict[str, Any]:
    if not product_id.isdigit():
        raise ProductCatalogError("product_id 必须是纯数字")
    scopes = granted_scopes(settings)
    if REQUIRED_SCOPE not in scopes:
        raise ProductCatalogError(f"当前授权缺少商品目录权限：{REQUIRED_SCOPE}")
    access_token = keyring.get_password(keyring_service(settings), "access_token") or ""
    if not access_token:
        raise ProductCatalogError("尚无 seller access_token，请先完成 OAuth 授权")
    if not settings.tts_app_key or not settings.tts_app_secret:
        raise ProductCatalogError("TTS_APP_KEY/TTS_APP_SECRET 未配置")
    shop = _latest_shop_context(settings)
    path = GET_PRODUCT_PATH.format(product_id=product_id)
    url = f"{settings.effective_tts_base_url.rstrip('/')}{path}"
    EndpointPolicy.from_project_config(settings).assert_allowed(
        capability="get_product_v202309",
        method="GET",
        url=url,
        mutating=False,
    )
    refreshed = False
    payload: dict[str, Any] = {}
    with httpx.Client(timeout=30) as client:
        for attempt in range(1, 5):
            query: dict[str, str | int] = {
                "app_key": settings.tts_app_key,
                "shop_cipher": shop["shop_cipher"],
                "timestamp": int(time.time()),
                "locale": "en-US",
            }
            query["sign"] = generate_signature(
                url=url,
                query=query,
                app_secret=settings.tts_app_secret,
            )
            headers = {
                "content-type": "application/json",
                "x-tts-access-token": keyring.get_password(
                    keyring_service(settings), "access_token"
                ) or "",
            }
            response = client.get(url, params=query, headers=headers)
            try:
                candidate = response.json()
            except ValueError:
                candidate = {}
            payload = candidate if isinstance(candidate, dict) else {}
            if response.status_code == 401 and not refreshed:
                refresh_token(settings)
                refreshed = True
                continue
            if response.status_code in RETRYABLE_STATUS_CODES and attempt < 4:
                time.sleep(min(2 ** (attempt - 1), 4))
                continue
            code = payload.get("code", "unknown")
            message = payload.get("message", "unknown response")
            if response.status_code >= 400:
                raise ProductCatalogError(
                    f"TikTok API HTTP {response.status_code} / 错误 {code}: {redact_text(message)}"
                )
            if code != 0:
                raise ProductCatalogError(f"TikTok API 错误 {code}: {redact_text(message)}")
            break
        else:
            raise ProductCatalogError("TikTok 商品详情请求重试耗尽")

    data = payload.get("data") or {}
    if not isinstance(data, dict) or str(data.get("id") or "") != product_id:
        raise ProductCatalogError("商品详情响应与请求商品 ID 不一致")
    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    date_key = fetched_at[:10]
    raw_dir = PROJECT_ROOT / "data" / "raw" / "tiktok" / date_key / "seller" / "product_detail"
    raw_dir.mkdir(parents=True, exist_ok=True)
    request_id = str(payload.get("request_id") or uuid.uuid4())
    raw_path = raw_dir / f"{request_id}.json"
    digest = _write_immutable_json(raw_path, payload)
    normalized_path = (
        PROJECT_ROOT / "data" / "normalized" / "product_detail" / shop["shop_id"] / date_key / f"{product_id}.json"
    )
    normalized_path.parent.mkdir(parents=True, exist_ok=True)
    normalized_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return {
        "ok": True,
        "dataset": "product_detail",
        "shop": {"id": shop["shop_id"], "name": shop["shop_name"]},
        "fetched_at": fetched_at,
        "product": data,
        "raw_source_path": str(raw_path),
        "raw_source_sha256": digest,
        "normalized_source_path": str(normalized_path),
        "required_scope": [REQUIRED_SCOPE],
        "granted_scope": scopes,
        "errors": [],
    }


def _request_page(
    client: httpx.Client,
    *,
    settings: Settings,
    url: str,
    shop_cipher: str,
    page_size: int,
    page_token: str,
    body: dict[str, Any],
) -> dict[str, Any]:
    refreshed = False
    for attempt in range(1, 5):
        query: dict[str, str | int] = {
            "app_key": settings.tts_app_key,
            "shop_cipher": shop_cipher,
            "page_size": page_size,
            "timestamp": int(time.time()),
        }
        if page_token:
            query["page_token"] = page_token
        query["sign"] = generate_signature(
            url=url,
            query=query,
            body=body,
            app_secret=settings.tts_app_secret,
        )
        headers = {
            "content-type": "application/json",
            "x-tts-access-token": keyring.get_password(
                keyring_service(settings), "access_token"
            ) or "",
        }
        try:
            response = client.post(url, params=query, headers=headers, json=body)
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            if attempt == 4:
                raise ProductCatalogError(f"TikTok API 网络/超时错误：{redact_text(exc)}") from exc
            time.sleep(min(2 ** (attempt - 1), 4))
            continue
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if response.status_code == 401 and not refreshed:
            refresh_token(settings)
            refreshed = True
            continue
        if response.status_code in RETRYABLE_STATUS_CODES and attempt < 4:
            time.sleep(min(2 ** (attempt - 1), 4))
            continue
        if response.status_code >= 400:
            code = payload.get("code", "unknown") if isinstance(payload, dict) else "unknown"
            message = payload.get("message", "unknown response") if isinstance(payload, dict) else "unknown response"
            raise ProductCatalogError(
                f"TikTok API HTTP {response.status_code} / 错误 {code}: {redact_text(message)}"
            )
        if not isinstance(payload, dict) or payload.get("code") != 0:
            code = payload.get("code", "unknown") if isinstance(payload, dict) else "unknown"
            message = payload.get("message", "unknown response") if isinstance(payload, dict) else "unknown response"
            raise ProductCatalogError(f"TikTok API 错误 {code}: {redact_text(message)}")
        return payload
    raise ProductCatalogError("TikTok 商品目录请求重试耗尽")
