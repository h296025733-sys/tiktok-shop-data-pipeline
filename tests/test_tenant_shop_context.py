from __future__ import annotations

import csv

import pytest

from src.config import Settings
from src.tiktok.video_performance import VideoPerformanceError, _latest_shop_context


def _write_shops(root, profile: str, rows: list[dict[str, str]]) -> None:
    target = root / "data" / "normalized" / "authorized_shops" / profile / "2026-08-04" / "shops.csv"
    target.parent.mkdir(parents=True)
    with target.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["id", "name", "cipher"])
        writer.writeheader()
        writer.writerows(rows)


def test_named_profile_selects_configured_shop_without_crossing_profiles(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("src.tiktok.video_performance.PROJECT_ROOT", tmp_path)
    _write_shops(tmp_path, "store-a", [{"id": "101", "name": "Alpha", "cipher": "a"}])
    _write_shops(tmp_path, "store-b", [{"id": "202", "name": "Beta", "cipher": "b"}])
    selected = _latest_shop_context(Settings(
        _env_file=None,
        tts_credential_profile="store-b",
        tts_shop_id="202",
    ))
    assert selected == {"shop_id": "202", "shop_name": "Beta", "shop_cipher": "b"}


def test_named_profile_rejects_shop_that_only_exists_in_other_profile(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("src.tiktok.video_performance.PROJECT_ROOT", tmp_path)
    _write_shops(tmp_path, "store-a", [{"id": "101", "name": "Alpha", "cipher": "a"}])
    with pytest.raises(VideoPerformanceError, match="未找到当前凭证配置"):
        _latest_shop_context(Settings(
            _env_file=None,
            tts_credential_profile="store-b",
            tts_shop_id="101",
        ))
