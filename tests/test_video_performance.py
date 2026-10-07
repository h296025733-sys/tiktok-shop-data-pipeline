from __future__ import annotations

import pytest
from types import SimpleNamespace

from src.tiktok.video_performance import (
    VideoPerformanceError,
    _extract_page_videos,
    _request_page,
    _write_immutable_json,
    _validate_window,
    canonicalize_videos,
    failed_contract,
    flatten_video,
)


def test_flatten_video_keeps_ids_and_metrics() -> None:
    row = flatten_video(
        {
            "id": "1234567890123456789",
            "creator": {"open_id": "creator-id", "author_type": "AFFILIATE"},
            "gmv": {"amount": "12.34", "currency": "USD"},
            "hash_tags": ["#one", "#two"],
        },
        shop_id="shop-id",
        start_date="2026-07-19",
        end_date_exclusive="2026-07-26",
        fetched_at="2026-07-27T00:00:00+00:00",
    )
    assert row["id"] == "1234567890123456789"
    assert row["creator_open_id"] == "creator-id"
    assert row["gmv_amount"] == "12.34"
    assert row["hash_tags"] == '["#one","#two"]'
    assert row["shop_id"] == "shop-id"


def test_window_validation() -> None:
    _validate_window("2026-07-19", "2026-07-26")
    with pytest.raises(VideoPerformanceError):
        _validate_window("2026-07-26", "2026-07-19")
    with pytest.raises(VideoPerformanceError):
        _validate_window("bad-date", "2026-07-19")


def test_canonicalize_videos_deduplicates_exact_and_rejects_conflicts() -> None:
    canonical, exact_duplicates, conflicts = canonicalize_videos(
        [
            {"id": "10000000001", "views": 5},
            {"id": "10000000001", "views": 5},
            {"id": "10000000002", "views": 6},
            {"id": "10000000002", "views": 7},
            {"id": "10000000003", "views": 8},
        ]
    )
    assert exact_duplicates == 1
    assert conflicts == ["10000000002"]
    assert [row["id"] for row in canonical] == ["10000000001", "10000000003"]


def test_immutable_raw_json_has_sha256_and_refuses_overwrite(tmp_path) -> None:
    target = tmp_path / "request.json"
    digest = _write_immutable_json(target, {"code": 0, "data": {"videos": []}})
    assert len(digest) == 64
    assert target.with_suffix(".json.sha256").read_text("ascii").startswith(digest)
    assert _write_immutable_json(target, {"code": 0, "data": {"videos": []}}) == digest
    with pytest.raises(VideoPerformanceError, match="拒绝覆盖"):
        _write_immutable_json(target, {"code": 0, "data": {"videos": [1]}})


def test_zero_total_count_may_omit_videos_array() -> None:
    assert _extract_page_videos(
        {"total_count": 0, "latest_available_date": "2026-07-25"}
    ) == []
    with pytest.raises(VideoPerformanceError, match="data.videos"):
        _extract_page_videos({"total_count": 1})


def test_failed_machine_contract_has_required_fields(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.tiktok.video_performance._token_metadata",
        lambda: {"granted_scopes": ["seller.authorization.info"]},
    )
    result = failed_contract(
        start_date="2026-07-26",
        end_date_exclusive="2026-07-27",
        error=VideoPerformanceError("scope missing"),
    )
    assert result["ok"] is False
    assert result["dataset"] == "shop_video_performance"
    assert result["rows"] == []
    assert result["missing_capabilities"] == ["data.shop_analytics.public.read"]


def test_internal_error_reports_tiktok_request_id(monkeypatch) -> None:
    class FakeResponse:
        status_code = 500
        headers = {}

        def json(self):
            return {
                "code": 36009003,
                "message": "Internal error",
                "request_id": "test-request-id",
            }

    class FakeClient:
        def get(self, *_args, **_kwargs):
            return FakeResponse()

    monkeypatch.setattr(
        "src.tiktok.video_performance.keyring.get_password",
        lambda *_args: "test-token",
    )
    monkeypatch.setattr(
        "src.tiktok.video_performance.keyring_service",
        lambda *_args: "test-service",
    )
    monkeypatch.setattr(
        "src.tiktok.video_performance.generate_signature",
        lambda **_kwargs: "test-signature",
    )
    monkeypatch.setattr("src.tiktok.video_performance.time.sleep", lambda *_args: None)
    with pytest.raises(VideoPerformanceError, match="request_id=test-request-id"):
        _request_page(
            FakeClient(),
            settings=SimpleNamespace(tts_app_secret="test-secret"),
            url="https://example.invalid/analytics",
            base_query={"start_date_ge": "2026-09-21"},
            page_token="",
        )
