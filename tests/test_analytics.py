from __future__ import annotations

import pytest

from src.config import Settings
from src.tiktok.analytics import AnalyticsError, DATASETS, fetch_analytics


def test_officially_verified_analytics_datasets_are_registered() -> None:
    assert set(DATASETS) == {
        "shop_product_performance",
        "shop_sku_performance",
        "product_performance_detail",
        "shop_video_performance_detail",
        "shop_video_performance_overview",
        "shop_video_product_performance",
        "shop_live_performance_list",
        "shop_live_performance_overview",
        "shop_live_product_performance",
        "shop_performance_hourly",
    }


def test_unknown_dataset_is_blocked_before_network() -> None:
    with pytest.raises(AnalyticsError, match="不支持"):
        fetch_analytics(
            Settings(_env_file=None),
            dataset="shop_write_anything",
            start_date="2026-07-25",
            end_date_exclusive="2026-07-26",
        )


def test_today_probe_is_restricted_to_the_documented_video_overview() -> None:
    with pytest.raises(AnalyticsError, match="today=true"):
        fetch_analytics(
            Settings(_env_file=None),
            dataset="shop_product_performance",
            start_date="2026-07-25",
            end_date_exclusive="2026-07-26",
            today=True,
        )
