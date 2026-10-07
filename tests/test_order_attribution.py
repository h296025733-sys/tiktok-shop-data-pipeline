from __future__ import annotations

import pytest

from src.config import Settings
from src.tiktok.order_attribution import (
    AFFILIATE_SCOPE,
    ANALYTICS_SCOPE,
    CLASSIFICATION_POLICY,
    LEGACY_CLASSIFICATION_POLICY,
    ORDER_SCOPE,
    PAID_SNAPSHOT_POLICY,
    VIDEO_ORDER_SNAPSHOT_POLICY,
    OrderAttributionError,
    build_paid_order_snapshot,
    build_paid_video_order_snapshot,
    classify_order_attribution,
    classify_order_tags_exact,
    fetch_order_attribution,
    validate_paid_snapshot_window,
)
from src.tiktok.safety import EndpointPolicy


DAY_TS = 1785513600


def analytics_row(
    product_id: str = "p1",
    *,
    total_orders: int = 4,
    total_items: int = 5,
    self_live_orders: int = 1,
    self_live_items: int = 1,
    self_video_orders: int = 0,
    self_video_items: int = 0,
    card_orders: int = 1,
    card_items: int = 2,
    affiliate_orders: int = 2,
    affiliate_items: int = 2,
) -> dict:
    return {
        "id": product_id,
        "total_performance": {"orders": total_orders, "items_sold": total_items},
        "seller_live_performance": {
            "attributed_orders": self_live_orders,
            "attributed_sold_items": self_live_items,
        },
        "seller_video_performance": {
            "attributed_orders": self_video_orders,
            "attributed_sold_items": self_video_items,
        },
        "seller_product_card_performance": {
            "attributed_orders": card_orders,
            "attributed_sold_items": card_items,
        },
        "affiliate_total_performance": {
            "attributed_orders": affiliate_orders,
            "attributed_sold_items": affiliate_items,
        },
    }


def paid_order(order_id: str, quantity: int, *, product_id: str = "p1", room_id: str = "") -> dict:
    return {
        "id": order_id,
        "status": "COMPLETED",
        "create_time": DAY_TS,
        "paid_time": DAY_TS,
        "payment": {"total_amount": "9.99"},
        "line_items": [{
            "product_id": product_id,
            "sku_id": f"sku-{product_id}",
            "quantity": quantity,
            "sale_price": "3.33",
            "currency": "USD",
            **({"room_id": room_id} if room_id else {}),
        }],
    }


def test_order_and_analytics_endpoints_are_exactly_allowlisted() -> None:
    policy = EndpointPolicy.from_project_config(Settings(_env_file=None))
    policy.assert_allowed(
        capability="search_seller_affiliate_orders_v202410",
        method="POST",
        url="https://open-api.tiktokglobalshop.com/affiliate_seller/202410/orders/search",
        mutating=False,
    )
    policy.assert_allowed(
        capability="search_orders_v202309",
        method="POST",
        url="https://open-api.tiktokglobalshop.com/order/202309/orders/search",
        mutating=False,
    )
    policy.assert_allowed(
        capability="shop_product_performance",
        method="GET",
        url="https://open-api.tiktokglobalshop.com/analytics/202605/shop_products/performance",
        mutating=False,
    )
    policy.assert_allowed(
        capability="shop_live_performance_list",
        method="GET",
        url="https://open-api.tiktokglobalshop.com/analytics/202509/shop_lives/performance",
        mutating=False,
    )


