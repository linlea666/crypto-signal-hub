"""CryptoSignal Hub 主入口。

负责：
1. 初始化日志（带轮转）
2. 加载配置
3. 组装所有服务（依赖注入）
4. 启动 Web 服务器和定时调度器

启动方式：python main.py
Docker 方式：见 Dockerfile / docker-compose.yml
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
import webbrowser
from pathlib import Path

import uvicorn

from config.manager import ConfigManager
from core.constants import APP_NAME, DEFAULT_PORT, VERSION

# 确保项目根目录在 Python 路径中
PROJECT_ROOT = Path(__file__).parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

LOG_DIR = PROJECT_ROOT / "data" / "logs"
LOG_FILE = LOG_DIR / "app.log"


def setup_logging() -> None:
    """配置全局日志，含日志轮转（单文件 5MB、保留 10 份）"""
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    file_handler = logging.handlers.RotatingFileHandler(
        LOG_FILE,
        maxBytes=5 * 1024 * 1024,
        backupCount=10,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.INFO)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)

    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_handler.setFormatter(formatter)
    console_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(file_handler)
    root.addHandler(console_handler)

    # 降低第三方库的日志噪音
    for lib in ("httpx", "ccxt", "yfinance", "apscheduler", "urllib3"):
        logging.getLogger(lib).setLevel(logging.WARNING)


def build_services(config_manager: ConfigManager) -> "FastAPI":
    """组装所有服务，建立依赖关系并返回 FastAPI 应用实例。

    这是整个系统的依赖注入入口。
    所有模块的依赖都在此显式声明，不使用全局状态。
    启动/关闭流程由 FastAPI lifespan 管理（见 web/app.py）。
    """
    config = config_manager.config

    # ── 1. 存储层 ──
    from storage.database import Database
    db = Database()

    # ── 2. 数据采集层 ──
    from collectors.registry import CollectorRegistry
    from collectors.exchange import ExchangeCollector
    from collectors.macro import MacroCollector
    from collectors.options import OptionsCollector
    from collectors.calendar import CalendarCollector

    collector_registry = CollectorRegistry()
    collector_registry.register(ExchangeCollector(config.exchanges))
    collector_registry.register(MacroCollector())
    collector_registry.register(OptionsCollector(config.exchanges))
    collector_registry.register(CalendarCollector())

    if config.nofx.enabled and config.nofx.api_key:
        from collectors.nofx import NofxCollector
        collector_registry.register(NofxCollector(config.nofx))

    # ── 3. 评分引擎 ──
    from engine.scorer import SignalScorer
    from engine.factors.technical import TechnicalFactor
    from engine.factors.funding_rate import FundingRateFactor
    from engine.factors.open_interest import OpenInterestFactor
    from engine.factors.long_short import LongShortFactor
    from engine.factors.options_factor import OptionsFactor
    from engine.factors.macro import MacroFactor
    from engine.factors.sentiment import SentimentFactor
    from engine.factors.nofx_signal import NofxSignalFactor

    scorer = SignalScorer(config.scoring)
    factor_classes = [
        (TechnicalFactor, config.scoring.technical),
        (FundingRateFactor, config.scoring.funding_rate),
        (OpenInterestFactor, config.scoring.open_interest),
        (LongShortFactor, config.scoring.long_short_ratio),
        (OptionsFactor, config.scoring.options),
        (MacroFactor, config.scoring.macro),
        (SentimentFactor, config.scoring.sentiment),
        (NofxSignalFactor, config.scoring.nofx_signal),
    ]
    for factor_cls, factor_config in factor_classes:
        if factor_config.enabled:
            scorer.register_factor(factor_cls(max_score_val=factor_config.weight))

    # ── 4. AI 分析器 ──
    from analyzer.reporter import AIReporter
    ai_reporter = AIReporter(config.ai)

    # ── 5. 通知系统 ──
    from notifier.email_sender import EmailNotifier
    from notifier.throttle import NotificationThrottle
    from notifier.dispatcher import NotificationDispatcher

    throttle = NotificationThrottle(config.schedule, db)
    dispatcher = NotificationDispatcher(throttle=throttle, db=db)
    dispatcher.register_channel(EmailNotifier(config.email))

    # ── 6. 调度器 ──
    from scheduler.jobs import JobScheduler
    job_scheduler = JobScheduler(
        config=config,
        registry=collector_registry,
        scorer=scorer,
        ai_reporter=ai_reporter,
        dispatcher=dispatcher,
        db=db,
    )

    # ── 7. 健康检查器 ──
    from core.health import HealthChecker
    health_checker = HealthChecker(
        exchange_config=config.exchanges,
        email_config=config.email,
        ai_config=config.ai,
        nofx_config=config.nofx,
    )

    # ── 8. Web 应用（lifespan 管理启动/关闭） ──
    from web.app import create_app
    return create_app(
        config_manager=config_manager,
        db=db,
        job_scheduler=job_scheduler,
        collector_registry=collector_registry,
        health_checker=health_checker,
    )


def main():
    """主入口函数"""
    (PROJECT_ROOT / "data").mkdir(exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    setup_logging()
    logger = logging.getLogger(__name__)
    logger.info("=" * 50)
    logger.info("  %s v%s", APP_NAME, VERSION)
    logger.info("=" * 50)

    config_manager = ConfigManager()
    if config_manager.is_first_run:
        config_manager.generate_default()
        logger.info("首次运行，已生成默认配置文件")

    app = build_services(config_manager)

    # Docker 环境下不打开浏览器
    host = os.environ.get("CSH_HOST", "127.0.0.1")
    port = int(os.environ.get("CSH_PORT", str(DEFAULT_PORT)))
    in_docker = os.environ.get("CSH_DOCKER", "").lower() in ("1", "true")

    url = f"http://{host}:{port}"
    if config_manager.is_first_run:
        url += "/setup"

    if not in_docker:
        logger.info("🌐 打开浏览器: %s", url)
        import threading
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    else:
        logger.info("🐳 Docker 模式，监听 %s:%d", host, port)

    # 启动 Web 服务器
    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
