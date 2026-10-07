from __future__ import annotations

import json

from src.tiktok.raw_integrity import verify_and_backfill_raw_hashes


def test_raw_integrity_backfills_and_detects_mismatch(tmp_path) -> None:
    raw = tmp_path / "request.json"
    raw.write_text(json.dumps({"code": 0}), encoding="utf-8")
    first = verify_and_backfill_raw_hashes(tmp_path)
    assert first["ok"] is True
    assert first["created"] == 1
    second = verify_and_backfill_raw_hashes(tmp_path)
    assert second["ok"] is True
    assert second["created"] == 0
    raw.write_text(json.dumps({"code": 1}), encoding="utf-8")
    third = verify_and_backfill_raw_hashes(tmp_path)
    assert third["ok"] is False
    assert third["errors"]
