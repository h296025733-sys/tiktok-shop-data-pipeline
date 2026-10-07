from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import typer

from src.config import PROJECT_ROOT, get_settings
from src.doctor import run_doctor
from src.logging_config import configure_logging
from src.tiktok.oauth import exchange_code, refresh_token, token_status


app = typer.Typer(no_args_is_help=True, help="Tech-Wave 安全只读数据工具")
ingest_app = typer.Typer(no_args_is_help=True)
audit_app = typer.Typer(no_args_is_help=True)
auth_app = typer.Typer(no_args_is_help=True)
tiktok_app = typer.Typer(no_args_is_help=True)
app.add_typer(ingest_app, name="ingest")
app.add_typer(audit_app, name="audit")
app.add_typer(auth_app, name="auth")
app.add_typer(tiktok_app, name="tiktok")


def _configure_utf8_stdio() -> None:
    """Keep Windows consoles and redirected automation output Unicode-safe."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")


@app.command()
def doctor() -> None:
    """检查本地配置、安全开关、状态库和外部依赖。"""
    settings = get_settings()
    configure_logging(settings.log_level)
    typer.echo(json.dumps(run_doctor(settings), ensure_ascii=False, indent=2))


@ingest_app.command("workbook-snapshot")
def ingest_workbook_snapshot(
    file: Path = typer.Option(..., exists=True, dir_okay=False),
    sheet: str = typer.Option(...),
    snapshot_date: str = typer.Option(..., help="快照日期，格式 YYYY-MM-DD"),
) -> None:
    """只读导入一个工作簿日期快照。"""
    from src.ingest import ingest_snapshot

    settings = get_settings()
    configure_logging(settings.log_level)
    source_path = file if file.is_absolute() else PROJECT_ROOT / file
    try:
        parsed_snapshot_date = date.fromisoformat(snapshot_date)
    except ValueError as exc:
        raise typer.BadParameter("snapshot-date 必须是 YYYY-MM-DD") from exc
    result = ingest_snapshot(source_path, sheet, parsed_snapshot_date)
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


@audit_app.command("workbook")
def audit_workbook_command(
    file: Path = typer.Option(..., exists=True, dir_okay=False),
) -> None:
    """只读审计工作簿结构、字段、公式、断链与跨表依赖。"""
    from src.audit_workbook import audit_workbook

    settings = get_settings()
    configure_logging(settings.log_level)
    source_path = file if file.is_absolute() else PROJECT_ROOT / file
    result = audit_workbook(source_path)
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


@auth_app.command("exchange")
def auth_exchange(
    code: str = typer.Option(
        None,
        prompt="请输入回调 URL 中的临时 code",
        hide_input=True,
    )
) -> None:
    """使用明确配置的官方 Token endpoint 交换临时授权码。"""
    settings = get_settings()
    configure_logging(settings.log_level)
    exchange_code(settings, code)
    typer.echo("Token 已保存到 Windows Credential Manager。")


@auth_app.command("refresh")
def auth_refresh() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    refresh_token(settings)
    typer.echo("Token 已刷新并保存到 Windows Credential Manager。")


@auth_app.command("status")
def auth_status() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    typer.echo(json.dumps(token_status(settings), ensure_ascii=False, indent=2))


@tiktok_app.command("capabilities")
def tiktok_capabilities() -> None:
    from src.capability_matrix import build_capability_matrix

    path = PROJECT_ROOT / "config" / "datasets" / "tiktok_capabilities.json"
    typer.echo(path.read_text(encoding="utf-8"))
    typer.echo(f"矩阵框架：{build_capability_matrix()}")


@tiktok_app.command("shops")
def tiktok_shops() -> None:
    """查询 Seller 已授权店铺；只调用官方白名单只读 endpoint。"""
    from src.tiktok.client import TikTokAPIError, fetch_authorized_shops

    settings = get_settings()
    configure_logging(settings.log_level)
    try:
        result = fetch_authorized_shops(settings)
    except TikTokAPIError as exc:
        typer.echo(f"查询未执行：{exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


@tiktok_app.command("products")
def tiktok_products(
    page_size: int = typer.Option(100, min=1, max=100),
    max_pages: int = typer.Option(100, min=1, max=100),
    machine_json: bool = typer.Option(False, "--machine-json"),
) -> None:
    """Read the authorized shop product catalog and titles."""
    from src.tiktok.products import ProductCatalogError, fetch_product_catalog

    settings = get_settings()
    configure_logging(settings.log_level)
    try:
        result = fetch_product_catalog(
            settings,
            page_size=page_size,
            max_pages=max_pages,
        )
    except ProductCatalogError as exc:
        payload = {
            "ok": False,
            "dataset": "product_catalog",
            "rows": [],
            "row_count": 0,
            "required_scope": ["seller.product.basic"],
            "errors": [str(exc)],
        }
        typer.echo(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        raise typer.Exit(code=2) from exc
    typer.echo(
        json.dumps(
            result,
            ensure_ascii=False,
            separators=(",", ":") if machine_json else None,
            indent=None if machine_json else 2,
        )
    )


@tiktok_app.command("product")
def tiktok_product(
    product_id: str = typer.Option(...),
    machine_json: bool = typer.Option(False, "--machine-json"),
) -> None:
    """Read one product's official title and details."""
    from src.tiktok.products import ProductCatalogError, fetch_product_detail

    settings = get_settings()
    configure_logging(settings.log_level)
    try:
        result = fetch_product_detail(settings, product_id=product_id)
    except ProductCatalogError as exc:
        typer.echo(json.dumps({
            "ok": False,
            "dataset": "product_detail",
            "product": None,
            "required_scope": ["seller.product.basic"],
            "errors": [str(exc)],
        }, ensure_ascii=False, separators=(",", ":")))
        raise typer.Exit(code=2) from exc
    typer.echo(json.dumps(
        result,
        ensure_ascii=False,
        separators=(",", ":") if machine_json else None,
        indent=None if machine_json else 2,
    ))


