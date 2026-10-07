from __future__ import annotations

import pytest

from src.config import Settings
from src.tiktok.products import (
    ProductCatalogError,
    canonicalize_products,
    extract_product_page,
    fetch_product_catalog,
)
from src.tiktok.safety import EndpointPolicy, SafetyViolation


def test_product_catalog_endpoint_is_exactly_allowlisted() -> None:
    settings = Settings(_env_file=None)
    policy = EndpointPolicy.from_project_config(settings)
    policy.assert_allowed(
        capability="search_products_v202502",
        method="POST",
        url="https://open-api.tiktokglobalshop.com/product/202502/products/search",
        mutating=False,
    )
    with pytest.raises(SafetyViolation):
        policy.assert_allowed(
            capability="search_products_v202502",
            method="POST",
            url="https://open-api.tiktokglobalshop.com/product/202502/products/delete",
            mutating=False,
        )


def test_product_detail_endpoint_only_allows_numeric_ids() -> None:
    settings = Settings(_env_file=None)
    policy = EndpointPolicy.from_project_config(settings)
    policy.assert_allowed(
        capability="get_product_v202309",
        method="GET",
        url="https://open-api.tiktokglobalshop.com/product/202309/products/1732000000000000001",
        mutating=False,
    )
    with pytest.raises(SafetyViolation):
        policy.assert_allowed(
            capability="get_product_v202309",
            method="GET",
            url="https://open-api.tiktokglobalshop.com/product/202309/products/../secrets",
            mutating=False,
        )


def test_product_catalog_requires_product_basic_scope_before_network(monkeypatch) -> None:
    monkeypatch.setattr("src.tiktok.products.granted_scopes", lambda settings: ["seller.authorization.info"])
    with pytest.raises(ProductCatalogError, match="seller.product.basic"):
        fetch_product_catalog(Settings(_env_file=None, tts_app_key="app", tts_app_secret="secret"))


def test_product_catalog_deduplicates_exact_rows_and_rejects_conflicts() -> None:
    rows, duplicates, conflicts = canonicalize_products([
        {"id": "1", "title": "A"},
        {"id": "1", "title": "A"},
        {"id": "2", "title": "B"},
        {"id": "2", "title": "C"},
        {"id": "3", "title": "D"},
    ])
    assert rows == [{"id": "1", "title": "A"}, {"id": "3", "title": "D"}]
    assert duplicates == 1
    assert conflicts == ["2"]


def test_product_catalog_accepts_empty_terminal_page_after_all_rows() -> None:
    rows, token = extract_product_page(
        {"total_count": 9, "next_page_token": ""},
        already_fetched=9,
    )
    assert rows == []
    assert token == ""


def test_product_catalog_does_not_hide_missing_rows() -> None:
    with pytest.raises(ProductCatalogError, match="products"):
        extract_product_page(
            {"total_count": 9, "next_page_token": ""},
            already_fetched=8,
        )
    with pytest.raises(ProductCatalogError, match="products"):
        extract_product_page(
            {"total_count": 9, "next_page_token": "another-page"},
            already_fetched=9,
        )
