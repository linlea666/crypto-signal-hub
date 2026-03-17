"""页面路由：大屏、配置、引导、历史、日志等 HTML 页面"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    """数据大屏首页——始终渲染所有面板，无数据时显示占位"""
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

    # 优先取内存缓存，fallback 到 DB 持久化数据
    report = None
    latest = scheduler.latest_reports
    if latest:
        report = latest.get(active_symbol)
    if not report:
        report = db.get_latest_report(active_symbol)

    reports_list = db.get_recent_reports(limit=10)
    emails_today = db.count_emails_today()
    stats = db.get_signal_accuracy_stats(days=7)
    backtest = db.get_backtest_stats(days=7)

    health_checker = request.app.state.health_checker
    health_report = health_checker.last_report if health_checker else None

    return request.app.state.templates.TemplateResponse("dashboard.html", {
        "request": request,
        "config": config,
        "symbols": symbols,
        "active_symbol": active_symbol,
        "report": report,
        "reports_list": reports_list,
        "emails_today": emails_today,
        "health": health_report,
        "stats": stats,
        "backtest": backtest,
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


@router.get("/executor", response_class=HTMLResponse)
async def executor_page(request: Request):
    """执行面板页"""
    scheduler = request.app.state.job_scheduler
    executor = scheduler.executor if scheduler else None
    config = request.app.state.config_manager.config

    status = executor.get_status() if executor else {"enabled": False}
    active_orders = executor.tracker.get_active_orders() if executor else []
    history = executor.tracker.get_history(limit=20) if executor else []
    balance = {}
    positions = []
    if executor and executor.client.is_connected:
        try:
            balance = await executor.client.get_balance()
            positions = await executor.client.get_positions()
        except Exception:
            pass

    tracked_keys = set()
    for o in active_orders:
        if o.get("status") == "open":
            sym = o["symbol"].replace("/USDT", "/USDT:USDT")
            side = "long" if o.get("side") == "buy" else "short"
            tracked_keys.add((sym, side))

    for p in positions:
        key = (p["symbol"], p["side"])
        p["tracked"] = key in tracked_keys

    return request.app.state.templates.TemplateResponse("executor.html", {
        "request": request,
        "config": config,
        "status": status,
        "active_orders": active_orders,
        "history": history,
        "balance": balance,
        "positions": positions,
    })