@tiktok_app.command("video-performance")
def tiktok_video_performance(
    start_date: str = typer.Option(..., help="查询开始日期，YYYY-MM-DD（含）"),
    end_date_exclusive: str = typer.Option(
        ..., help="查询结束日期，YYYY-MM-DD（不含）"
    ),
    page_size: int = typer.Option(100, min=1, max=100),
    api_version: str = typer.Option("202605", help="官方 API 版本：202605 或 202509"),
    account_type: str = typer.Option(
        "ALL",
        help="账号范围：ALL、OFFICIAL_ACCOUNTS、MARKETING_ACCOUNTS 或 AFFILIATE_ACCOUNTS",
    ),
    max_pages: int = typer.Option(100, min=1, max=100),
    machine_json: bool = typer.Option(
        False,
        "--machine-json",
        help="stdout 只输出一份稳定 JSON 合同；错误合同也写 stdout",
    ),
) -> None:
    """只读查询全部分页店铺视频表现并保存原始 JSON 与规范化 CSV。"""
    from src.tiktok.video_performance import (
        VideoPerformanceError,
        failed_contract,
        fetch_shop_video_performance,
    )

    try:
        settings = get_settings()
        configure_logging(settings.log_level)
        result = fetch_shop_video_performance(
            settings,
            start_date=start_date,
            end_date_exclusive=end_date_exclusive,
            page_size=page_size,
            api_version=api_version,
            account_type=account_type,
            max_pages=max_pages,
        )
    except Exception as exc:
        typer.echo(f"查询未执行：{exc}", err=True)
        if machine_json:
            typer.echo(
                json.dumps(
                    failed_contract(
                        start_date=start_date,
                        end_date_exclusive=end_date_exclusive,
                        error=exc,
                        settings=settings,
                    ),
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
        raise typer.Exit(code=2) from exc
    typer.echo(
        json.dumps(
            result,
            ensure_ascii=False,
            separators=(",", ":") if machine_json else None,
            indent=None if machine_json else 2,
        )
    )


@tiktok_app.command("analytics")
def tiktok_analytics(
    dataset: str = typer.Option(...),
    start_date: str = typer.Option(...),
    end_date_exclusive: str = typer.Option(...),
    resource_id: str = typer.Option(""),
    account_type: str = typer.Option("ALL"),
    today: bool = typer.Option(False, "--today"),
    page_size: int = typer.Option(100, min=1, max=100),
    max_pages: int = typer.Option(100, min=1, max=100),
    machine_json: bool = typer.Option(False, "--machine-json"),
) -> None:
    """调用已由官方文档确认且已进入白名单的其他 Analytics 只读端点。"""
    from src.tiktok.analytics import failed_analytics_contract, fetch_analytics

    try:
        settings = get_settings()
        configure_logging(settings.log_level)
        result = fetch_analytics(
            settings,
            dataset=dataset,
            start_date=start_date,
            end_date_exclusive=end_date_exclusive,
            resource_id=resource_id,
            account_type=account_type,
            today=today,
            page_size=page_size,
            max_pages=max_pages,
        )
    except Exception as exc:
        typer.echo(f"查询未执行：{exc}", err=True)
        if machine_json:
            typer.echo(
                json.dumps(
                    failed_analytics_contract(
                        dataset=dataset,
                        start_date=start_date,
                        end_date_exclusive=end_date_exclusive,
                        error=exc,
                        settings=settings,
                    ),
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
        raise typer.Exit(code=2) from exc
    typer.echo(
        json.dumps(
            result,
            ensure_ascii=False,
            separators=(",", ":") if machine_json else None,
            indent=None if machine_json else 2,
        )
    )


@tiktok_app.command("raw-integrity")
def tiktok_raw_integrity() -> None:
    """验证全部 TikTok 原始 JSON 的 SHA256，并只补建缺失旁车文件。"""
    from src.tiktok.raw_integrity import verify_and_backfill_raw_hashes

    result = verify_and_backfill_raw_hashes()
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))
    if not result["ok"]:
        raise typer.Exit(code=2)


@tiktok_app.command("order-attribution")
def tiktok_order_attribution(
    start_date: str = typer.Option(..., help="业务时区开始日期，YYYY-MM-DD（含）"),
    end_date_exclusive: str = typer.Option(..., help="业务时区结束日期，YYYY-MM-DD（不含）"),
    time_zone: str = typer.Option("Asia/Shanghai", "--time-zone", help="投产比业务日期时区"),
    target_collaboration_is_self_operated: bool = typer.Option(
        False,
        "--target-collaboration-is-self-operated",
        help="已用真实Seller Center标签确认target collaboration等于商家签约/自营达人",
    ),
    page_size: int = typer.Option(100, min=1, max=100),
    max_pages: int = typer.Option(100, min=1, max=100),
    machine_json: bool = typer.Option(False, "--machine-json"),
) -> None:
    """按付款北京时间聚合订单，并在权限齐全时用联盟订单标签精确拆分渠道。"""
    from src.tiktok.order_attribution import (
        failed_order_attribution_contract,
        fetch_order_attribution,
    )

    settings = get_settings()
    configure_logging(settings.log_level)
    try:
        result = fetch_order_attribution(
            settings,
            start_date=start_date,
            end_date_exclusive=end_date_exclusive,
            time_zone=time_zone,
            target_collaboration_is_self_operated=target_collaboration_is_self_operated,
            page_size=page_size,
            max_pages=max_pages,
        )
    except Exception as exc:
        result = failed_order_attribution_contract(
            start_date=start_date,
            end_date_exclusive=end_date_exclusive,
            error=exc,
            settings=settings,
        )
        typer.echo(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        raise typer.Exit(code=2) from exc
    typer.echo(json.dumps(
        result,
        ensure_ascii=False,
        separators=(",", ":") if machine_json else None,
        indent=None if machine_json else 2,
    ))


@tiktok_app.command("paid-order-snapshot")
def tiktok_paid_order_snapshot(
    start_date: str = typer.Option(..., help="业务时区开始日期，YYYY-MM-DD（含）"),
    end_date_exclusive: str = typer.Option(..., help="业务时区结束日期，YYYY-MM-DD（不含）"),
    time_zone: str = typer.Option("Asia/Shanghai", "--time-zone", help="付款归日时区"),
    page_size: int = typer.Option(100, min=1, max=100),
    max_pages: int = typer.Option(100, min=1, max=100),
    machine_json: bool = typer.Option(False, "--machine-json"),
) -> None:
    """轻量查询已付款订单；有Affiliate权限时同时返回精确video_id归因。"""
    from src.tiktok.order_attribution import (
        failed_paid_order_snapshot_contract,
        fetch_paid_order_snapshot,
    )

    settings = get_settings()
    configure_logging(settings.log_level)
    try:
        result = fetch_paid_order_snapshot(
            settings,
            start_date=start_date,
            end_date_exclusive=end_date_exclusive,
            time_zone=time_zone,
            page_size=page_size,
            max_pages=max_pages,
        )
    except Exception as exc:
        result = failed_paid_order_snapshot_contract(
            start_date=start_date,
            end_date_exclusive=end_date_exclusive,
            error=exc,
            settings=settings,
        )
        typer.echo(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        raise typer.Exit(code=2) from exc
    typer.echo(json.dumps(
        result,
        ensure_ascii=False,
        separators=(",", ":") if machine_json else None,
        indent=None if machine_json else 2,
    ))


@tiktok_app.command("paid-order-report-snapshot")
def tiktok_paid_order_report_snapshot(
    start_date: str = typer.Option(..., help="北京时间报告周期开始日期，YYYY-MM-DD（含）"),
    end_date_exclusive: str = typer.Option(..., help="北京时间报告周期结束日期，YYYY-MM-DD（不含）"),
    time_zone: str = typer.Option("Asia/Shanghai", "--time-zone", help="付款归日时区"),
    page_size: int = typer.Option(100, min=1, max=100),
    max_pages: int = typer.Option(100, min=1, max=100),
    machine_json: bool = typer.Option(False, "--machine-json"),
) -> None:
    """只读查询最长31天付款快照，供完整周报/月报补齐核心经营值。"""
    from src.tiktok.order_attribution import (
        failed_paid_order_snapshot_contract,
        fetch_paid_order_snapshot,
    )

    settings = get_settings()
    configure_logging(settings.log_level)
    try:
        result = fetch_paid_order_snapshot(
            settings,
            start_date=start_date,
            end_date_exclusive=end_date_exclusive,
            time_zone=time_zone,
            page_size=page_size,
            max_pages=max_pages,
            max_requested_days=31,
        )
    except Exception as exc:
        result = failed_paid_order_snapshot_contract(
            start_date=start_date,
            end_date_exclusive=end_date_exclusive,
            error=exc,
            settings=settings,
        )
        typer.echo(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        raise typer.Exit(code=2) from exc
    typer.echo(json.dumps(
        result,
        ensure_ascii=False,
        separators=(",", ":") if machine_json else None,
        indent=None if machine_json else 2,
    ))


if __name__ == "__main__":
    _configure_utf8_stdio()
    app()
