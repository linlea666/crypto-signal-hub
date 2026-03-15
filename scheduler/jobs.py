"""定时任务调度。

管理所有周期性任务：
- 常规分析（每 N 分钟）
- 日报推送（每天指定时间）
- 美股开盘前后加强监控
- 强信号实时检测

美股时段特殊处理：
- 美股盘前（ET 04:00-09:30 = 北京 16:00-21:30 夏令/17:00-22:30 冬令）
- 美股开盘（ET 09:30 = 北京 21:30/22:30）
- 开盘前 30 分钟和开盘后 30 分钟加密市场波动最大
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import TYPE_CHECKING

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from config.schema import AppConfig
from core.constants import AlertType, SignalStrength
from core.models import SignalReport

if TYPE_CHECKING:
    from analyzer.reporter import AIReporter
    from collectors.registry import CollectorRegistry
    from engine.scorer import SignalScorer
    from notifier.dispatcher import NotificationDispatcher
    from storage.database import Database

logger = logging.getLogger(__name__)


class JobScheduler:
    """任务调度器，编排所有定时任务"""

    def __init__(
        self,
        config: AppConfig,
        registry: "CollectorRegistry",
        scorer: "SignalScorer",
        ai_reporter: "AIReporter",
        dispatcher: "NotificationDispatcher",
        db: "Database",
    ):
        self._config = config
        self._registry = registry
        self._scorer = scorer
        self._ai_reporter = ai_reporter
        self._dispatcher = dispatcher
        self._db = db
        self._scheduler = AsyncIOScheduler()
        # 最新报告缓存（供 Web 大屏读取）
        self._latest_reports: dict[str, dict] = {}

    @property
    def latest_reports(self) -> dict[str, dict]:
        return dict(self._latest_reports)

    def reload_config(self, config: AppConfig) -> None:
        """热重载配置，级联更新所有子服务"""
        self._config = config
        self._ai_reporter.update_config(config.ai)
        self._dispatcher.update_channel_configs(email=config.email)
        logger.info("调度器配置已热重载")

    def setup(self) -> None:
        """配置所有定时任务"""
        interval = self._config.general.analysis_interval_minutes

        # 1. 常规分析任务（按配置周期执行）
        self._scheduler.add_job(
            self._run_analysis_cycle,
            IntervalTrigger(minutes=interval),
            id="analysis_cycle",
            name=f"常规分析 (每{interval}分钟)",
            replace_existing=True,
        )

        # 2. 日报推送任务
        for time_str in self._config.schedule.daily_report_times:
            try:
                hour, minute = time_str.split(":")
                self._scheduler.add_job(
                    self._run_daily_report,
                    CronTrigger(hour=int(hour), minute=int(minute)),
                    id=f"daily_report_{time_str}",
                    name=f"日报推送 ({time_str})",
                    replace_existing=True,
                )
            except ValueError:
                logger.error("无效的日报时间格式: %s", time_str)

        # 3. 美股开盘特殊监控
        # 夏令时 ET 09:30 = 北京 21:30, 冬令时 = 北京 22:30
        # 在两个时间点前后都加强监控
        if self._config.schedule.us_market_alert:
            for hour in [21, 22]:
                # 开盘前 15 分钟
                self._scheduler.add_job(
                    self._run_us_market_alert,
                    CronTrigger(hour=hour, minute=15),
                    id=f"us_market_pre_{hour}",
                    name=f"美股开盘前监控 ({hour}:15)",
                    replace_existing=True,
                )
                # 开盘后 15 分钟
                self._scheduler.add_job(
                    self._run_us_market_alert,
                    CronTrigger(hour=hour, minute=45),
                    id=f"us_market_post_{hour}",
                    name=f"美股开盘后监控 ({hour}:45)",
                    replace_existing=True,
                )

        # 4. 信号回测验证（多时间窗口）
        self._scheduler.add_job(
            lambda: asyncio.ensure_future(self._run_signal_backtest("4h", 4)),
            IntervalTrigger(hours=2),
            id="backtest_4h",
            name="回测验证 4h窗口",
            replace_existing=True,
        )
        self._scheduler.add_job(
            lambda: asyncio.ensure_future(self._run_signal_backtest("12h", 12)),
            IntervalTrigger(hours=4),
            id="backtest_12h",
            name="回测验证 12h窗口",
            replace_existing=True,
        )
        self._scheduler.add_job(
            lambda: asyncio.ensure_future(self._run_signal_backtest("24h", 24)),
            IntervalTrigger(hours=6),
            id="backtest_24h",
            name="回测验证 24h窗口",
            replace_existing=True,
        )

        # 5. 每日统计推送（21:00）
        self._scheduler.add_job(
            self._run_daily_stats,
            CronTrigger(hour=21, minute=0),
            id="daily_stats",
            name="每日回测统计 (21:00)",
            replace_existing=True,
        )

        logger.info("调度器配置完成: %d 个任务", len(self._scheduler.get_jobs()))

    def start(self) -> None:
        self._scheduler.start()
        logger.info("调度器已启动")

    def stop(self) -> None:
        self._scheduler.shutdown(wait=False)
        logger.info("调度器已停止")

    async def run_now(self, symbol: str | None = None) -> dict | None:
        """手动立即执行一次分析（Web 界面的"立即分析"按钮）"""
        symbols = [symbol] if symbol else self._config.general.symbols
        result = None
        for sym in symbols:
            result = await self._analyze_symbol(sym, alert_type=AlertType.HOURLY_REPORT)
        return result

    # ── 定时任务实现 ──

    async def _run_analysis_cycle(self) -> None:
        """常规分析周期"""
        for symbol in self._config.general.symbols:
            await self._analyze_symbol(symbol, alert_type=AlertType.HOURLY_REPORT)

    async def _run_daily_report(self) -> None:
        """日报推送：绕过 throttle，始终发送"""
        for symbol in self._config.general.symbols:
            report_dict = await self._analyze_symbol(
                symbol, alert_type=AlertType.DAILY_REPORT, skip_dispatch=True
            )
            if report_dict and report_dict.get("_report_obj"):
                report_obj = report_dict.pop("_report_obj")
                await self._dispatcher.dispatch_daily_report(report_obj)
                logger.info("日报已发送: %s", symbol)

    async def _run_us_market_alert(self) -> None:
        """美股开盘前后的加强监控"""
        logger.info("美股开盘时段，启动加强监控")
        for symbol in self._config.general.symbols:
            await self._analyze_symbol(
                symbol, alert_type=AlertType.STRONG_SIGNAL
            )

    async def _run_signal_backtest(self, window: str = "4h", hours_ago: int = 4) -> None:
        """多窗口回测验证历史信号"""
        import ccxt.async_support as ccxt

        unverified = self._db.get_unverified_signals(hours_ago=hours_ago, window=window)
        if not unverified:
            return

        logger.info("回测[%s]: 检查 %d 条信号", window, len(unverified))

        ex = ccxt.okx({"enableRateLimit": True, "timeout": 8000})
        try:
            for signal in unverified:
                try:
                    symbol = signal["symbol"]
                    direction = signal["direction"]
                    snapshot = json.loads(signal.get("snapshot_json", "{}"))
                    entry_price = snapshot.get("price", {}).get("current", 0)
                    if not entry_price:
                        continue

                    ticker = await ex.fetch_ticker(symbol)
                    current_price = ticker.get("last", 0)
                    if not current_price:
                        continue

                    change_pct = ((current_price - entry_price) / entry_price) * 100

                    if direction == "bullish":
                        correct = change_pct > 0
                    elif direction == "bearish":
                        correct = change_pct < 0
                    else:
                        correct = abs(change_pct) < 1.0

                    self._db.update_signal_outcome(
                        signal["id"], current_price, correct,
                        window=window, change_pct=change_pct,
                    )
                    logger.info(
                        "回测[%s] %s: %s %.0f→%.0f (%+.2f%%) %s",
                        window, signal["id"][:8], direction,
                        entry_price, current_price, change_pct,
                        "✓" if correct else "✗",
                    )
                except Exception as e:
                    logger.warning("回测[%s] %s 失败: %s", window, signal["id"][:8], e)
        finally:
            await ex.close()

    async def _run_daily_stats(self) -> None:
        """每日 21:00 推送回测统计"""
        try:
            stats = self._db.get_backtest_stats(days=1)
            summary = stats.get("summary", {})
            if summary.get("verified", 0) == 0:
                logger.info("今日无已验证信号，跳过统计推送")
                return

            w24 = stats.get("24h", {})
            w12 = stats.get("12h", {})
            w4 = stats.get("4h", {})

            text = (
                f"📊 CryptoSignal Hub 每日统计\n\n"
                f"今日信号: {summary['verified']} 条 | "
                f"命中: {summary['correct']} 条 | "
                f"准确率: {summary['rate']}%\n\n"
                f"分窗口统计:\n"
                f"  4h:  {w4.get('rate', 0)}% ({w4.get('verified', 0)}条, "
                f"均变 {w4.get('avg_change_pct', 0):+.2f}%)\n"
                f"  12h: {w12.get('rate', 0)}% ({w12.get('verified', 0)}条, "
                f"均变 {w12.get('avg_change_pct', 0):+.2f}%)\n"
                f"  24h: {w24.get('rate', 0)}% ({w24.get('verified', 0)}条, "
                f"均变 {w24.get('avg_change_pct', 0):+.2f}%)"
            )
            await self._dispatcher.dispatch_text("daily_stats", text)
            logger.info("每日统计已推送")
        except Exception as e:
            logger.error("每日统计推送失败: %s", e)

    async def _analyze_symbol(
        self,
        symbol: str,
        alert_type: AlertType = AlertType.HOURLY_REPORT,
        skip_dispatch: bool = False,
    ) -> dict | None:
        """执行单个交易对的完整分析流程"""
        try:
            logger.info("开始分析: %s", symbol)

            # 1. 采集数据
            snapshot = await self._registry.collect_snapshot(symbol)

            # 2. 评分
            report = self._scorer.evaluate(snapshot)

            # 3. AI 分析（仅中等以上信号或日报触发）
            if (
                report.signal_strength in (SignalStrength.STRONG, SignalStrength.MODERATE)
                or alert_type == AlertType.DAILY_REPORT
            ):
                from analyzer.reporter import build_score_summary
                summary = build_score_summary(report)
                ai_text = await self._ai_reporter.analyze(
                    snapshot.to_dict(), summary
                )
                report = SignalReport(
                    id=report.id,
                    timestamp=report.timestamp,
                    symbol=report.symbol,
                    snapshot=report.snapshot,
                    factor_scores=report.factor_scores,
                    total_score=report.total_score,
                    max_possible_score=report.max_possible_score,
                    direction=report.direction,
                    confidence=report.confidence,
                    signal_strength=report.signal_strength,
                    key_levels=report.key_levels,
                    ai_analysis=ai_text,
                    alert_type=alert_type,
                )

            # 4. 存储
            report_dict = self._serialize_report(report)
            self._db.save_report(report_dict)
            self._latest_reports[symbol] = report_dict

            # 5. 通知分发（日报场景由调用方单独处理）
            if not skip_dispatch:
                await self._dispatcher.dispatch(report)
            else:
                report_dict["_report_obj"] = report

            logger.info(
                "分析完成: %s | 评分 %s | 信心度 %.0f%% | 强度 %s",
                symbol, report.score_display,
                report.confidence, report.signal_strength.value,
            )
            return report_dict

        except Exception as e:
            logger.error("分析 %s 失败: %s", symbol, e, exc_info=True)
            return None

    # 中文标签映射（因子名、关键位来源、强度）
    _FACTOR_LABELS = {
        "technical": "技术面",
        "funding_rate": "资金费率",
        "open_interest": "持仓量",
        "long_short_ratio": "多空比",
        "options": "期权数据",
        "macro": "宏观环境",
        "sentiment": "市场情绪",
    }
    _SOURCE_LABELS = {
        "MA20": "20日均线",
        "MA60": "60日均线",
        "24h_low": "24h低点",
        "24h_high": "24h高点",
        "options_put_oi": "期权Put密集",
        "options_call_oi": "期权Call密集",
        "max_pain": "最大痛点",
        "fib_0.382": "斐波那契38.2%",
        "fib_0.500": "斐波那契50%",
        "fib_0.618": "斐波那契61.8%",
        "swing_low": "前期低点",
        "swing_high": "前期高点",
        "round_number": "整数关口",
        "orderbook_bid": "买盘密集区",
        "orderbook_ask": "卖盘密集区",
    }
    _STRENGTH_LABELS = {"strong": "强", "medium": "中", "weak": "弱"}

    @staticmethod
    def _serialize_report(report) -> dict:
        """将 SignalReport 序列化为可存储的字典"""
        fl = JobScheduler._FACTOR_LABELS
        sl = JobScheduler._SOURCE_LABELS
        stl = JobScheduler._STRENGTH_LABELS

        def _level_dict(lv):
            return {
                "price": lv.price,
                "source": lv.source,
                "source_label": sl.get(lv.source, lv.source),
                "strength": lv.strength,
                "strength_label": stl.get(lv.strength, lv.strength),
            }

        return {
            "id": report.id,
            "timestamp": report.timestamp.isoformat(),
            "symbol": report.symbol,
            "total_score": report.total_score,
            "max_score": report.max_possible_score,
            "direction": report.direction.value,
            "confidence": report.confidence,
            "signal_strength": report.signal_strength.value,
            "alert_type": report.alert_type.value,
            "ai_analysis": report.ai_analysis,
            "snapshot": report.snapshot.to_dict(),
            "scores": [
                {
                    "name": fs.name,
                    "label": fl.get(
                        fs.name.value if hasattr(fs.name, "value") else fs.name,
                        str(fs.name),
                    ),
                    "score": fs.score,
                    "max_score": fs.max_score,
                    "direction": fs.direction.value,
                    "details": fs.details,
                }
                for fs in report.factor_scores
            ],
            "levels": {
                "supports": [_level_dict(lv) for lv in report.key_levels.supports],
                "resistances": [_level_dict(lv) for lv in report.key_levels.resistances],
            },
        }
