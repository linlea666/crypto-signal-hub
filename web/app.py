"""FastAPI 应用工厂。

创建并配置 Web 应用，挂载路由、静态文件和模板。
应用实例持有对核心服务的引用（通过 app.state），
路由处理函数通过 request.app.state 访问服务。
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from core.constants import APP_NAME, VERSION
from web.routes.api import router as api_router
from web.routes.pages import router as pages_router

if TYPE_CHECKING:
    from config.manager import ConfigManager
    from core.health import HealthChecker
    from scheduler.jobs import JobScheduler
    from storage.database import Database
    from collectors.registry import CollectorRegistry

logger = logging.getLogger(__name__)

WEB_DIR = Path(__file__).parent
TEMPLATES_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.globals["ASSET_VERSION"] = VERSION


def create_app(
    config_manager: "ConfigManager",
    db: "Database",
    job_scheduler: "JobScheduler",
    collector_registry: "CollectorRegistry",
    health_checker: "HealthChecker | None" = None,
) -> FastAPI:
    """创建并配置 FastAPI 应用实例"""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logger.info("正在初始化采集器...")
        await collector_registry.initialize_all()
        logger.info("正在配置调度任务...")
        job_scheduler.setup()
        job_scheduler.start()
        if health_checker:
            logger.info("执行首次健康检查...")
            await health_checker.check_all()
        logger.info("✅ 系统启动完成")
        yield
        await job_scheduler.stop()
        await collector_registry.cleanup_all()
        logger.info("系统已安全关闭")

    app = FastAPI(
        title=APP_NAME,
        version=VERSION,
        docs_url="/api/docs",
        lifespan=lifespan,
    )

    app.state.config_manager = config_manager
    app.state.db = db
    app.state.job_scheduler = job_scheduler
    app.state.collector_registry = collector_registry
    app.state.health_checker = health_checker
    app.state.templates = templates

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    app.include_router(pages_router)
    app.include_router(api_router, prefix="/api")

    return app
