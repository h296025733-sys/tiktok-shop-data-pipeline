from unittest.mock import patch

from src.config import Settings
from src.tiktok.shop_risks import collect_shop_risks


def test_risk_snapshot_strips_order_pii_and_records_partial_sources(tmp_path):
    settings = Settings(tts_app_key="test", tts_app_secret="test")
    with patch("src.tiktok.shop_risks.PROJECT_ROOT", tmp_path), \
         patch("src.tiktok.shop_risks._latest_shop_context", return_value={"shop_id":"1", "shop_name":"A", "shop_cipher":"hidden"}), \
         patch("src.tiktok.shop_risks.granted_scopes", return_value=["seller.product.basic", "seller.order.info", "data.shop_analytics.public.read"]), \
         patch("src.tiktok.shop_risks.fetch_product_catalog", return_value={"rows":[], "raw_source_paths":[], "pagination_truncated":True}), \
         patch("src.tiktok.shop_risks._post_pages", return_value=[{"request_id":"r", "data":{"total_count":1,"orders":[{"id":"2","status":"AWAITING_COLLECTION","tts_sla_time":100,"buyer_email":"private","recipient_address":{"phone":"secret"}}]}}]), \
         patch("src.tiktok.shop_risks._sps", side_effect=RuntimeError("permission")):
        result = collect_shop_risks(settings)
    assert result["sources"]["products"]["ok"] is False
    assert result["sources"]["orders"]["ok"] is True
    assert result["sources"]["orders"]["rows"] == [{"id":"2","status":"AWAITING_COLLECTION","tts_sla_time":100}]
    assert result["sources"]["sps_overview"]["ok"] is False
    assert "private" not in str(result)
    assert "secret" not in str(result)
    assert result["evidenceSha256"]
