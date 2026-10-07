from __future__ import annotations

import json
from pathlib import Path

from src.config import PROJECT_ROOT
from src.xlsx_utils import write_rows_to_workbook


MATRIX_FIELDS = [
    "业务模块",
    "目标字段",
    "官方接口名称",
    "官方 endpoint",
    "所需 scope",
    "官方文档链接",
    "当前应用是否配置",
    "是否真实测试",
    "测试结果",
    "目标字段是否返回",
    "时间窗口",
    "分页",
    "限流",
    "数据延迟",
    "数据口径",
    "是否含 PII",
    "是否能生成文件",
    "是否需要浏览器补充",
    "状态",
    "备注",
]

VIDEO_REPORT_FIELDS = [
    "视频链接",
    "达人昵称",
    "达人ID",
    "视频内容",
    "视频ID",
    "发布时间",
    "商品",
    "视频VV",
    "成交件数",
    "视频GMV",
    "GPM",
    "可购物视频归因GMV",
    "视频点击率",
    "引流率",
    "完播率",
    "点击成交转化率",
]


def build_capability_matrix() -> Path:
    config_path = (
        PROJECT_ROOT / "config" / "datasets" / "tiktok_capabilities.json"
    )
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    rows: list[list[object]] = []

    for item in payload["capabilities"]:
        is_authorized_shops = item["business_module"] == "授权店铺"
        is_video_product = item["business_module"] == "视频商品表现"
        is_real_tested = item["status"] == "已由真实店铺测试成功"
        rows.append(
            [
                item["business_module"],
                "店铺 ID、名称、区域、seller_type、shop_cipher、店铺代码"
                if is_authorized_shops
                else "视频ID、商品ID、商品名、成交件数、GMV、日均买家"
                if is_video_product
                else "",
                "Get Authorized Shops" if is_authorized_shops else "Get Shop Video Product Performance List"
                if is_video_product else "",
                "/authorization/202309/shops" if is_authorized_shops else "/analytics/202409/shop_videos/{video_id}/products/performance"
                if is_video_product else "",
                "seller.authorization.info 或 test.scope.public"
                if is_authorized_shops
                else "data.shop_analytics.public.read" if is_video_product else "",
                "https://partner.tiktokshop.com/docv2/page/get-authorized-shops-202309"
                if is_authorized_shops
                else "https://partner.tiktokshop.com/docv2/page/get-shop-video-product-performance-list-202409"
                if is_video_product else "",
                "是" if is_real_tested else "尚未核对",
                "是" if is_real_tested else "否",
                "成功" if is_real_tested else "未测试",
                "官方文档确认返回" if is_authorized_shops else "尚未验证",
                "店铺注册时区自然日" if is_video_product else "",
                "无分页参数（官方参考页）" if is_authorized_shops else "page_token，最多100条/页"
                if is_video_product else "",
                "",
                "T-1" if is_video_product else "",
                "Seller 级授权店铺列表" if is_authorized_shops else "指定视频下按商品拆分的归因表现"
                if is_video_product else "",
                "否" if is_authorized_shops else "待确认",
                "是" if is_authorized_shops else "否",
                "否" if is_authorized_shops else "待确认",
                item["status"],
                (
                    "endpoint/scope 已由官方确认，且当前应用已完成真实店铺只读调用。"
                    if is_real_tested
                    else "endpoint/scope 已由官方确认；当前应用 scope 与真实店铺仍未测试。"
                    if is_authorized_shops
                    else "框架占位；不得据此认定当前应用可用。"
                ),
            ]
        )

    for field in VIDEO_REPORT_FIELDS:
        rows.append(
            [
                "视频表现",
                field,
                "",
                "",
                "",
                "",
                "尚未核对",
                "否",
                "未测试",
                "尚未验证",
                "",
                "",
                "",
                "",
                "",
                "否",
                "否",
                "待确认",
                "尚未验证",
                "需按官方文档、当前应用 scope 和真实店铺最小只读测试逐字段确认。",
            ]
        )

    output_path = (
        PROJECT_ROOT / "exports" / "reports" / "tiktok_api_capability_matrix.xlsx"
    )
    write_rows_to_workbook(
        output_path,
        sheet_name="API能力矩阵",
        headers=MATRIX_FIELDS,
        rows=rows,
        table_name="TikTokCapabilityMatrix",
    )
    return output_path
