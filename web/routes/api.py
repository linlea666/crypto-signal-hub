"""JSON API 路由：供前端 AJAX 和外部调用"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)
router = APIRouter()

LOG_DIR = Path(__file__).parent.parent.parent / "data" / "logs"

# 实时 ticker 缓存（30 秒有效期，避免交易所限流）
_ticker_cache: dict[str, dict] = {}
_ticker_cache_ts: dict[str, float] = {}
_TICKER_TTL = 30


@router.get("/status")
async def system_status(request: Request):
    """系统状态：各采集器健康、邮件计数等"""
    registry = request.app.state.collector_registry
    db = request.app.state.db
    health = request.app.state.health_checker
    return {
        "collectors": registry.status,
        "emails_today": db.count_emails_today(),
        "health": _serialize_health(health.last_report) if health and health.last_report else None,
    }


@router.get("/health")
async def health_check(request: Request, refresh: bool = False):
    """获取或刷新服务健康状态"""
    checker = request.app.state.health_checker
    if not checker:
        return JSONResponse({"error": "健康检查未启用"}, status_code=501)
    if refresh:
        report = await checker.check_all()
    else:
        report = checker.last_report
    if not report:
        report = await checker.check_all()
    return _serialize_health(report)


@router.get("/logs")
async def get_logs(lines: int = 200):
    """返回最近 N 行日志"""
    log_file = LOG_DIR / "app.log"
    if not log_file.exists():
        return {"lines": [], "total": 0}
    try:
        tail: deque[str] = deque(maxlen=lines)
        with open(log_file, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                tail.append(line.rstrip("\n"))
        return {"lines": list(tail), "total": len(tail)}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


def _serialize_health(report) -> dict:
    """将 HealthReport 序列化为 JSON 友好结构"""
    return {
        "overall": report.overall.value,
        "checked_at": report.checked_at,
        "ok_count": report.ok_count,
        "total_count": report.total_count,
        "probes": [
            {
                "name": p.name,
                "status": p.status.value,
                "latency_ms": p.latency_ms,
                "message": p.message,
                "last_check": p.last_check,
            }
            for p in report.probes
        ],
    }


@router.get("/latest")
async def all_latest_reports(request: Request):
    """获取所有交易对的最新报告"""
    scheduler = request.app.state.job_scheduler
    return scheduler.latest_reports


@router.get("/latest/{symbol}")
async def latest_report(request: Request, symbol: str):
    """获取指定交易对的最新报告"""
    scheduler = request.app.state.job_scheduler
    report = scheduler.latest_reports.get(symbol)
    if not report:
        db = request.app.state.db
        report = db.get_latest_report(symbol)
    if not report:
        return JSONResponse({"error": "暂无数据"}, status_code=404)
    return report


@router.get("/reports")
async def list_reports(request: Request, limit: int = 50, offset: int = 0, symbol: str = ""):
    """分页获取历史报告，支持按交易对筛选"""
    db = request.app.state.db
    return db.get_recent_reports(limit=limit, offset=offset, symbol=symbol or None)


@router.get("/report/{report_id}")
async def report_detail(request: Request, report_id: str):
    """获取单份报告详情"""
    db = request.app.state.db
    detail = db.get_report_detail(report_id)
    if not detail:
        return JSONResponse({"error": "报告不存在"}, status_code=404)
    return detail


@router.post("/analyze")
async def trigger_analysis(request: Request):
    """手动触发一次分析，支持指定交易对"""
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    scheduler = request.app.state.job_scheduler
    config = request.app.state.config_manager.config
    symbol = body.get("symbol") or (
        config.general.symbols[0] if config.general.symbols else "BTC/USDT"
    )
    result = await scheduler.run_now(symbol)
    if result:
        return {"success": True, "report": result}
    return JSONResponse({"success": False, "error": "分析失败"}, status_code=500)


@router.post("/config")
async def save_config(request: Request):
    """保存配置"""
    body = await request.json()
    cm = request.app.state.config_manager
    try:
        cm.update(**body)
        _reload_all_services(request.app)
        # 异步刷新健康检查，让大屏即时反映新配置状态
        checker = request.app.state.health_checker
        if checker:
            asyncio.ensure_future(checker.check_all())
        return {"success": True}
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=400)


@router.post("/setup/complete")
async def complete_setup(request: Request):
    """完成首次引导"""
    body = await request.json()
    cm = request.app.state.config_manager
    try:
        body["setup_completed"] = True
        cm.update(**body)
        _reload_all_services(request.app)
        return {"success": True}
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=400)


def _reload_all_services(app) -> None:
    """配置变更后，将新配置传播到所有持有配置引用的服务"""
    config = app.state.config_manager.config

    scheduler = app.state.job_scheduler
    if scheduler:
        scheduler.reload_config(config)

    # NOFX 采集器动态启停
    registry = app.state.collector_registry
    if registry:
        nofx_want = config.nofx.enabled and bool(config.nofx.api_key)
        nofx_has = registry.has("nofx")
        if nofx_want and not nofx_has:
            from collectors.nofx import NofxCollector
            registry.register(NofxCollector(config.nofx))
        elif not nofx_want and nofx_has:
            registry.unregister("nofx")

    health_checker = app.state.health_checker
    if health_checker:
        health_checker.update_config(
            exchange_config=config.exchanges,
            email_config=config.email,
            ai_config=config.ai,
            nofx_config=config.nofx,
        )


@router.post("/test/email")
async def test_email(request: Request):
    """测试邮件发送"""
    from config.schema import EmailConfig
    from notifier.email_sender import EmailNotifier

    body = await request.json()
    try:
        email_config = EmailConfig(**body)
        notifier = EmailNotifier(email_config)
        success = await notifier.send_test()
        return {"success": success}
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=400)


@router.post("/test/ai")
async def test_ai(request: Request):
    """测试 AI 连接"""
    from analyzer.reporter import AIReporter
    from config.schema import AIConfig

    body = await request.json()
    try:
        ai_config = AIConfig(**body)
        reporter = AIReporter(ai_config)
        success = await reporter.test_connection()
        return {"success": success}
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=400)


@router.get("/stats")
async def signal_stats(request: Request, days: int = 7):
    """信号统计数据"""
    db = request.app.state.db
    return db.get_signal_accuracy_stats(days=days)


@router.get("/stats/backtest")
async def backtest_stats(request: Request, days: int = 7, symbol: str = ""):
    """多窗口回测统计"""
    db = request.app.state.db
    return db.get_backtest_stats(days=days, symbol=symbol or "")


@router.get("/executor/status")
async def executor_status(request: Request):
    """执行层状态"""
    scheduler = request.app.state.job_scheduler
    executor = scheduler.executor if scheduler else None
    if not executor:
        return {"enabled": False, "initialized": False}
    status = executor.get_status()
    balance = await executor.client.get_balance() if executor.client.is_connected else {}
    status["balance"] = balance
    return status


@router.get("/executor/orders")
async def executor_orders(request: Request):
    """活跃订单列表"""
    scheduler = request.app.state.job_scheduler
    executor = scheduler.executor if scheduler else None
    if not executor:
        return []
    return executor.tracker.get_active_orders()


@router.get("/executor/history")
async def executor_history(request: Request, limit: int = 50, offset: int = 0):
    """历史订单"""
    scheduler = request.app.state.job_scheduler
    executor = scheduler.executor if scheduler else None
    if not executor:
        return []
    return executor.tracker.get_history(limit=limit, offset=offset)


@router.get("/executor/stats")
async def executor_stats(request: Request, days: int = 30):
    """执行统计"""
    scheduler = request.app.state.job_scheduler
    executor = scheduler.executor if scheduler else None
    if not executor:
        return {"overall": {}, "daily": []}
    return {
        "overall": executor.tracker.get_overall_stats(),
        "daily": executor.tracker.get_daily_stats(days=days),
    }


@router.post("/executor/close")
async def executor_close_position(request: Request):
    """手动平仓"""
    scheduler = request.app.state.job_scheduler
    executor = scheduler.executor if scheduler else None
    if not executor or not executor.client.is_connected:
        return JSONResponse({"error": "执行层未启用"}, status_code=400)
    body = await request.json()
    symbol = body.get("symbol", "")
    side = body.get("side", "")
    order_id = body.get("order_id", "")
    if not symbol or not side:
        return JSONResponse({"error": "需要 symbol 和 side"}, status_code=400)
    ok = await executor.client.close_position(symbol, side)
    if ok and order_id:
        from executor.models import OrderStatus
        from core.time_utils import now_beijing
        executor.tracker.update_status(
            order_id, OrderStatus.CLOSED_MANUAL, closed_at=now_beijing().isoformat(),
        )
    return {"success": ok}


@router.post("/executor/cancel")
async def executor_cancel_order(request: Request):
    """取消待触发订单"""
    scheduler = request.app.state.job_scheduler
    executor = scheduler.executor if scheduler else None
    if not executor:
        return JSONResponse({"error": "执行层未启用"}, status_code=400)
    body = await request.json()
    order_id = body.get("order_id", "")
    if not order_id:
        return JSONResponse({"error": "需要 order_id"}, status_code=400)
    from executor.models import OrderStatus
    executor.tracker.update_status(order_id, OrderStatus.CANCELLED)
    return {"success": True}


@router.get("/executor/pnl-curve")
async def executor_pnl_curve(request: Request, days: int = 30):
    """累计盈亏曲线数据"""
    scheduler = request.app.state.job_scheduler
    executor = scheduler.executor if scheduler else None
    if not executor:
        return {"dates": [], "cumulative_pnl": []}
    daily = executor.tracker.get_daily_stats(days=days)
    daily.sort(key=lambda x: x["date"])
    dates = []
    cum_pnl = []
    running = 0.0
    for d in daily:
        running += d.get("total_pnl_usd", 0)
        dates.append(d["date"])
        cum_pnl.append(round(running, 2))
    return {"dates": dates, "cumulative_pnl": cum_pnl}


@router.get("/executor/export")
async def executor_export(request: Request):
    """导出交易日志 CSV"""
    scheduler = request.app.state.job_scheduler
    executor = scheduler.executor if scheduler else None
    if not executor:
        return JSONResponse({"error": "执行层未启用"}, status_code=400)

    rows = executor.export_trade_log(limit=500)
    if not rows:
        return JSONResponse({"error": "暂无数据"}, status_code=404)

    import csv
    import io
    from fastapi.responses import StreamingResponse

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=rows[0].keys())
    writer.writeheader()
    writer.writerows(rows)
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=executor_trades.csv"},
    )


@router.post("/executor/test-connection")
async def executor_test_connection(request: Request):
    """测试执行层交易所连接"""
    from config.schema import ExecutorConfig
    from executor.exchange_client import ExchangeClient

    body = await request.json()
    try:
        config = ExecutorConfig(**body)
        client = ExchangeClient(config)
        await client.initialize()
        result = await client.test_connection()
        await client.cleanup()
        return result
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@router.get("/export/signals")
async def export_signals(request: Request, days: int = 7, fmt: str = "json"):
    """导出信号报告（含评分、关键位、AI 分析）"""
    db = request.app.state.db
    reports = db.get_recent_reports(limit=500)
    if fmt == "csv":
        import csv, io
        from fastapi.responses import StreamingResponse
        rows = []
        for r in reports:
            rows.append({
                "id": r.get("id", ""),
                "timestamp": r.get("timestamp", ""),
                "symbol": r.get("symbol", ""),
                "direction": r.get("direction", ""),
                "confidence": r.get("confidence", 0),
                "signal_strength": r.get("signal_strength", ""),
                "total_score": r.get("total_score", 0),
                "max_score": r.get("max_score", 0),
                "alert_type": r.get("alert_type", ""),
            })
        if not rows:
            return JSONResponse({"error": "暂无数据"}, status_code=404)
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
        output.seek(0)
        return StreamingResponse(
            iter([output.getvalue()]), media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=signals.csv"},
        )
    return reports


@router.get("/export/trades")
async def export_trades(request: Request, fmt: str = "json"):
    """导出执行记录"""
    scheduler = request.app.state.job_scheduler
    executor = scheduler.executor if scheduler else None
    if not executor:
        return JSONResponse({"error": "执行层未启用"}, status_code=400)
    rows = executor.export_trade_log(limit=500)
    if fmt == "csv" and rows:
        import csv, io
        from fastapi.responses import StreamingResponse
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
        output.seek(0)
        return StreamingResponse(
            iter([output.getvalue()]), media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=trades.csv"},
        )
    return rows


@router.get("/export/backtest")
async def export_backtest(request: Request, days: int = 7):
    """导出回测结果统计"""
    db = request.app.state.db
    return db.get_backtest_stats(days=days)


@router.get("/export/full")
async def export_full(request: Request, days: int = 7):
    """合并导出：信号 + 执行 + 回测"""
    db = request.app.state.db
    scheduler = request.app.state.job_scheduler
    executor = scheduler.executor if scheduler else None

    signals = db.get_recent_reports(limit=200)
    backtest = db.get_backtest_stats(days=days)
    trades = executor.export_trade_log(limit=200) if executor else []

    full_reports = []
    for sig in signals[:50]:
        detail = db.get_report_detail(sig["id"])
        if detail:
            full_reports.append(detail)

    import json
    from fastapi.responses import StreamingResponse
    payload = json.dumps({
        "exported_at": datetime.now().isoformat(),
        "signals_summary": signals,
        "signals_full": full_reports,
        "trades": trades,
        "backtest": backtest,
    }, ensure_ascii=False, indent=2)
    return StreamingResponse(
        iter([payload]), media_type="application/json",
        headers={"Content-Disposition": "attachment; filename=csh_export.json"},
    )


@router.get("/executor/log")
async def executor_log(request: Request, lines: int = 50):
    """获取执行层相关日志"""
    log_file = LOG_DIR / "app.log"
    if not log_file.exists():
        return {"lines": []}
    try:
        tail: deque[str] = deque(maxlen=lines * 3)
        with open(log_file, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                tail.append(line.rstrip("\n"))
        keywords = ("执行", "executor", "风控", "下单", "平仓", "止损", "移动",
                     "触发", "开仓", "资金分配", "注册", "策略")
        filtered = [l for l in tail if any(k in l.lower() for k in keywords)]
        return {"lines": filtered[-lines:]}
    except Exception as e:
        logger.warning("读取执行日志失败: %s", e)
        return {"lines": []}


@router.get("/ticker/{symbol:path}")
async def realtime_ticker(symbol: str):
    """实时价格 ticker（30 秒缓存，用于前端轮询）"""
    import ccxt.async_support as ccxt

    now = time.monotonic()
    cached = _ticker_cache.get(symbol)
    if cached and (now - _ticker_cache_ts.get(symbol, 0)) < _TICKER_TTL:
        return cached

    try:
        ex = ccxt.okx({"enableRateLimit": True, "timeout": 8000})
        try:
            t = await ex.fetch_ticker(symbol)
        finally:
            await ex.close()

        result = {
            "symbol": symbol,
            "price": t.get("last"),
            "change_pct": t.get("percentage"),
            "high_24h": t.get("high"),
            "low_24h": t.get("low"),
            "volume_24h": t.get("quoteVolume"),
            "timestamp": t.get("datetime", ""),
        }
        _ticker_cache[symbol] = result
        _ticker_cache_ts[symbol] = now
        return result
    except Exception as e:
        logger.warning("Ticker %s 获取失败: %s", symbol, e)
        if cached:
            return cached
        return JSONResponse({"error": str(e)}, status_code=502)
