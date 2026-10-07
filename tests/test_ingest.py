from __future__ import annotations

import csv
from datetime import date, datetime
from pathlib import Path

from openpyxl import Workbook

from src import ingest


def _fixture(path: Path) -> None:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "6.17"
    worksheet.append(
        [
            None,
            "达人昵称",
            "达人ID",
            "视频信息",
            "视频ID",
            "发布时间",
            "商品",
            "视频vv",
            "视频商品成交件数",
            "商品交易总额（视频） ($)",
            "GPM ($)",
            "归因于可购物视频的商品交易总额 ($)",
            "点击率（视频）",
            "引流率",
            "视频完播率",
            "点击成交转化率（视频）",
        ]
    )
    worksheet.append(
        [
            "https://example.invalid/video/0001",
            "creator",
            "000012345678901234",
            "video",
            "000098765432109876",
            datetime(2025, 5, 20),
            "product",
            100,
            2,
            19.98,
            199.8,
            19.98,
            0.05,
            0,
            0.2,
            0.1,
        ]
    )
    workbook.save(path)


def test_snapshot_and_publish_date_are_separate_and_ids_stay_text(
    tmp_path: Path, monkeypatch
) -> None:
    project = tmp_path / "project"
    source = project / "input" / "workbook" / "fixture.xlsx"
    source.parent.mkdir(parents=True)
    _fixture(source)
    monkeypatch.setattr(ingest, "PROJECT_ROOT", project)
    monkeypatch.setattr(ingest, "STATE_DB", project / "data" / "state" / "state.sqlite3")

    first = ingest.ingest_snapshot(source, "6.17", date(2025, 6, 17))
    second = ingest.ingest_snapshot(source, "6.17", date(2025, 6, 17))

    with Path(first["normalized_csv"]).open(
        "r", encoding="utf-8-sig", newline=""
    ) as handle:
        row = next(csv.DictReader(handle))
    assert row["snapshot_date"] == "2025-06-17"
    assert row["publish_date"] == "2025-05-20"
    assert row["creator_id"] == "000012345678901234"
    assert row["video_id"] == "000098765432109876"
    assert first["source_sha256"] == second["source_sha256"]

