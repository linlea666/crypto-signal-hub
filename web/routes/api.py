"""JSON API 路由：供前端 AJAX 和外部调用"""

from __future__ import annotations

import os
from collections import deque
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter()

LOG_DIR = Path(__file__).parent.parent.parent / "data" / "logs"


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
async def list_reports(request: Request, limit: int = 50, offset: int = 0):
    """分页获取历史报告"""
    db = request.app.state.db
    return db.get_recent_reports(limit=limit, offset=offset)


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
    """手动触发一次分析"""
    scheduler = request.app.state.job_scheduler
    config = request.app.state.config_manager.config
    symbol = config.general.symbols[0] if config.general.symbols else "BTC/USDT"
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

    health_checker = app.state.health_checker
    if health_checker:
        health_checker.update_config(
            exchange_config=config.exchanges,
            email_config=config.email,
            ai_config=config.ai,
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
