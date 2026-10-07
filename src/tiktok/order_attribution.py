from __future__ import annotations

import hashlib
import json
import time
import uuid
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import httpx
import keyring

from src.config import PROJECT_ROOT, Settings
from src.logging_config import redact_text
from src.tiktok.analytics import fetch_analytics
from src.tiktok.oauth import keyring_service, refresh_token
from src.tiktok.safety import EndpointPolicy
from src.tiktok.signing import generate_signature
from src.tiktok.video_performance import _latest_shop_context, granted_scopes


ORDER_SEARCH_PATH = "/order/202309/orders/search"
AFFILIATE_ORDER_SEARCH_PATH = "/affiliate_seller/202410/orders/search"
ORDER_SCOPE = "seller.order.info"
AFFILIATE_SCOPE = "seller.affiliate_collaboration.read"
ANALYTICS_SCOPE = "data.shop_analytics.public.read"
REQUIRED_SCOPES = (ORDER_SCOPE, ANALYTICS_SCOPE)
CLASSIFICATION_POLICY = "seller_affiliate_order_tags_exact_v1"
LEGACY_CLASSIFICATION_POLICY = "affiliate_non_live_as_video"
PAID_SNAPSHOT_POLICY = "paid_positive_non_sample_local_day_exact_v2"
VIDEO_ORDER_SNAPSHOT_POLICY = "affiliate_content_id_exact_v1"
ELIGIBLE_ORDER_STATUSES = {
    "AWAITING_SHIPMENT",
    "AWAITING_COLLECTION",
    "PARTIALLY_SHIPPING",
    "IN_TRANSIT",
    "DELIVERED",
    "COMPLETED",
}
EXCLUDED_ORDER_STATUSES = {"UNPAID", "ON_HOLD", "CANCELLED"}
EXCLUDED_NON_SALE_ORDER_TYPES = {"ZERO_LOTTERY"}
LIVE_CONTENT_TYPES = {"LIVE", "PRE_LIVE"}
VIDEO_CONTENT_TYPES = {"VIDEO"}
PRODUCT_CARD_CONTENT_TYPES = {"SHOP"}
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


class OrderAttributionError(RuntimeError):
    pass


def _safe_str(value: Any) -> str:
    return str(value or "").strip()


