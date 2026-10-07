from __future__ import annotations

import logging

import pytest

from src.config import Settings
from src.logging_config import RedactingFilter, redact_text
from src.tiktok.safety import EndpointPolicy, SafetyViolation


def test_safe_mode_blocks_mutating_endpoint_before_network() -> None:
    settings = Settings(
        _env_file=None,
        safe_mode=True,
        tiktok_write_actions_enabled=False,
    )
    policy = EndpointPolicy(settings=settings, entries=[])
    with pytest.raises(SafetyViolation, match="SAFE_MODE"):
        policy.assert_allowed(
            capability="test-write",
            method="POST",
            url="https://example.invalid/write/test",
            mutating=True,
        )


def test_unapproved_read_endpoint_is_blocked() -> None:
    settings = Settings(_env_file=None)
    policy = EndpointPolicy(settings=settings, entries=[])
    with pytest.raises(SafetyViolation, match="白名单"):
        policy.assert_allowed(
            capability="shops",
            method="GET",
            url="https://example.invalid/read/test",
        )


def test_video_performance_endpoint_is_exactly_allowlisted() -> None:
    settings = Settings(_env_file=None)
    policy = EndpointPolicy.from_project_config(settings)
    policy.assert_allowed(
        capability="shop_video_performance",
        method="GET",
        url=(
            "https://open-api.tiktokglobalshop.com"
            "/analytics/202605/shop_videos/performance"
        ),
    )


def test_dynamic_analytics_paths_only_allow_numeric_resource_ids() -> None:
    settings = Settings(_env_file=None)
    policy = EndpointPolicy.from_project_config(settings)
    policy.assert_allowed(
        capability="product_performance_detail",
        method="GET",
        url=(
            "https://open-api.tiktokglobalshop.com"
            "/analytics/202509/shop_products/1732000000000000001/performance"
        ),
    )
    with pytest.raises(SafetyViolation):
        policy.assert_allowed(
            capability="product_performance_detail",
            method="GET",
            url=(
                "https://open-api.tiktokglobalshop.com"
                "/analytics/202509/shop_products/../performance"
            ),
        )


def test_video_product_dataset_uses_a_numeric_video_resource() -> None:
    from src.tiktok.analytics import DATASETS

    settings = Settings(_env_file=None)
    policy = EndpointPolicy.from_project_config(settings)
    dataset = DATASETS["shop_video_product_performance"]
    assert dataset.list_key == "products"
    assert dataset.paginated is True
    assert dataset.path.format(resource_id="7667272841808710926") == (
        "/analytics/202409/shop_videos/7667272841808710926/products/performance"
    )
    policy.assert_allowed(
        capability="shop_performance_hourly",
        method="GET",
        url=(
            "https://open-api.tiktokglobalshop.com"
            "/analytics/202510/shop/performance/2026-07-26/performance_per_hour"
        ),
    )
    policy.assert_allowed(
        capability="shop_video_product_performance_v202409",
        method="GET",
        url=(
            "https://open-api.tiktokglobalshop.com"
            "/analytics/202409/shop_videos/7667272841808710926/products/performance"
        ),
    )
    policy.assert_allowed(
        capability="shop_video_performance_detail_v202509",
        method="GET",
        url=(
            "https://open-api.tiktokglobalshop.com"
            "/analytics/202509/shop_videos/7667272841808710926/performance"
        ),
    )
    live_dataset = DATASETS["shop_live_product_performance"]
    assert live_dataset.list_key == "products"
    assert live_dataset.paginated is False
    assert live_dataset.path.format(resource_id="7520000000000000000") == (
        "/analytics/202512/shop/7520000000000000000/products_performance"
    )
    policy.assert_allowed(
        capability="shop_live_product_performance_v202512",
        method="GET",
        url=(
            "https://open-api.tiktokglobalshop.com"
            "/analytics/202512/shop/7520000000000000000/products_performance"
        ),
    )
    with pytest.raises(SafetyViolation):
        policy.assert_allowed(
            capability="shop_video_product_performance_v202409",
            method="GET",
            url=(
                "https://open-api.tiktokglobalshop.com"
                "/analytics/202409/shop_videos/../products/performance"
            ),
        )
    with pytest.raises(SafetyViolation):
        policy.assert_allowed(
            capability="shop_video_performance",
            method="GET",
            url=(
                "https://open-api.tiktokglobalshop.com"
                "/analytics/202605/shop_videos/overview_performance"
            ),
        )
    policy.assert_allowed(
        capability="shop_video_performance_v202509",
        method="GET",
        url=(
            "https://open-api.tiktokglobalshop.com"
            "/analytics/202509/shop_videos/performance"
        ),
    )


def test_secret_redaction() -> None:
    raw = (
        "app_secret=abcdefghijklmnopqrstuvwxyz "
        "access_token:1234567890abcdefghijkl Bearer abcdefghijklmnop"
    )
    redacted = redact_text(raw)
    assert "abcdefghijklmnopqrstuvwxyz" not in redacted
    assert "1234567890abcdefghijkl" not in redacted
    assert "abcdefghijklmnop" not in redacted
    assert "abcd…wxyz" in redacted


def test_logging_filter_redacts_message() -> None:
    record = logging.LogRecord(
        "test", logging.INFO, __file__, 1, "refresh_token=abcdefghijklmnop", (), None
    )
    assert RedactingFilter().filter(record)
    assert "abcdefghijklmnop" not in str(record.msg)


def test_logging_filter_preserves_numeric_format_arguments() -> None:
    record = logging.LogRecord(
        "test",
        logging.INFO,
        __file__,
        1,
        'HTTP Request: "%s %d"',
        ("HTTP/1.1", 404),
        None,
    )
    assert RedactingFilter().filter(record)
    assert record.getMessage() == 'HTTP Request: "HTTP/1.1 404"'


def test_logging_filter_redacts_oauth_query_parameters() -> None:
    record = logging.LogRecord(
        "httpx",
        logging.INFO,
        __file__,
        1,
        "GET https://example.test/token?app_secret=%s&auth_code=%s&grant_type=x",
        ("abcdefghijklmnopqrstuvwxyz", "ROW_abcdefghijklmnop"),
        None,
    )
    assert RedactingFilter().filter(record)
    rendered = record.getMessage()
    assert "abcdefghijklmnopqrstuvwxyz" not in rendered
    assert "ROW_abcdefghijklmnop" not in rendered
    assert "grant_type=x" in rendered
