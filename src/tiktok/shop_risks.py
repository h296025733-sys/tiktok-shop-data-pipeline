"""Read-only, shop-isolated evidence for the daily operational risk reminder.

No financial/creator-health inference. Order payloads are reduced to an explicit
non-PII allowlist before leaving memory. Partial sources never mean zero issues.
"""
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
from src.tiktok.products import fetch_product_catalog
from src.tiktok.order_attribution import _post_pages, ORDER_SEARCH_PATH
from src.tiktok.safety import EndpointPolicy
from src.tiktok.signing import generate_signature
from src.tiktok.video_performance import _latest_shop_context, _write_immutable_json, granted_scopes

ORDER_FIELDS = (
    "id", "status", "fulfillment_type", "shipping_type", "create_time", "update_time",
    "rts_sla_time", "tts_sla_time", "shipping_due_time", "collection_due_time",
    "cancel_order_sla_time", "rts_time", "collection_time",
)


def _sps(settings: Settings, shop: dict, name: str) -> dict:
    url = settings.effective_tts_base_url.rstrip("/") + f"/analytics/202606/shop_performances/{name}"
    EndpointPolicy.from_project_config(settings).assert_allowed(
        capability=f"sps_{name}_v202606", method="GET", url=url, mutating=False)
    refreshed = False
    with httpx.Client(timeout=25) as client:
        for attempt in range(3):
            query = {"app_key": settings.tts_app_key, "timestamp": int(time.time()),
                     "shop_cipher": shop["shop_cipher"], "locale": "en-US"}
            query["sign"] = generate_signature(url=url, query=query, app_secret=settings.tts_app_secret)
            response = client.get(url, params=query, headers={"content-type": "application/json",
                "x-tts-access-token": keyring.get_password(keyring_service(settings), "access_token") or ""})
            payload = response.json()
            if response.status_code == 401 and payload.get("code") != 105005 and not refreshed:
                refresh_token(settings)
                refreshed = True
                continue
            if response.status_code in (429, 500, 502, 503, 504) and attempt < 2:
                time.sleep(2 ** attempt)
                continue
            if response.status_code >= 400 or payload.get("code") != 0:
                raise RuntimeError(f"HTTP {response.status_code}/code {payload.get('code')}: "
                    f"{redact_text(str(payload.get('message')))}; request_id={payload.get('request_id')}")
            if not isinstance(payload.get("data"), dict):
                raise ValueError("SPS data missing")
            return payload
    raise RuntimeError("SPS request failed")


def collect_shop_risks(settings: Settings) -> dict[str, Any]:
    shop = _latest_shop_context(settings)
    scopes = granted_scopes(settings)
    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    sources: dict[str, dict] = {}

    def collect(name, scope, load):
        try:
            if scope not in scopes:
                raise ValueError(f"Missing scope: {scope}")
            sources[name] = {"ok": True, **load()}
        except Exception as exc:
            sources[name] = {"ok": False, "error": redact_text(str(exc))[:1200]}

    def products():
        result = fetch_product_catalog(settings)
        if result.get("pagination_truncated") or result.get("conflicting_duplicate_ids"):
            raise ValueError("Product pagination incomplete or conflicting IDs")
        if any(not isinstance(p, dict) or not p.get("id") or not p.get("status") for p in result["rows"]):
            raise ValueError("Product identity/status missing")
        return {"rows": result["rows"], "raw_source_paths": result["raw_source_paths"]}

    def orders():
        rows = {}
        requests = []
        for status in ("AWAITING_SHIPMENT", "PARTIALLY_SHIPPING", "AWAITING_COLLECTION"):
            pages = _post_pages(settings, capability="search_orders_v202309", path=ORDER_SEARCH_PATH,
                body={"order_status": status}, page_size=100, max_pages=30)
            for page in pages:
                requests.append(page.get("request_id"))
                data = page.get("data") or {}
                items = data.get("orders", [])
                if not isinstance(items, list) or (not items and int(data.get("total_count", 0)) > 0):
                    raise ValueError("Order page incomplete")
                for order in items:
                    if not order.get("id") or not order.get("status"):
                        raise ValueError("Order identity/status missing")
                    safe = {key: order[key] for key in ORDER_FIELDS if key in order}
                    # A moving order can appear under two statuses; retain latest update.
                    prev = rows.get(str(order["id"]))
                    if prev is None or int(safe.get("update_time", 0)) >= int(prev.get("update_time", 0)):
                        rows[str(order["id"])] = safe
        return {"rows": list(rows.values()), "request_ids": requests}

    collect("products", "seller.product.basic", products)
    collect("orders", "seller.order.info", orders)
    for name in ("overview", "metrics"):
        collect(f"sps_{name}", "data.shop_analytics.public.read", lambda name=name: {"response": _sps(settings, shop, name)})
    result = {"version": 1, "shop": {"id": shop["shop_id"], "name": shop["shop_name"]},
              "fetchedAt": fetched_at, "sources": sources}
    target = PROJECT_ROOT / "data" / "normalized" / "shop_risks" / shop["shop_id"] / fetched_at[:10] / f"{uuid.uuid4()}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    digest = _write_immutable_json(target, result)
    return {**result, "evidencePath": str(target), "evidenceSha256": digest}


if __name__ == "__main__":
    import sys
    from src.config import get_settings
    from src.logging_config import configure_logging
    sys.stdout.reconfigure(encoding="utf-8")
    configure_logging("WARNING")
    try:
        print(json.dumps(collect_shop_risks(get_settings()), ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({"error": redact_text(str(exc))}, ensure_ascii=False))
        raise SystemExit(2)
