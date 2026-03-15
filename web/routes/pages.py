"""页面路由：大屏、配置、引导、历史、日志等 HTML 页面"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    """数据大屏首页"""
    cm = request.app.state.config_manager
    if cm.is_first_run:
        return RedirectResponse(url="/setup")

    db = request.app.state.db
    scheduler = request.app.state.job_scheduler
    config = cm.config

    symbols = config.general.symbols or ["BTC/USDT"]
    active_symbol = request.query_params.get("symbol", symbols[0])
    if active_symbol not in symbols:
        active_symbol = symbols[0]

    latest = scheduler.latest_reports
    report = latest.get(active_symbol) if latest else None
    reports_list = db.get_recent_reports(limit=10)
    collector_status = request.app.state.collector_registry.status
    emails_today = db.count_emails_today()

    health_checker = request.app.state.health_checker
    health_report = health_checker.last_report if health_checker else None

    return request.app.state.templates.TemplateResponse("dashboard.html", {
        "request": request,
        "config": config,
        "symbols": symbols,
        "active_symbol": active_symbol,
        "report": report,
        "reports_list": reports_list,
        "collector_status": collector_status,
        "emails_today": emails_today,
        "health": health_report,
    })


@router.get("/config", response_class=HTMLResponse)
async def config_page(request: Request):
    """参数配置页"""
    cm = request.app.state.config_manager
    return request.app.state.templates.TemplateResponse("config.html", {
        "request": request,
        "config": cm.config,
    })


@router.get("/setup", response_class=HTMLResponse)
async def setup_page(request: Request):
    """首次引导配置页"""
    return request.app.state.templates.TemplateResponse("setup.html", {
        "request": request,
    })


@router.get("/history", response_class=HTMLResponse)
async def history_page(request: Request):
    """历史信号列表页"""
    db = request.app.state.db
    reports = db.get_recent_reports(limit=100)
    stats = db.get_signal_accuracy_stats()
    return request.app.state.templates.TemplateResponse("history.html", {
        "request": request,
        "reports": reports,
        "stats": stats,
    })


@router.get("/logs", response_class=HTMLResponse)
async def logs_page(request: Request):
    """运行日志页"""
    return request.app.state.templates.TemplateResponse("logs.html", {
        "request": request,
    })