def _safe_int(value: Any, *, label: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise OrderAttributionError(f"{label} 不是整数") from exc
    if parsed < 0:
        raise OrderAttributionError(f"{label} 不能为负数")
    return parsed


def _safe_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return _safe_str(value).lower() in {"1", "true", "yes"}


def _positive_money(value: Any) -> bool:
    try:
        return Decimal(str(value or "0")) > 0
    except (InvalidOperation, ValueError):
        return False


def _money_or_none(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _hash_identifier(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:20]


def _day_key(timestamp: Any, time_zone: str) -> str:
    seconds = _safe_int(timestamp, label="create_time")
    return datetime.fromtimestamp(seconds, timezone.utc).astimezone(
        ZoneInfo(time_zone)
    ).date().isoformat()


def _relationship_bucket(sku: dict[str, Any]) -> str:
    """Official target/open collaboration IDs are retained as evidence.

    A target collaboration is the only API-level signal that can plausibly map to
    Seller Center's merchant-contracted relationship.  The caller must explicitly
    opt into that business mapping after comparing a real order with Seller Center.
    """
    if _safe_str(sku.get("target_collaboration_id")):
        return "target"
    if _safe_str(sku.get("open_collaboration_id")) or _safe_str(sku.get("campaign_id")):
        return "open"
    return "unknown"


def _content_bucket(content_type: Any) -> str:
    normalized = _safe_str(content_type).upper()
    if normalized in VIDEO_CONTENT_TYPES:
        return "video"
    if normalized in LIVE_CONTENT_TYPES:
        return "live"
    if normalized in PRODUCT_CARD_CONTENT_TYPES:
        return "product_card"
    return "unknown"


def _effective_order_lines(
    orders: Iterable[Any], *, time_zone: str
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    lines: list[dict[str, Any]] = []
    audit_counts: dict[str, int] = defaultdict(int)
    eligible_status_counts: dict[str, int] = defaultdict(int)
    unknown_order_statuses: set[str] = set()
    unknown_line_statuses: set[str] = set()
    for raw_order in orders:
        if not isinstance(raw_order, dict):
            continue
        status = _safe_str(raw_order.get("status")).upper()
        if _safe_bool(raw_order.get("is_sample_order")):
            audit_counts["excluded_sample_orders"] += 1
            continue
        if _safe_str(raw_order.get("order_type")).upper() in EXCLUDED_NON_SALE_ORDER_TYPES:
            audit_counts["excluded_non_sale_orders"] += 1
            continue
        if not _safe_str(raw_order.get("paid_time")):
            audit_counts["excluded_without_paid_time_orders"] += 1
            continue
        payment = raw_order.get("payment") or {}
        total_amount = payment.get("total_amount") if isinstance(payment, dict) else None
        if not _positive_money(total_amount):
            audit_counts["excluded_non_positive_payment_orders"] += 1
            continue
        if status in EXCLUDED_ORDER_STATUSES:
            audit_counts["excluded_status_orders"] += 1
            continue
        if status not in ELIGIBLE_ORDER_STATUSES:
            audit_counts["unknown_status_orders"] += 1
            unknown_order_statuses.add(status or "<EMPTY>")
            continue
        eligible_status_counts[status] += 1
        order_id = _safe_str(raw_order.get("id"))
        create_time = raw_order.get("create_time")
        if not order_id or create_time in (None, ""):
            audit_counts["excluded_incomplete_orders"] += 1
            continue
        # Seller analytics and the requested business definition both count a sale
        # on the payment day.  create_time is only the Order API search cursor.
        paid_time = _safe_int(raw_order.get("paid_time"), label="paid_time")
        day = _day_key(paid_time, time_zone)
        for raw_line in raw_order.get("line_items") or []:
            if not isinstance(raw_line, dict):
                continue
            if _safe_bool(raw_line.get("is_gift")):
                audit_counts["excluded_gift_lines"] += 1
                continue
            line_status = _safe_str(raw_line.get("display_status") or status).upper()
            if line_status in EXCLUDED_ORDER_STATUSES:
                audit_counts["excluded_status_lines"] += 1
                continue
            if line_status not in ELIGIBLE_ORDER_STATUSES:
                audit_counts["unknown_status_lines"] += 1
                unknown_line_statuses.add(line_status or "<EMPTY>")
                continue
            product_id = _safe_str(raw_line.get("product_id"))
            sku_id = _safe_str(raw_line.get("sku_id"))
            if not product_id:
                audit_counts["excluded_incomplete_lines"] += 1
                continue
            quantity = raw_line.get("quantity", 1)
            lines.append({
                "day": day,
                "order_id": order_id,
                "paid_time": paid_time,
                "product_id": product_id,
                "sku_id": sku_id,
                "quantity": _safe_int(quantity, label=f"订单{order_id}数量"),
                "room_id": _safe_str(raw_line.get("room_id")),
                "sale_price": _money_or_none(raw_line.get("sale_price")),
                "currency": _safe_str(raw_line.get("currency")),
            })
            audit_counts["included_lines"] += 1
    return lines, {
        **dict(sorted(audit_counts.items())),
        "eligible_order_status_counts": dict(sorted(eligible_status_counts.items())),
        "unknown_order_statuses": sorted(unknown_order_statuses),
        "unknown_line_statuses": sorted(unknown_line_statuses),
    }


def build_paid_order_snapshot(
    orders: Iterable[Any],
    *,
    requested_dates: Iterable[str],
    time_zone: str = "Asia/Shanghai",
) -> dict[str, Any]:
    """Aggregate the fastest safe paid-order totals independently of Analytics.

    The snapshot deliberately contains product/day order, item and validated sales totals. It
    excludes unpaid, non-positive, sample, gift and already-cancelled rows using
    the same strict eligibility rules as the attribution classifier.  The caller
    writes the latest eligible snapshot exactly, so a cancelled line is not
    silently preserved as a current paid sale.
    """
    dates = sorted(set(requested_dates))
    date_set = set(dates)
    effective_lines, eligibility_audit = _effective_order_lines(
        orders,
        time_zone=time_zone,
    )
    relevant_lines = [line for line in effective_lines if line["day"] in date_set]
    product_order_sets: dict[tuple[str, str], set[str]] = defaultdict(set)
    product_items: dict[tuple[str, str], int] = defaultdict(int)
    product_sales: dict[tuple[str, str], Decimal] = defaultdict(Decimal)
    product_currencies: dict[tuple[str, str], set[str]] = defaultdict(set)
    product_sales_ready: dict[tuple[str, str], bool] = defaultdict(lambda: True)
    product_paid_times: dict[tuple[str, str, str], int] = {}
    store_order_sets: dict[str, set[str]] = defaultdict(set)
    store_items: dict[str, int] = defaultdict(int)
    for line in relevant_lines:
        day = line["day"]
        product_id = line["product_id"]
        order_id = line["order_id"]
        product_order_sets[(day, product_id)].add(order_id)
        product_paid_times[(day, product_id, order_id)] = line["paid_time"]
        product_items[(day, product_id)] += line["quantity"]
        if line["sale_price"] is None:
            product_sales_ready[(day, product_id)] = False
        else:
            product_sales[(day, product_id)] += line["sale_price"] * line["quantity"]
        if line["currency"]:
            product_currencies[(day, product_id)].add(line["currency"])
        store_order_sets[day].add(order_id)
        store_items[day] += line["quantity"]

    rows = [
        {
            "date": day,
            "product_id": product_id,
            "total_orders": len(order_ids),
            "total_items": product_items[(day, product_id)],
            "sales_amount": str(product_sales[(day, product_id)]),
            "sales_currency": (
                next(iter(product_currencies[(day, product_id)]))
                if len(product_currencies[(day, product_id)]) == 1 else None
            ),
            "sales_ready": (
                product_sales_ready[(day, product_id)]
                and len(product_currencies[(day, product_id)]) == 1
            ),
            "total_order_keys": sorted(_hash_identifier(value) for value in order_ids),
            "total_order_paid_at": {
                _hash_identifier(value): product_paid_times[(day, product_id, value)]
                for value in sorted(order_ids)
            },
        }
        for (day, product_id), order_ids in sorted(product_order_sets.items())
    ]
    order_paid_times = {
        _hash_identifier(line["order_id"]): line["paid_time"]
        for line in relevant_lines
    }
    store_rows = [
        {
            "date": day,
            "total_orders": len(store_order_sets[day]),
            "total_items": store_items[day],
            "total_order_keys": sorted(
                _hash_identifier(value) for value in store_order_sets[day]
            ),
            "total_order_paid_at": {
                _hash_identifier(value): order_paid_times[_hash_identifier(value)]
                for value in sorted(store_order_sets[day])
            },
        }
        for day in dates
    ]
    return {
        "policy": PAID_SNAPSHOT_POLICY,
        "dates": dates,
        "rows": rows,
        "row_count": len(rows),
        "store_rows": store_rows,
        "store_row_count": len(store_rows),
        "effective_order_count": len({line["order_id"] for line in relevant_lines}),
        "sales_errors": [
            f"{day}.{product_id} 商品行金额缺失或币种不唯一"
            for (day, product_id) in sorted(product_order_sets)
            if not (
                product_sales_ready[(day, product_id)]
                and len(product_currencies[(day, product_id)]) == 1
            )
        ],
        "eligibility_audit": eligibility_audit,
    }


def build_paid_video_order_snapshot(
    orders: Iterable[Any],
    affiliate_orders: Iterable[Any],
    *,
    requested_dates: Iterable[str],
    time_zone: str = "Asia/Shanghai",
) -> dict[str, Any]:
    """Build privacy-safe, exact order-key sets per video when Affiliate is authorized.

    Ordinary seller orders prove whether an order is currently paid and eligible.
    Affiliate seller order SKU tags are used only to attach a proven VIDEO
    ``content_id`` to that strict order line. Missing or conflicting evidence blocks
    the whole video snapshot instead of guessing from product-level sales.
    """
    dates = sorted(set(requested_dates))
    date_set = set(dates)
    effective_lines, _eligibility_audit = _effective_order_lines(
        orders,
        time_zone=time_zone,
    )
    relevant_lines = [line for line in effective_lines if line["day"] in date_set]
    relevant_order_ids = {line["order_id"] for line in relevant_lines}
    errors: list[str] = []

    affiliate_by_exact_key: dict[tuple[str, str], dict[str, Any]] = {}
    affiliate_by_product_key: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for raw_order in affiliate_orders:
        if not isinstance(raw_order, dict):
            continue
        order_id = _safe_str(raw_order.get("id"))
        if not order_id or order_id not in relevant_order_ids:
            continue
        for raw_sku in raw_order.get("skus") or []:
            if not isinstance(raw_sku, dict):
                continue
            sku_id = _safe_str(raw_sku.get("id"))
            product_id = _safe_str(raw_sku.get("product_id"))
            evidence = {
                "order_id": order_id,
                "sku_id": sku_id,
                "product_id": product_id,
                "quantity": _safe_int(
                    raw_sku.get("quantity", 1),
                    label="联盟订单SKU数量",
                ),
                "content": _content_bucket(raw_sku.get("content_type")),
                "content_id": _safe_str(raw_sku.get("content_id")),
            }
            if sku_id:
                key = (order_id, sku_id)
                existing = affiliate_by_exact_key.get(key)
                if existing and existing != evidence:
                    errors.append(
                        f"订单{_hash_identifier(order_id)}的SKU视频归因响应冲突"
                    )
                affiliate_by_exact_key[key] = evidence
            if product_id:
                affiliate_by_product_key[(order_id, product_id)].append(evidence)

    video_order_sets: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    video_items: dict[tuple[str, str, str], int] = defaultdict(int)
    video_paid_times: dict[tuple[str, str, str, str], int] = {}
    matched_quantities: dict[tuple[str, str], int] = defaultdict(int)
    matched_evidence: dict[tuple[str, str], dict[str, Any]] = {}
    for line in relevant_lines:
        order_id = line["order_id"]
        product_id = line["product_id"]
        sku_id = line["sku_id"]
        evidence = affiliate_by_exact_key.get((order_id, sku_id)) if sku_id else None
        if evidence is None:
            candidates = affiliate_by_product_key.get((order_id, product_id), [])
            if len(candidates) == 1:
                evidence = candidates[0]
            elif len(candidates) > 1:
                errors.append(
                    f"订单{_hash_identifier(order_id)}商品{product_id}存在多个联盟SKU，无法精确归到视频"
                )
                continue
        # An eligible order absent from the complete Affiliate response is a
        # product-card order and deliberately has no video attribution.
        if evidence is None:
            continue
        evidence_sku = str(evidence.get("sku_id") or sku_id)
        evidence_key = (order_id, evidence_sku)
        matched_quantities[evidence_key] += line["quantity"]
        matched_evidence[evidence_key] = evidence
        content = str(evidence["content"])
        if content != "video":
            if content == "unknown":
                errors.append(
                    f"订单{_hash_identifier(order_id)}存在未知内容类型，视频归因已停用"
                )
            continue
        video_id = str(evidence["content_id"])
        if not video_id.isdigit() or len(video_id) < 10:
            errors.append(
                f"订单{_hash_identifier(order_id)}的视频标签缺少有效content_id"
            )
            continue
        key = (line["day"], product_id, video_id)
        video_order_sets[key].add(order_id)
        video_items[key] += line["quantity"]
        video_paid_times[(*key, order_id)] = line["paid_time"]

    for evidence_key, evidence in matched_evidence.items():
        matched = matched_quantities[evidence_key]
        if matched != int(evidence["quantity"]):
            errors.append(
                f"订单{_hash_identifier(evidence_key[0])}联盟SKU数量"
                f"{evidence['quantity']}与严格订单数量{matched}不一致"
            )

    rows = [
        {
            "date": day,
            "product_id": product_id,
            "video_id": video_id,
            "total_orders": len(order_ids),
            "total_items": video_items[(day, product_id, video_id)],
            "total_order_keys": sorted(
                _hash_identifier(value) for value in order_ids
            ),
            "total_order_paid_at": {
                _hash_identifier(value): video_paid_times[(day, product_id, video_id, value)]
                for value in sorted(order_ids)
            },
        }
        for (day, product_id, video_id), order_ids in sorted(video_order_sets.items())
    ]
    unique_errors = sorted(set(errors))
    return {
        "policy": VIDEO_ORDER_SNAPSHOT_POLICY,
        "ready": not unique_errors,
        "rows": rows if not unique_errors else [],
        "row_count": len(rows) if not unique_errors else 0,
        "errors": unique_errors,
    }


def classify_order_tags_exact(
    orders: Iterable[Any],
    affiliate_orders: Iterable[Any],
    *,
    complete_dates: Iterable[str],
    time_zone: str = "Asia/Shanghai",
    target_collaboration_is_self_operated: bool,
) -> dict[str, Any]:
    """Classify strict paid order lines using the official affiliate-order tags.

    Ordinary Order API is authoritative for paid time, cancellation/sample/gift
    exclusion and the complete order universe. Search Seller Affiliate Orders is
    used only for its collaboration and content tags. A strict order line absent
    from the complete affiliate response is the deterministic residual product-
    card bucket. Unknown content types, uncalibrated target collaboration or any
    quantity mismatch block writes instead of being guessed.
    """
    complete = set(complete_dates)
    effective_lines, eligibility_audit = _effective_order_lines(orders, time_zone=time_zone)
    relevant_lines = [line for line in effective_lines if line["day"] in complete]
    reconciliation_errors: list[str] = []
    evidence_counts: dict[str, int] = defaultdict(int)

    affiliate_by_exact_key: dict[tuple[str, str], dict[str, Any]] = {}
    affiliate_by_product_key: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for raw_order in affiliate_orders:
        if not isinstance(raw_order, dict):
            continue
        order_id = _safe_str(raw_order.get("id"))
        if not order_id:
            reconciliation_errors.append("联盟订单响应存在缺少订单ID的记录")
            continue
        for raw_sku in raw_order.get("skus") or []:
            if not isinstance(raw_sku, dict):
                continue
            sku_id = _safe_str(raw_sku.get("id"))
            product_id = _safe_str(raw_sku.get("product_id"))
            quantity = _safe_int(raw_sku.get("quantity", 1), label="联盟订单SKU数量")
            content = _content_bucket(raw_sku.get("content_type"))
            relationship = _relationship_bucket(raw_sku)
            if content == "product_card":
                metric = "product_card"
            elif content not in {"video", "live"}:
                metric = "unclassified"
                reconciliation_errors.append(
                    f"订单{_hash_identifier(order_id)}存在未知内容类型"
                )
            elif relationship == "target":
                if target_collaboration_is_self_operated:
                    metric = f"self_operated_{content}"
                else:
                    metric = "unclassified"
                    reconciliation_errors.append(
                        f"订单{_hash_identifier(order_id)}的target合作尚未校准为自营达人"
                    )
            elif relationship == "open":
                metric = f"alliance_{content}"
            else:
                metric = "unclassified"
                reconciliation_errors.append(
                    f"订单{_hash_identifier(order_id)}缺少可识别的达人关系标签"
                )
            evidence = {
                "order_id": order_id,
                "sku_id": sku_id,
                "product_id": product_id,
                "quantity": quantity,
                "metric": metric,
            }
            if sku_id:
                key = (order_id, sku_id)
                existing = affiliate_by_exact_key.get(key)
                if existing and existing != evidence:
                    reconciliation_errors.append(
                        f"订单{_hash_identifier(order_id)}的SKU标签响应冲突"
                    )
                affiliate_by_exact_key[key] = evidence
            if product_id:
                affiliate_by_product_key[(order_id, product_id)].append(evidence)

    metric_names = (
        "alliance_video",
        "alliance_live",
        "self_operated_video",
        "self_operated_live",
        "product_card",
        "unclassified",
    )
    item_counts: dict[tuple[str, str, str], int] = defaultdict(int)
    order_sets: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    total_items: dict[tuple[str, str], int] = defaultdict(int)
    total_order_sets: dict[tuple[str, str], set[str]] = defaultdict(set)
    matched_affiliate_quantities: dict[tuple[str, str], int] = defaultdict(int)
    seen_affiliate_keys: set[tuple[str, str]] = set()
    per_order_product_metrics: dict[tuple[str, str, str], set[str]] = defaultdict(set)

    for line in relevant_lines:
        day = line["day"]
        order_id = line["order_id"]
        product_id = line["product_id"]
        sku_id = line["sku_id"]
        quantity = line["quantity"]
        evidence = affiliate_by_exact_key.get((order_id, sku_id)) if sku_id else None
        if evidence is None:
            candidates = affiliate_by_product_key.get((order_id, product_id), [])
            if len(candidates) == 1:
                evidence = candidates[0]
            elif len(candidates) > 1:
                reconciliation_errors.append(
                    f"订单{_hash_identifier(order_id)}商品{product_id}存在多个联盟SKU且无法按SKU匹配"
                )
        metric = str(evidence["metric"]) if evidence else "product_card"
        if evidence:
            evidence_sku = str(evidence.get("sku_id") or sku_id)
            seen_affiliate_keys.add((order_id, evidence_sku))
            matched_affiliate_quantities[(order_id, evidence_sku)] += quantity
            evidence_counts[f"tag:{metric}"] += 1
        else:
            evidence_counts["residual:product_card"] += 1
        item_counts[(day, product_id, metric)] += quantity
        order_sets[(day, product_id, metric)].add(order_id)
        total_items[(day, product_id)] += quantity
        total_order_sets[(day, product_id)].add(order_id)
        per_order_product_metrics[(day, order_id, product_id)].add(metric)

    relevant_order_ids = {line["order_id"] for line in relevant_lines}
    for (order_id, sku_id), evidence in affiliate_by_exact_key.items():
        if order_id not in relevant_order_ids:
            continue
        matched = matched_affiliate_quantities[(order_id, sku_id)]
        if matched != int(evidence["quantity"]):
            reconciliation_errors.append(
                f"订单{_hash_identifier(order_id)}联盟SKU数量{evidence['quantity']}与严格订单数量{matched}不一致"
            )
    for (day, order_id, product_id), metrics in per_order_product_metrics.items():
        if len(metrics) > 1:
            reconciliation_errors.append(
                f"{day}订单{_hash_identifier(order_id)}商品{product_id}同时命中多个渠道"
            )

    product_keys = sorted(total_order_sets)
    rows: list[dict[str, Any]] = []
    for day, product_id in product_keys:
        row: dict[str, Any] = {"date": day, "product_id": product_id}
        for metric in metric_names:
            keys = order_sets[(day, product_id, metric)]
            row[f"{metric}_orders"] = len(keys)
            row[f"{metric}_items"] = item_counts[(day, product_id, metric)]
            row[f"{metric}_order_keys"] = sorted(_hash_identifier(value) for value in keys)
        total_keys = total_order_sets[(day, product_id)]
        row["total_orders"] = len(total_keys)
        row["total_items"] = total_items[(day, product_id)]
        row["total_order_keys"] = sorted(_hash_identifier(value) for value in total_keys)
        channel_items = sum(row[f"{metric}_items"] for metric in metric_names)
        row["reconciled"] = channel_items == row["total_items"] and row["unclassified_items"] == 0
        if not row["reconciled"]:
            reconciliation_errors.append(f"{day}商品{product_id}渠道件数未与严格订单闭合")
        rows.append(row)

    store_rows: list[dict[str, Any]] = []
    for day in sorted(complete):
        day_lines = [line for line in relevant_lines if line["day"] == day]
        store_row: dict[str, Any] = {"date": day}
        for metric in metric_names:
            day_order_keys = {
                line["order_id"]
                for line in day_lines
                if line["order_id"] in {
                    order_id
                    for candidate_day, _product_id, candidate_metric in order_sets
                    if candidate_day == day and candidate_metric == metric
                    for order_id in order_sets[(candidate_day, _product_id, candidate_metric)]
                }
            }
            store_row[f"{metric}_orders"] = len(day_order_keys)
            store_row[f"{metric}_items"] = sum(
                item_counts[(candidate_day, product_id, candidate_metric)]
                for candidate_day, product_id, candidate_metric in item_counts
                if candidate_day == day and candidate_metric == metric
            )
        store_row["total_orders"] = len({line["order_id"] for line in day_lines})
        store_row["total_items"] = sum(line["quantity"] for line in day_lines)
        store_row["reconciled"] = (
            sum(store_row[f"{metric}_items"] for metric in metric_names)
            == store_row["total_items"]
            and store_row["unclassified_items"] == 0
        )
        store_rows.append(store_row)

    eligibility_errors: list[str] = []
    if eligibility_audit["unknown_order_statuses"]:
        eligibility_errors.append(
            "发现未知订单状态：" + "、".join(eligibility_audit["unknown_order_statuses"])
        )
    if eligibility_audit["unknown_line_statuses"]:
        eligibility_errors.append(
            "发现未知商品行状态：" + "、".join(eligibility_audit["unknown_line_statuses"])
        )
    unclassified_items = sum(row["unclassified_items"] for row in rows)
    return {
        "rows": rows,
        "row_count": len(rows),
        "store_rows": store_rows,
        "store_row_count": len(store_rows),
        "effective_order_count": len({line["order_id"] for line in relevant_lines}),
        "eligible_statuses": sorted(ELIGIBLE_ORDER_STATUSES),
        "excluded_statuses": sorted(EXCLUDED_ORDER_STATUSES),
        "eligibility_audit": eligibility_audit,
        "eligibility_errors": eligibility_errors,
        "business_time_zone": time_zone,
        "classification_policy": CLASSIFICATION_POLICY,
        "approximation_acknowledged": False,
        "classification_evidence": dict(sorted(evidence_counts.items())),
        "unclassified_items": unclassified_items,
        "reconciliation_errors": sorted(set(reconciliation_errors)),
        "reconciliation_warnings": [],
        "write_ready": (
            unclassified_items == 0
            and not reconciliation_errors
            and not eligibility_errors
        ),
    }


def _metric_value(row: dict[str, Any], block: str, key: str, *, label: str) -> int:
    value = row.get(block) or {}
    if not isinstance(value, dict):
        raise OrderAttributionError(f"{label}.{block} 不是对象")
    return _safe_int(value.get(key, 0), label=f"{label}.{block}.{key}")


def classify_order_attribution(
    orders: Iterable[Any],
    product_rows_by_date: dict[str, list[dict[str, Any]]],
    *,
    live_session_relationships: dict[str, str],
    complete_dates: Iterable[str],
    time_zone: str = "Asia/Shanghai",
    live_session_conflicts: Iterable[str] = (),
) -> dict[str, Any]:
    """Build the approved hybrid attribution contract without Affiliate Seller API.

    Product Analytics supplies exact seller-video, product-card and affiliate-total
    metrics.  Strict paid orders plus live-session account types supply the live
    relationship split.  Per the merchant-approved approximation, every affiliate
    non-live order is reported as affiliate video.
    """
    complete = set(complete_dates)
    effective_lines, eligibility_audit = _effective_order_lines(orders, time_zone=time_zone)
    relevant_lines = [line for line in effective_lines if line["day"] in complete]
    reconciliation_errors: list[str] = []
    reconciliation_warnings: list[str] = []
    evidence_counts: dict[str, int] = defaultdict(int)
    unclassified_items = 0

    for room_hash in live_session_conflicts:
        reconciliation_errors.append(f"直播场次{room_hash}同时出现在多个账号类型")

    live_item_counts: dict[tuple[str, str, str], int] = defaultdict(int)
    live_order_sets: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    products_by_order: dict[tuple[str, str], set[str]] = defaultdict(set)
    raw_product_order_sets: dict[tuple[str, str], set[str]] = defaultdict(set)
    raw_product_items: dict[tuple[str, str], int] = defaultdict(int)
    raw_store_order_sets: dict[str, set[str]] = defaultdict(set)
    raw_store_items: dict[str, int] = defaultdict(int)

    for line in relevant_lines:
        day = line["day"]
        product_id = line["product_id"]
        order_id = line["order_id"]
        quantity = line["quantity"]
        products_by_order[(day, order_id)].add(product_id)
        raw_product_order_sets[(day, product_id)].add(order_id)
        raw_product_items[(day, product_id)] += quantity
        raw_store_order_sets[day].add(order_id)
        raw_store_items[day] += quantity
        room_id = line["room_id"]
        if not room_id:
            continue
        relationship = live_session_relationships.get(room_id)
        if relationship not in {"alliance", "self_operated"}:
            unclassified_items += quantity
            reconciliation_errors.append(
                f"{day}商品{product_id}的直播场次{_hash_identifier(room_id)}未匹配账号类型"
            )
            continue
        metric = f"{relationship}_live"
        live_item_counts[(day, product_id, metric)] += quantity
        live_order_sets[(day, product_id, metric)].add(order_id)
        evidence_counts[f"live_relationship:{relationship}"] += 1

    for (day, order_id), product_ids in products_by_order.items():
        if len(product_ids) > 1:
            reconciliation_errors.append(
                f"{day}订单{_hash_identifier(order_id)}包含多个商品，店铺渠道订单数无法安全去重"
            )

    metric_names = (
        "alliance_video", "alliance_live", "self_operated_video",
        "self_operated_live", "product_card", "unclassified",
    )
    rows: list[dict[str, Any]] = []
    for day in sorted(complete):
        analytics_rows = product_rows_by_date.get(day, [])
        analytics_ids = {
            _safe_str(row.get("id")) for row in analytics_rows if isinstance(row, dict)
        }
        raw_live_product_ids = {
            product_id for candidate_day, product_id, _metric in live_item_counts
            if candidate_day == day
        }
        for missing_id in sorted(raw_live_product_ids - analytics_ids):
            reconciliation_errors.append(f"{day}直播商品{missing_id}未出现在商品Analytics中")

        for analytics_row in analytics_rows:
            if not isinstance(analytics_row, dict):
                continue
            product_id = _safe_str(analytics_row.get("id"))
            if not product_id:
                reconciliation_errors.append(f"{day}商品Analytics存在缺少商品ID的记录")
                continue
            label = f"{day}.{product_id}"
            total_orders = _metric_value(
                analytics_row, "total_performance", "orders", label=label
            )
            total_items = _metric_value(
                analytics_row, "total_performance", "items_sold", label=label
            )
            seller_live_orders = _metric_value(
                analytics_row, "seller_live_performance", "attributed_orders", label=label
            )
            seller_live_items = _metric_value(
                analytics_row, "seller_live_performance", "attributed_sold_items", label=label
            )
            self_video_orders = _metric_value(
                analytics_row, "seller_video_performance", "attributed_orders", label=label
            )
            self_video_items = _metric_value(
                analytics_row, "seller_video_performance", "attributed_sold_items", label=label
            )
            product_card_orders = _metric_value(
                analytics_row, "seller_product_card_performance", "attributed_orders", label=label
            )
            product_card_items = _metric_value(
                analytics_row, "seller_product_card_performance", "attributed_sold_items", label=label
            )
            affiliate_total_orders = _metric_value(
                analytics_row, "affiliate_total_performance", "attributed_orders", label=label
            )
            affiliate_total_items = _metric_value(
                analytics_row, "affiliate_total_performance", "attributed_sold_items", label=label
            )
            alliance_live_orders = len(live_order_sets[(day, product_id, "alliance_live")])
            alliance_live_items = live_item_counts[(day, product_id, "alliance_live")]
            observed_self_live_orders = len(
                live_order_sets[(day, product_id, "self_operated_live")]
            )
            observed_self_live_items = live_item_counts[
                (day, product_id, "self_operated_live")
            ]
            # Product Analytics is the calibrated source of seller-operated LIVE
            # attribution.  room_id remains relationship evidence, but it does not
            # include every 24-hour-attributed LIVE order shown by Seller Center.
            self_live_orders = seller_live_orders
            self_live_items = seller_live_items

            if (observed_self_live_orders, observed_self_live_items) != (
                seller_live_orders, seller_live_items
            ):
                reconciliation_warnings.append(
                    f"{label}普通订单room_id观察到自营直播"
                    f"{observed_self_live_orders}/{observed_self_live_items}，"
                    f"采用商品Analytics校准值{seller_live_orders}/{seller_live_items}"
                )
            if alliance_live_orders > affiliate_total_orders or alliance_live_items > affiliate_total_items:
                reconciliation_errors.append(
                    f"{label}联盟直播{alliance_live_orders}/{alliance_live_items}超过"
                    f"联盟总量{affiliate_total_orders}/{affiliate_total_items}"
                )
            alliance_video_orders = max(0, affiliate_total_orders - alliance_live_orders)
            alliance_video_items = max(0, affiliate_total_items - alliance_live_items)
            channel_orders = (
                alliance_video_orders + alliance_live_orders + self_video_orders
                + self_live_orders + product_card_orders
            )
            channel_items = (
                alliance_video_items + alliance_live_items + self_video_items
                + self_live_items + product_card_items
            )
            reconciled = channel_orders == total_orders and channel_items == total_items
            if not reconciled:
                reconciliation_errors.append(
                    f"{label}渠道订单/件数{channel_orders}/{channel_items}与"
                    f"商品总量{total_orders}/{total_items}不相等"
                )

            raw_orders = len(raw_product_order_sets[(day, product_id)])
            raw_items = raw_product_items[(day, product_id)]
            if (raw_orders, raw_items) != (total_orders, total_items):
                reconciliation_warnings.append(
                    f"{label}严格订单{raw_orders}/{raw_items}与Analytics总量"
                    f"{total_orders}/{total_items}存在口径差异"
                )
            row = {
                "date": day,
                "product_id": product_id,
                "alliance_video_orders": alliance_video_orders,
                "alliance_video_items": alliance_video_items,
                "alliance_video_order_keys": [],
                "alliance_live_orders": alliance_live_orders,
                "alliance_live_items": alliance_live_items,
                "alliance_live_order_keys": sorted(
                    _hash_identifier(value)
                    for value in live_order_sets[(day, product_id, "alliance_live")]
                ),
                "self_operated_video_orders": self_video_orders,
                "self_operated_video_items": self_video_items,
                "self_operated_video_order_keys": [],
                "self_operated_live_orders": self_live_orders,
                "self_operated_live_items": self_live_items,
                "self_operated_live_order_keys": sorted(
                    _hash_identifier(value)
                    for value in live_order_sets[(day, product_id, "self_operated_live")]
                ),
                "product_card_orders": product_card_orders,
                "product_card_items": product_card_items,
                "product_card_order_keys": [],
                "unclassified_orders": 0,
                "unclassified_items": 0,
                "unclassified_order_keys": [],
                "total_orders": total_orders,
                "total_items": total_items,
                "total_order_keys": sorted(
                    _hash_identifier(value)
                    for value in raw_product_order_sets[(day, product_id)]
                ),
                "reconciled": reconciled,
            }
            rows.append(row)

    store_rows: list[dict[str, Any]] = []
    for day in sorted(complete):
        day_rows = [row for row in rows if row["date"] == day]
        store_row: dict[str, Any] = {"date": day}
        for metric in metric_names:
            store_row[f"{metric}_orders"] = sum(row[f"{metric}_orders"] for row in day_rows)
            store_row[f"{metric}_items"] = sum(row[f"{metric}_items"] for row in day_rows)
        store_row["total_orders"] = sum(row["total_orders"] for row in day_rows)
        store_row["total_items"] = sum(row["total_items"] for row in day_rows)
        store_row["strict_order_count"] = len(raw_store_order_sets[day])
        store_row["strict_order_items"] = raw_store_items[day]
        store_row["reconciled"] = all(row["reconciled"] for row in day_rows)
        store_rows.append(store_row)

    eligibility_errors: list[str] = []
    if eligibility_audit["unknown_order_statuses"]:
        eligibility_errors.append(
            "发现未知订单状态：" + "、".join(eligibility_audit["unknown_order_statuses"])
        )
    if eligibility_audit["unknown_line_statuses"]:
        eligibility_errors.append(
            "发现未知商品行状态：" + "、".join(eligibility_audit["unknown_line_statuses"])
        )
    return {
        "rows": rows,
        "row_count": len(rows),
        "store_rows": store_rows,
        "store_row_count": len(store_rows),
        "effective_order_count": len({line["order_id"] for line in relevant_lines}),
        "eligible_statuses": sorted(ELIGIBLE_ORDER_STATUSES),
        "excluded_statuses": sorted(EXCLUDED_ORDER_STATUSES),
        "eligibility_audit": eligibility_audit,
        "eligibility_errors": eligibility_errors,
        "business_time_zone": time_zone,
        "classification_policy": LEGACY_CLASSIFICATION_POLICY,
        "approximation_acknowledged": True,
        "classification_evidence": dict(sorted(evidence_counts.items())),
        "unclassified_items": unclassified_items,
        "reconciliation_errors": sorted(set(reconciliation_errors)),
        "reconciliation_warnings": sorted(set(reconciliation_warnings)),
        "write_ready": (
            unclassified_items == 0
            and not reconciliation_errors
            and not eligibility_errors
        ),
    }


def _post_pages(
    settings: Settings,
    *,
    capability: str,
    path: str,
    body: dict[str, Any],
    page_size: int,
    max_pages: int,
) -> list[dict[str, Any]]:
    shop = _latest_shop_context(settings)
    url = f"{settings.effective_tts_base_url.rstrip('/')}{path}"
    EndpointPolicy.from_project_config(settings).assert_allowed(
        capability=capability, method="POST", url=url, mutating=False
    )
    payloads: list[dict[str, Any]] = []
    page_token = ""
    refreshed = False
    with httpx.Client(timeout=30) as client:
        for attempt_page in range(max_pages):
            for attempt in range(1, 5):
                query: dict[str, str | int] = {
                    "app_key": settings.tts_app_key,
                    "shop_cipher": shop["shop_cipher"],
                    "timestamp": int(time.time()),
                    "page_size": page_size,
                }
                if page_token:
                    query["page_token"] = page_token
                if path == ORDER_SEARCH_PATH:
                    query.update(sort_field="create_time", sort_order="ASC")
                query["sign"] = generate_signature(
                    url=url, query=query, app_secret=settings.tts_app_secret, body=body
                )
                response = client.post(
                    url,
                    params=query,
                    headers={
                        "content-type": "application/json",
                        "x-tts-access-token": keyring.get_password(
                            keyring_service(settings), "access_token"
                        ) or "",
                    },
                    json=body,
                )
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
                if response.status_code >= 400 or payload.get("code") != 0:
                    raise OrderAttributionError(
                        f"TikTok API HTTP {response.status_code} / 错误 "
                        f"{payload.get('code', 'unknown')}: "
                        f"{redact_text(payload.get('message', 'unknown response'))}"
                    )
                break
            payloads.append(payload)
            data = payload.get("data") or {}
            page_token = _safe_str(data.get("next_page_token")) if isinstance(data, dict) else ""
            if not page_token:
                return payloads
        if page_token:
            raise OrderAttributionError(f"{capability} 分页达到安全上限，拒绝使用不完整结果")
    return payloads


def _timestamp_range(start_date: str, end_date_exclusive: str, time_zone: str) -> tuple[int, int]:
    try:
        start = datetime.fromisoformat(start_date).replace(tzinfo=ZoneInfo(time_zone))
        end = datetime.fromisoformat(end_date_exclusive).replace(tzinfo=ZoneInfo(time_zone))
    except (ValueError, KeyError) as exc:
        raise OrderAttributionError("日期必须是有效的 YYYY-MM-DD，时区必须是 IANA 名称") from exc
    if start >= end:
        raise OrderAttributionError("结束日期必须晚于开始日期")
    if (end - start).days > 31:
        raise OrderAttributionError("订单归因单次最多查询31天")
    return int(start.timestamp()), int(end.timestamp())


def _safe_snapshot(payloads: Iterable[dict[str, Any]], *, key: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for payload in payloads:
        data = payload.get("data") or {}
        rows = data.get(key) if isinstance(data, dict) else []
        result.extend(row for row in (rows or []) if isinstance(row, dict))
    return result


def _date_keys(start_date: str, end_date_exclusive: str) -> list[str]:
    start = date.fromisoformat(start_date)
    end = date.fromisoformat(end_date_exclusive)
    return [
        (start + timedelta(days=offset)).isoformat()
        for offset in range((end - start).days)
    ]


def validate_paid_snapshot_window(
    start_date: str,
    end_date_exclusive: str,
    *,
    max_requested_days: int,
) -> list[str]:
    if not 1 <= max_requested_days <= 31:
        raise OrderAttributionError("付款快照允许的最大天数必须在1至31之间")
    requested_dates = _date_keys(start_date, end_date_exclusive)
    if not requested_dates:
        raise OrderAttributionError("付款快照日期窗口必须至少包含1天")
    if len(requested_dates) > max_requested_days:
        raise OrderAttributionError(f"付款快照单次最多查询{max_requested_days}天")
    return requested_dates


def _live_relationship_map(
    contracts: Iterable[tuple[str, dict[str, Any]]],
) -> tuple[dict[str, str], list[str]]:
    relationships: dict[str, str] = {}
    conflicts: set[str] = set()
    for relationship, contract in contracts:
        for raw_row in contract.get("rows") or []:
            if not isinstance(raw_row, dict):
                continue
            room_id = _safe_str(raw_row.get("id"))
            if not room_id:
                continue
            existing = relationships.get(room_id)
            if existing and existing != relationship:
                conflicts.add(_hash_identifier(room_id))
                continue
            relationships[room_id] = relationship
    return relationships, sorted(conflicts)


def fetch_paid_order_snapshot(
    settings: Settings,
    *,
    start_date: str,
    end_date_exclusive: str,
    time_zone: str = "Asia/Shanghai",
    page_size: int = 100,
    max_pages: int = 100,
    max_requested_days: int = 2,
) -> dict[str, Any]:
    """Fetch the lightweight paid-order snapshot used by near-real-time polling.

    This path intentionally does not call Analytics. When Affiliate permission is
    present it also returns privacy-safe per-video order keys; otherwise product
    monitoring remains available and video monitoring is explicitly disabled.
    """
    if not 1 <= page_size <= 100 or not 1 <= max_pages <= 100:
        raise OrderAttributionError("page_size/max_pages 超出安全范围")
    scopes = granted_scopes(settings)
    if ORDER_SCOPE not in scopes:
        raise OrderAttributionError(f"当前授权缺少订单读取权限：{ORDER_SCOPE}")
    if not keyring.get_password(keyring_service(settings), "access_token"):
        raise OrderAttributionError("尚无 seller access_token，请先完成 OAuth 授权")
    if not settings.tts_app_key or not settings.tts_app_secret:
        raise OrderAttributionError("TTS_APP_KEY/TTS_APP_SECRET 未配置")
    _start_ts, end_ts = _timestamp_range(start_date, end_date_exclusive, time_zone)
    requested_dates = validate_paid_snapshot_window(
        start_date,
        end_date_exclusive,
        max_requested_days=max_requested_days,
    )

    buffered_start = (date.fromisoformat(start_date) - timedelta(days=7)).isoformat()
    order_start_ts, _ = _timestamp_range(buffered_start, end_date_exclusive, time_zone)
    order_payloads = _post_pages(
        settings,
        capability="search_orders_v202309",
        path=ORDER_SEARCH_PATH,
        body={"create_time_ge": order_start_ts, "create_time_lt": end_ts},
        page_size=page_size,
        max_pages=max_pages,
    )
    orders = _safe_snapshot(order_payloads, key="orders")
    paid_snapshot = build_paid_order_snapshot(
        orders,
        requested_dates=requested_dates,
        time_zone=time_zone,
    )
    eligibility = paid_snapshot["eligibility_audit"]
    paid_snapshot_ready = not (
        eligibility.get("unknown_order_statuses")
        or eligibility.get("unknown_line_statuses")
    )

    affiliate_payloads: list[dict[str, Any]] = []
    missing_capabilities: list[str] = []
    if AFFILIATE_SCOPE in scopes:
        affiliate_payloads = _post_pages(
            settings,
            capability="search_seller_affiliate_orders_v202410",
            path=AFFILIATE_ORDER_SEARCH_PATH,
            body={"create_time_ge": order_start_ts, "create_time_lt": end_ts},
            page_size=page_size,
            max_pages=max_pages,
        )
        video_snapshot = build_paid_video_order_snapshot(
            orders,
            _safe_snapshot(affiliate_payloads, key="orders"),
            requested_dates=requested_dates,
            time_zone=time_zone,
        )
    else:
        missing_capabilities.append(AFFILIATE_SCOPE)
        video_snapshot = {
            "policy": VIDEO_ORDER_SNAPSHOT_POLICY,
            "ready": False,
            "rows": [],
            "row_count": 0,
            "errors": [
                f"当前授权缺少{AFFILIATE_SCOPE}，普通订单没有video_id，拒绝猜测视频归因"
            ],
        }

    shop = _latest_shop_context(settings)
    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    normalized_contract = {
        "business_time_zone": time_zone,
        "paid_snapshot_policy": paid_snapshot["policy"],
        "paid_snapshot_dates": paid_snapshot["dates"],
        "paid_snapshot_rows": paid_snapshot["rows"],
        "paid_snapshot_row_count": paid_snapshot["row_count"],
        "paid_snapshot_store_rows": paid_snapshot["store_rows"],
        "paid_snapshot_store_row_count": paid_snapshot["store_row_count"],
        "paid_snapshot_effective_order_count": paid_snapshot["effective_order_count"],
        "paid_snapshot_eligibility_audit": paid_snapshot["eligibility_audit"],
        "paid_snapshot_sales_errors": paid_snapshot["sales_errors"],
        "paid_snapshot_ready": paid_snapshot_ready,
        "video_attribution_policy": video_snapshot["policy"],
        "video_attribution_ready": (
            paid_snapshot_ready and bool(video_snapshot["ready"])
        ),
        "video_order_rows": video_snapshot["rows"],
        "video_order_row_count": video_snapshot["row_count"],
        "video_attribution_errors": video_snapshot["errors"],
    }
    output_dir = (
        PROJECT_ROOT
        / "data"
        / "normalized"
        / "paid_order_snapshot"
        / shop["shop_id"]
        / fetched_at[:10]
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{start_date}_{end_date_exclusive}_{int(time.time())}.json"
    output_path.write_text(
        json.dumps(normalized_contract, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    request_ids = [
        _safe_str(payload.get("request_id"))
        for payload in [*order_payloads, *affiliate_payloads]
        if _safe_str(payload.get("request_id"))
    ]
    return {
        "ok": True,
        "dataset": "paid_order_snapshot",
        "shop": {"id": shop["shop_id"], "name": shop["shop_name"]},
        "window_start": start_date,
        "window_end_exclusive": end_date_exclusive,
        "fetched_at": fetched_at,
        **normalized_contract,
        "request_ids": list(dict.fromkeys(request_ids)),
        "raw_source_paths": [],
        "normalized_source_path": str(output_path),
        "required_scope": [ORDER_SCOPE],
        "granted_scope": scopes,
        "missing_capabilities": missing_capabilities,
        "errors": [],
        "pagination_truncated": False,
        "source_contracts": {
            "orders": ORDER_SEARCH_PATH,
            "affiliate_order_tags": (
                AFFILIATE_ORDER_SEARCH_PATH if AFFILIATE_SCOPE in scopes else None
            ),
            "date_basis": f"paid_time@{time_zone}",
        },
        "privacy": (
            "仅持久化按日商品/视频汇总与不可逆订单键；"
            "未保存买家、地址、电话、达人名或原始订单ID"
        ),
    }


def fetch_order_attribution(
    settings: Settings,
    *,
    start_date: str,
    end_date_exclusive: str,
    time_zone: str = "Asia/Shanghai",
    target_collaboration_is_self_operated: bool = False,
    page_size: int = 100,
    max_pages: int = 100,
) -> dict[str, Any]:
    if not 1 <= page_size <= 100 or not 1 <= max_pages <= 100:
        raise OrderAttributionError("page_size/max_pages 超出安全范围")
    scopes = granted_scopes(settings)
    if ORDER_SCOPE not in scopes:
        raise OrderAttributionError(f"当前授权缺少订单读取权限：{ORDER_SCOPE}")
    if not keyring.get_password(keyring_service(settings), "access_token"):
        raise OrderAttributionError("尚无 seller access_token，请先完成 OAuth 授权")
    if not settings.tts_app_key or not settings.tts_app_secret:
        raise OrderAttributionError("TTS_APP_KEY/TTS_APP_SECRET 未配置")
    _start_ts, end_ts = _timestamp_range(start_date, end_date_exclusive, time_zone)
    requested_span = (date.fromisoformat(end_date_exclusive) - date.fromisoformat(start_date)).days
    if requested_span > 7:
        raise OrderAttributionError("订单归因单次最多查询7天")
    # Orders can be created before payment.  Search an earlier create-time buffer,
    # then retain only lines whose paid_time belongs to the requested dates.
    buffered_start = (date.fromisoformat(start_date) - timedelta(days=7)).isoformat()
    order_start_ts, _ = _timestamp_range(buffered_start, end_date_exclusive, time_zone)
    order_payloads = _post_pages(
        settings,
        capability="search_orders_v202309",
        path=ORDER_SEARCH_PATH,
        body={"create_time_ge": order_start_ts, "create_time_lt": end_ts},
        page_size=page_size,
        max_pages=max_pages,
    )
    orders = _safe_snapshot(order_payloads, key="orders")
    requested_dates = _date_keys(start_date, end_date_exclusive)
    paid_snapshot = build_paid_order_snapshot(
        orders,
        requested_dates=requested_dates,
        time_zone=time_zone,
    )
    affiliate_payloads: list[dict[str, Any]] = []
    analytics_contracts: list[dict[str, Any]] = []
    missing_capabilities: list[str] = []
    latest_analytics_dates: list[str] = []
    product_rows_by_date: dict[str, list[dict[str, Any]]] = {}
    if AFFILIATE_SCOPE in scopes:
        affiliate_payloads = _post_pages(
            settings,
            capability="search_seller_affiliate_orders_v202410",
            path=AFFILIATE_ORDER_SEARCH_PATH,
            body={"create_time_ge": order_start_ts, "create_time_lt": end_ts},
            page_size=page_size,
            max_pages=max_pages,
        )
        classified = classify_order_tags_exact(
            orders,
            _safe_snapshot(affiliate_payloads, key="orders"),
            complete_dates=requested_dates,
            time_zone=time_zone,
            target_collaboration_is_self_operated=target_collaboration_is_self_operated,
        )
    else:
        missing_capabilities.append(AFFILIATE_SCOPE)
        relationship_contracts: list[tuple[str, dict[str, Any]]] = []
        complete_dates: list[str] = []
        for day in requested_dates:
            next_day = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
            product_contract = fetch_analytics(
                settings,
                dataset="shop_product_performance",
                start_date=day,
                end_date_exclusive=next_day,
                page_size=page_size,
                max_pages=max_pages,
            )
            analytics_contracts.append(product_contract)
            latest = _safe_str(product_contract.get("latest_available_date"))
            if latest:
                latest_analytics_dates.append(latest)
            if not latest or latest < day:
                continue
            product_rows_by_date[day] = [
                row for row in product_contract.get("rows") or []
                if isinstance(row, dict)
            ]
            for relationship, account_type in (
                ("alliance", "AFFILIATE_ACCOUNTS"),
                ("self_operated", "OFFICIAL_ACCOUNTS"),
                ("self_operated", "MARKETING_ACCOUNTS"),
            ):
                live_contract = fetch_analytics(
                    settings,
                    dataset="shop_live_performance_list",
                    start_date=day,
                    end_date_exclusive=next_day,
                    account_type=account_type,
                    page_size=page_size,
                    max_pages=max_pages,
                )
                analytics_contracts.append(live_contract)
                relationship_contracts.append((relationship, live_contract))
            complete_dates.append(day)
        relationships, relationship_conflicts = _live_relationship_map(
            relationship_contracts
        )
        classified = classify_order_attribution(
            orders,
            product_rows_by_date,
            live_session_relationships=relationships,
            complete_dates=complete_dates,
            time_zone=time_zone,
            live_session_conflicts=relationship_conflicts,
        )
        classified["attribution_unavailable_reason"] = (
            f"正式授权缺少{AFFILIATE_SCOPE}；维持当前Analytics近似渠道口径"
        )
    write_ready_candidates = (
        requested_dates
        if classified["classification_policy"] == CLASSIFICATION_POLICY
        else sorted(product_rows_by_date)
    )
    write_ready_dates = write_ready_candidates if classified["write_ready"] else []
    pending_dates = [day for day in requested_dates if day not in write_ready_dates]
    eligibility = paid_snapshot["eligibility_audit"]
    paid_snapshot_ready = not (
        eligibility.get("unknown_order_statuses")
        or eligibility.get("unknown_line_statuses")
    )
    latest_available_date = (
        requested_dates[-1] if classified["classification_policy"] == CLASSIFICATION_POLICY
        else max(latest_analytics_dates, default=None)
    )
    shop = _latest_shop_context(settings)
    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    output_dir = PROJECT_ROOT / "data" / "normalized" / "order_attribution" / shop["shop_id"] / fetched_at[:10]
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{start_date}_{end_date_exclusive}.json"
    normalized_contract = {
        **classified,
        "paid_snapshot_policy": paid_snapshot["policy"],
        "paid_snapshot_dates": paid_snapshot["dates"],
        "paid_snapshot_rows": paid_snapshot["rows"],
        "paid_snapshot_row_count": paid_snapshot["row_count"],
        "paid_snapshot_store_rows": paid_snapshot["store_rows"],
        "paid_snapshot_store_row_count": paid_snapshot["store_row_count"],
        "paid_snapshot_effective_order_count": paid_snapshot["effective_order_count"],
        "paid_snapshot_eligibility_audit": paid_snapshot["eligibility_audit"],
        "paid_snapshot_sales_errors": paid_snapshot["sales_errors"],
        "paid_snapshot_ready": paid_snapshot_ready,
    }
    output_path.write_text(
        json.dumps(normalized_contract, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    request_ids = [
        _safe_str(payload.get("request_id"))
        for payload in [*order_payloads, *affiliate_payloads]
        if _safe_str(payload.get("request_id"))
    ]
    for contract in analytics_contracts:
        request_ids.extend(
            _safe_str(value)
            for value in contract.get("request_ids") or []
            if _safe_str(value)
        )
    analytics_raw_paths = [
        str(value)
        for contract in analytics_contracts
        for value in contract.get("raw_source_paths") or []
        if str(value)
    ]
    return {
        "ok": True,
        "dataset": "order_attribution",
        "shop": {"id": shop["shop_id"], "name": shop["shop_name"]},
        "window_start": start_date,
        "window_end_exclusive": end_date_exclusive,
        "fetched_at": fetched_at,
        **normalized_contract,
        "request_ids": list(dict.fromkeys(request_ids)),
        "raw_source_paths": analytics_raw_paths,
        "normalized_source_path": str(output_path),
        "required_scope": list(REQUIRED_SCOPES),
        "granted_scope": scopes,
        "missing_capabilities": missing_capabilities,
        "errors": [],
        "pagination_truncated": False,
        "latest_available_date": latest_available_date,
        "write_ready_dates": write_ready_dates,
        "pending_dates": pending_dates,
        "source_contracts": {
            "orders": ORDER_SEARCH_PATH,
            "affiliate_order_tags": (
                AFFILIATE_ORDER_SEARCH_PATH if AFFILIATE_SCOPE in scopes else None
            ),
            "fallback_channel_source": (
                None
                if AFFILIATE_SCOPE in scopes
                else "shop_product_performance+shop_live_performance_list"
            ),
            "date_basis": f"paid_time@{time_zone}",
        },
        "privacy": "仅持久化按日商品与店铺汇总；未保存买家、地址、电话、达人名或原始订单ID",
    }


def failed_order_attribution_contract(
    *, start_date: str, end_date_exclusive: str, error: Exception, settings: Settings
) -> dict[str, Any]:
    scopes = granted_scopes(settings)
    return {
        "ok": False,
        "dataset": "order_attribution",
        "shop": None,
        "window_start": start_date,
        "window_end_exclusive": end_date_exclusive,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "rows": [],
        "row_count": 0,
        "store_rows": [],
        "store_row_count": 0,
        "request_ids": [],
        "raw_source_paths": [],
        "normalized_source_path": None,
        "required_scope": list(REQUIRED_SCOPES),
        "granted_scope": scopes,
        "missing_capabilities": [scope for scope in REQUIRED_SCOPES if scope not in scopes],
        "errors": [redact_text(error)],
        "pagination_truncated": False,
        "latest_available_date": None,
        "write_ready_dates": [],
        "pending_dates": [],
        "classification_policy": CLASSIFICATION_POLICY,
        "approximation_acknowledged": False,
        "business_time_zone": "Asia/Shanghai",
        "paid_snapshot_policy": PAID_SNAPSHOT_POLICY,
        "paid_snapshot_dates": [],
        "paid_snapshot_rows": [],
        "paid_snapshot_row_count": 0,
        "paid_snapshot_store_rows": [],
        "paid_snapshot_store_row_count": 0,
        "paid_snapshot_effective_order_count": 0,
        "paid_snapshot_eligibility_audit": {},
        "paid_snapshot_sales_errors": [],
        "paid_snapshot_ready": False,
        "write_ready": False,
    }


def failed_paid_order_snapshot_contract(
    *, start_date: str, end_date_exclusive: str, error: Exception, settings: Settings
) -> dict[str, Any]:
    scopes = granted_scopes(settings)
    return {
        "ok": False,
        "dataset": "paid_order_snapshot",
        "shop": None,
        "window_start": start_date,
        "window_end_exclusive": end_date_exclusive,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "business_time_zone": "Asia/Shanghai",
        "paid_snapshot_policy": PAID_SNAPSHOT_POLICY,
        "paid_snapshot_dates": [],
        "paid_snapshot_rows": [],
        "paid_snapshot_row_count": 0,
        "paid_snapshot_store_rows": [],
        "paid_snapshot_store_row_count": 0,
        "paid_snapshot_effective_order_count": 0,
        "paid_snapshot_eligibility_audit": {},
        "paid_snapshot_sales_errors": [],
        "paid_snapshot_ready": False,
        "video_attribution_policy": VIDEO_ORDER_SNAPSHOT_POLICY,
        "video_attribution_ready": False,
        "video_order_rows": [],
        "video_order_row_count": 0,
        "video_attribution_errors": [],
        "request_ids": [],
        "raw_source_paths": [],
        "normalized_source_path": None,
        "required_scope": [ORDER_SCOPE],
        "granted_scope": scopes,
        "missing_capabilities": [ORDER_SCOPE] if ORDER_SCOPE not in scopes else [],
        "errors": [redact_text(error)],
        "pagination_truncated": False,
    }