def test_missing_scopes_block_before_network(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("src.tiktok.order_attribution.granted_scopes", lambda settings: [])
    with pytest.raises(OrderAttributionError, match=ORDER_SCOPE):
        fetch_order_attribution(
            Settings(_env_file=None, tts_app_key="app", tts_app_secret="secret"),
            start_date="2026-08-01",
            end_date_exclusive="2026-08-02",
        )


def test_hybrid_split_uses_affiliate_non_live_as_video() -> None:
    orders = [
        paid_order("affiliate-live", 1, room_id="affiliate-room"),
        paid_order("affiliate-non-live", 1),
        paid_order("self-live", 1, room_id="self-room"),
        paid_order("card", 2),
    ]
    result = classify_order_attribution(
        orders,
        {"2026-08-01": [analytics_row()]},
        live_session_relationships={
            "affiliate-room": "alliance",
            "self-room": "self_operated",
        },
        complete_dates=["2026-08-01"],
    )
    assert result["classification_policy"] == LEGACY_CLASSIFICATION_POLICY
    assert result["approximation_acknowledged"] is True
    assert result["write_ready"] is True
    row = result["rows"][0]
    assert (row["alliance_live_orders"], row["alliance_live_items"]) == (1, 1)
    assert (row["alliance_video_orders"], row["alliance_video_items"]) == (1, 1)
    assert (row["self_operated_live_orders"], row["self_operated_live_items"]) == (1, 1)
    assert (row["product_card_orders"], row["product_card_items"]) == (1, 2)
    assert (row["total_orders"], row["total_items"]) == (4, 5)
    assert result["store_rows"][0]["total_orders"] == 4


def test_unmatched_live_room_blocks_writing() -> None:
    result = classify_order_attribution(
        [paid_order("live", 1, room_id="unknown-room")],
        {"2026-08-01": [analytics_row(
            total_orders=1,
            total_items=1,
            self_live_orders=0,
            self_live_items=0,
            card_orders=0,
            card_items=0,
            affiliate_orders=1,
            affiliate_items=1,
        )]},
        live_session_relationships={},
        complete_dates=["2026-08-01"],
    )
    assert result["unclassified_items"] == 1
    assert result["write_ready"] is False
    assert any("未匹配账号类型" in message for message in result["reconciliation_errors"])


def test_affiliate_live_cannot_exceed_affiliate_total() -> None:
    result = classify_order_attribution(
        [paid_order("live", 1, room_id="affiliate-room")],
        {"2026-08-01": [analytics_row(
            total_orders=1,
            total_items=1,
            self_live_orders=0,
            self_live_items=0,
            card_orders=1,
            card_items=1,
            affiliate_orders=0,
            affiliate_items=0,
        )]},
        live_session_relationships={"affiliate-room": "alliance"},
        complete_dates=["2026-08-01"],
    )
    assert result["write_ready"] is False
    assert any("超过联盟总量" in message for message in result["reconciliation_errors"])


def test_self_live_uses_product_analytics_and_keeps_room_observation_as_warning() -> None:
    result = classify_order_attribution(
        [paid_order("live", 1, room_id="self-room")],
        {"2026-08-01": [analytics_row(
            total_orders=1,
            total_items=1,
            self_live_orders=0,
            self_live_items=0,
            card_orders=1,
            card_items=1,
            affiliate_orders=0,
            affiliate_items=0,
        )]},
        live_session_relationships={"self-room": "self_operated"},
        complete_dates=["2026-08-01"],
    )
    assert result["write_ready"] is True
    assert result["rows"][0]["self_operated_live_orders"] == 0
    assert any("采用商品Analytics校准值" in message for message in result["reconciliation_warnings"])


def test_multi_product_order_blocks_unsafe_store_order_sum() -> None:
    order = paid_order("multi", 1, product_id="p1")
    order["line_items"].append({"product_id": "p2", "sku_id": "sku-p2", "quantity": 1})
    result = classify_order_attribution(
        [order],
        {
            "2026-08-01": [
                analytics_row("p1", total_orders=1, total_items=1, self_live_orders=0, self_live_items=0,
                              card_orders=1, card_items=1, affiliate_orders=0, affiliate_items=0),
                analytics_row("p2", total_orders=1, total_items=1, self_live_orders=0, self_live_items=0,
                              card_orders=1, card_items=1, affiliate_orders=0, affiliate_items=0),
            ],
        },
        live_session_relationships={},
        complete_dates=["2026-08-01"],
    )
    assert result["write_ready"] is False
    assert any("包含多个商品" in message for message in result["reconciliation_errors"])


def test_sample_gift_unpaid_and_unknown_status_are_not_silent_sales() -> None:
    orders = [
        {**paid_order("sample", 1), "is_sample_order": True},
        {**paid_order("lottery", 1), "order_type": "ZERO_LOTTERY"},
        {
            "id": "unpaid", "status": "UNPAID", "create_time": DAY_TS,
            "payment": {"total_amount": "99"},
            "line_items": [{"product_id": "p1", "sku_id": "s1", "quantity": 1}],
        },
        {
            **paid_order("future", 1),
            "status": "NEW_FUTURE_STATUS",
            "line_items": [{
                "product_id": "p1", "sku_id": "s1", "quantity": 1,
                "display_status": "NEW_FUTURE_STATUS",
            }],
        },
    ]
    result = classify_order_attribution(
        orders,
        {"2026-08-01": []},
        live_session_relationships={},
        complete_dates=["2026-08-01"],
    )
    assert result["effective_order_count"] == 0
    assert result["write_ready"] is False
    assert result["eligibility_audit"]["excluded_sample_orders"] == 1
    assert result["eligibility_audit"]["excluded_non_sale_orders"] == 1
    assert result["eligibility_audit"]["excluded_without_paid_time_orders"] == 1
    assert result["eligibility_audit"]["unknown_order_statuses"] == ["NEW_FUTURE_STATUS"]


def test_scope_and_policy_constants_do_not_drift() -> None:
    assert ORDER_SCOPE == "seller.order.info"
    assert AFFILIATE_SCOPE == "seller.affiliate_collaboration.read"
    assert ANALYTICS_SCOPE == "data.shop_analytics.public.read"
    assert CLASSIFICATION_POLICY == "seller_affiliate_order_tags_exact_v1"
    assert LEGACY_CLASSIFICATION_POLICY == "affiliate_non_live_as_video"


def test_report_paid_snapshot_allows_a_complete_month_without_widening_realtime_polling() -> None:
    with pytest.raises(OrderAttributionError, match="最多查询2天"):
        validate_paid_snapshot_window(
            "2026-08-01", "2026-08-08", max_requested_days=2
        )
    assert len(validate_paid_snapshot_window(
        "2026-08-01", "2026-09-01", max_requested_days=31
    )) == 31
    with pytest.raises(OrderAttributionError, match="至少包含1天"):
        validate_paid_snapshot_window(
            "2026-08-01", "2026-08-01", max_requested_days=31
        )


def test_paid_snapshot_counts_current_paid_sales_without_waiting_for_analytics() -> None:
    cancelled = {**paid_order("cancelled", 2), "status": "CANCELLED"}
    sample = {**paid_order("sample", 1), "is_sample_order": True}
    snapshot = build_paid_order_snapshot(
        [paid_order("paid", 3), cancelled, sample],
        requested_dates=["2026-08-01", "2026-08-02"],
    )
    assert snapshot["policy"] == PAID_SNAPSHOT_POLICY
    assert snapshot["rows"] == [{
        "date": "2026-08-01",
        "product_id": "p1",
        "total_orders": 1,
        "total_items": 3,
        "sales_amount": "9.99",
        "sales_currency": "USD",
        "sales_ready": True,
        "total_order_keys": snapshot["rows"][0]["total_order_keys"],
        "total_order_paid_at": snapshot["rows"][0]["total_order_paid_at"],
    }]
    assert snapshot["store_rows"][0]["total_orders"] == 1
    assert snapshot["store_rows"][0]["total_items"] == 3
    assert set(snapshot["store_rows"][0]["total_order_paid_at"].values()) == {DAY_TS}
    assert snapshot["store_rows"][1]["total_orders"] == 0
    assert snapshot["eligibility_audit"]["excluded_status_orders"] == 1
    assert snapshot["eligibility_audit"]["excluded_sample_orders"] == 1


def test_exact_order_tags_cover_four_creator_buckets_and_residual_product_card() -> None:
    orders = [
        paid_order("alliance-video", 1),
        paid_order("alliance-live", 1),
        paid_order("self-video", 1),
        paid_order("self-live", 1),
        paid_order("card", 1),
    ]
    affiliate_orders = [
        affiliate_order("alliance-video", content_type="VIDEO", relationship="open"),
        affiliate_order("alliance-live", content_type="LIVE", relationship="open"),
        affiliate_order("self-video", content_type="VIDEO", relationship="target"),
        affiliate_order("self-live", content_type="PRE_LIVE", relationship="target"),
    ]
    result = classify_order_tags_exact(
        orders,
        affiliate_orders,
        complete_dates=["2026-08-01"],
        target_collaboration_is_self_operated=True,
    )
    assert result["classification_policy"] == CLASSIFICATION_POLICY
    assert result["approximation_acknowledged"] is False
    assert result["write_ready"] is True
    row = result["rows"][0]
    assert (row["alliance_video_orders"], row["alliance_video_items"]) == (1, 1)
    assert (row["alliance_live_orders"], row["alliance_live_items"]) == (1, 1)
    assert (row["self_operated_video_orders"], row["self_operated_video_items"]) == (1, 1)
    assert (row["self_operated_live_orders"], row["self_operated_live_items"]) == (1, 1)
    assert (row["product_card_orders"], row["product_card_items"]) == (1, 1)
    assert (row["total_orders"], row["total_items"]) == (5, 5)


def test_target_relationship_and_unknown_content_are_hard_write_gates() -> None:
    order = paid_order("target-live", 1)
    affiliate = affiliate_order("target-live", content_type="LIVE", relationship="target")
    uncalibrated = classify_order_tags_exact(
        [order],
        [affiliate],
        complete_dates=["2026-08-01"],
        target_collaboration_is_self_operated=False,
    )
    assert uncalibrated["write_ready"] is False
    assert uncalibrated["unclassified_items"] == 1

    unknown = classify_order_tags_exact(
        [order],
        [affiliate_order("target-live", content_type="LINKSHARE", relationship="target")],
        complete_dates=["2026-08-01"],
        target_collaboration_is_self_operated=True,
    )
    assert unknown["write_ready"] is False
    assert any("未知内容类型" in value for value in unknown["reconciliation_errors"])


def test_paid_video_snapshot_uses_only_exact_affiliate_content_id() -> None:
    orders = [
        paid_order("video-one", 1),
        paid_order("video-two", 2),
        paid_order("live", 1),
        paid_order("card", 1),
    ]
    affiliate_orders = [
        affiliate_order(
            "video-one", content_type="VIDEO", relationship="open", content_id="7667000000000000001"
        ),
        affiliate_order(
            "video-two", content_type="VIDEO", relationship="target", content_id="7667000000000000001",
            quantity=2,
        ),
        affiliate_order(
            "live", content_type="LIVE", relationship="open", content_id="7520000000000000001"
        ),
    ]
    result = build_paid_video_order_snapshot(
        orders,
        affiliate_orders,
        requested_dates=["2026-08-01"],
    )
    assert result["policy"] == VIDEO_ORDER_SNAPSHOT_POLICY
    assert result["ready"] is True
    assert result["row_count"] == 1
    assert result["rows"][0]["video_id"] == "7667000000000000001"
    assert result["rows"][0]["total_orders"] == 2
    assert result["rows"][0]["total_items"] == 3
    assert len(result["rows"][0]["total_order_keys"]) == 2
    assert set(result["rows"][0]["total_order_paid_at"].values()) == {DAY_TS}


def test_paid_video_snapshot_refuses_video_without_content_id() -> None:
    result = build_paid_video_order_snapshot(
        [paid_order("video-missing-id", 1)],
        [affiliate_order("video-missing-id", content_type="VIDEO", relationship="open")],
        requested_dates=["2026-08-01"],
    )
    assert result["ready"] is False
    assert result["rows"] == []
    assert any("content_id" in value for value in result["errors"])


def affiliate_order(
    order_id: str,
    *,
    content_type: str,
    relationship: str,
    content_id: str = "",
    quantity: int = 1,
) -> dict:
    sku = {
        "id": "sku-p1",
        "product_id": "p1",
        "quantity": quantity,
        "content_type": content_type,
        **({"content_id": content_id} if content_id else {}),
    }
    if relationship == "target":
        sku["target_collaboration_id"] = "target-1"
    elif relationship == "open":
        sku["open_collaboration_id"] = "open-1"
    return {"id": order_id, "skus": [sku]}
