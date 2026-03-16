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
from core.constants import AlertType, MarketState, SignalStrength
from core.models import SignalReport
from scheduler.sentinel import SentinelMonitor

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
        # 哨兵监控器
        self._sentinel = SentinelMonitor(
            config=config,
            on_trigger=self._on_sentinel_trigger,
            on_price_tick=self._on_price_tick,
        )
        # 执行引擎（可选插件）
        self._executor = None
        if config.executor.enabled:
            try:
                from executor.engine import ExecutionEngine
                from pathlib import Path
                db_path = Path(db._path) if hasattr(db, '_path') else Path("data/signals.db")
                self._executor = ExecutionEngine(config.executor, db_path)
            except Exception as e:
                logger.warning("执行引擎初始化失败: %s", e)

    @property
    def latest_reports(self) -> dict[str, dict]:
        return dict(self._latest_reports)

    def reload_config(self, config: AppConfig) -> None:
        """热重载配置，级联更新所有子服务（含执行层动态启停）"""
        old_executor_enabled = self._config.executor.enabled
        self._config = config
        self._ai_reporter.update_config(config.ai)
        self._dispatcher.update_channel_configs(email=config.email)
        self._scorer.update_config(config.scoring)
        self._sentinel.update_config(config)

        # 执行层动态启停
        if config.executor.enabled and not self._executor:
            try:
                from executor.engine import ExecutionEngine
                from pathlib import Path
                db_path = Path(self._db._path) if hasattr(self._db, '_path') else Path("data/signals.db")
                self._executor = ExecutionEngine(config.executor, db_path)
                asyncio.ensure_future(self._executor.initialize())
                logger.info("执行层已通过热重载启用")
            except Exception as e:
                logger.warning("热重载启用执行层失败: %s", e)
        elif not config.executor.enabled and self._executor:
            asyncio.ensure_future(self._executor.shutdown())
            self._executor = None
            logger.info("执行层已通过热重载停用")
        elif config.executor.enabled and self._executor:
            self._executor._config = config.executor
            logger.info("执行层配置已更新")

        # NOFX 评分因子动态启停
        from core.constants import FactorName
        nofx_want = config.scoring.nofx_signal.enabled
        nofx_has = self._scorer.has_factor(FactorName.NOFX_SIGNAL)
        if nofx_want and not nofx_has:
            from engine.factors.nofx_signal import NofxSignalFactor
            self._scorer.register_factor(
                NofxSignalFactor(max_score_val=config.scoring.nofx_signal.weight)
            )
        elif not nofx_want and nofx_has:
            self._scorer.unregister_factor(FactorName.NOFX_SIGNAL)

        logger.info("调度器配置已热重载")

    def _is_signal_actionable(self, report: SignalReport) -> bool:
        """根据配置的门槛判断信号是否可操作（替代硬编码的 report.is_actionable）"""
        threshold = self._config.general.actionable_min_confidence
        if report.confidence < threshold:
            return False
        if report.trade_plan:
            return any(
                s.position_size.value != "skip" for s in report.trade_plan.strategies
            )
        return False

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
        asyncio.create_task(self._sentinel.start())
        if self._executor:
            asyncio.create_task(self._executor.initialize())
        logger.info("调度器已启动")

    async def stop(self) -> None:
        self._scheduler.shutdown(wait=False)
        await self._sentinel.stop()
        if self._executor:
            await self._executor.shutdown()
        logger.info("调度器已停止")

    @property
    def sentinel(self) -> SentinelMonitor:
        return self._sentinel

    @property
    def executor(self):
        return self._executor

    async def run_now(self, symbol: str | None = None) -> dict | None:
        """手动立即执行一次分析（Web 界面的"立即分析"按钮）。

        手动触发始终调用 AI，不受信号强度限制。
        """
        symbols = [symbol] if symbol else self._config.general.symbols
        result = None
        for sym in symbols:
            result = await self._analyze_symbol(
                sym, alert_type=AlertType.HOURLY_REPORT, force_ai=True,
            )
        return result

    # ── 哨兵事件回调 ──

    async def _on_price_tick(self, symbol: str, price: float) -> None:
        """哨兵每次获取价格后回调，转发给执行引擎检查条件单"""
        if self._executor:
            await self._executor.on_price_tick(symbol, price)

    async def _on_sentinel_trigger(
        self, symbol: str, reason: str, alert_type: AlertType,
    ) -> None:
        """哨兵检测到事件后触发的全量分析（始终调用 AI）。"""
        logger.info("哨兵触发分析: %s — %s", symbol, reason)
        await self._analyze_symbol(
            symbol,
            alert_type=alert_type,
            force_ai=True,
            trigger_reason=reason,
        )

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
        """K 线回溯回测：用信号发出后的 OHLCV 数据判定止盈/止损命中。

        回测逻辑：
        1. 读取信号的入场价、止损价、止盈价
        2. 获取信号发出后至今的 1h K 线数据
        3. 按时间顺序遍历每根 K 线，检查最高价/最低价是否触及 SL/TP
        4. 先触达止损 → sl_hit；先触达 TP1 → tp1_hit；先触达 TP2 → tp2_hit
        5. 窗口期内均未触达 → 退化为方向判断（correct_dir / wrong_dir）
        """
        import ccxt.async_support as ccxt

        unverified = self._db.get_unverified_signals(hours_ago=hours_ago, window=window)
        if not unverified:
            return

        logger.info("回测[%s]: 检查 %d 条信号", window, len(unverified))

        ex = ccxt.okx({"enableRateLimit": True, "timeout": 10000})
        try:
            for signal in unverified:
                try:
                    await self._backtest_one_signal(ex, signal, window, hours_ago)
                except Exception as e:
                    logger.warning("回测[%s] %s 失败: %s", window, signal["id"][:8], e)
        finally:
            await ex.close()

    async def _backtest_one_signal(
        self, exchange, signal: dict, window: str, hours_ago: int,
    ) -> None:
        """两阶段状态机回测：触发判定 → 盈亏判定。

        阶段 1（条件单触发）：遍历 K 线检查 trigger_price 是否被触及
        阶段 2（盈亏判定）：从触发点开始检查 SL/TP 命中
        旧版信号（无 trade_plan）退化为直接进入阶段 2。
        """
        symbol = signal["symbol"]
        direction = signal["direction"]
        snapshot = json.loads(signal.get("snapshot_json", "{}"))
        entry_price = snapshot.get("price", {}).get("current", 0)
        if not entry_price:
            return

        suggestion = {}
        raw_sug = signal.get("suggestion_json", "")
        if raw_sug:
            try:
                suggestion = json.loads(raw_sug)
                if not isinstance(suggestion, dict):
                    suggestion = {}
            except (json.JSONDecodeError, TypeError):
                suggestion = {}

        since_ts = self._iso_to_ms(signal["timestamp"])
        if not since_ts:
            return

        candles = await exchange.fetch_ohlcv(
            symbol, timeframe="1h", since=since_ts, limit=hours_ago + 1,
        )
        if not candles:
            return

        last_candle = candles[-1]
        current_price = last_candle[4]
        change_pct = ((current_price - entry_price) / entry_price) * 100

        # 尝试两阶段回测（基于 trade_plan 中的条件策略）
        plan_strategies = self._extract_plan_strategies(suggestion)
        outcome = "expired"

        if plan_strategies:
            outcome = self._evaluate_two_stage(direction, plan_strategies, candles)

        # 退化路径：旧版 suggestion 的直接 SL/TP
        if outcome == "expired":
            sl = suggestion.get("stop_loss", 0)
            tp1 = suggestion.get("take_profit_1", 0)
            tp2 = suggestion.get("take_profit_2", 0)
            if sl or tp1:
                outcome = self._evaluate_candles_simple(
                    direction, sl, tp1, tp2, candles,
                )

        # 无任何交易建议时退化为方向判断
        if outcome == "expired" and not plan_strategies and not suggestion.get("stop_loss"):
            if direction == "bullish":
                outcome = "correct_dir" if change_pct > 0 else "wrong_dir"
            elif direction == "bearish":
                outcome = "correct_dir" if change_pct < 0 else "wrong_dir"

        correct = outcome in ("tp1_hit", "tp2_hit", "correct_dir")

        self._db.update_signal_outcome(
            signal["id"], window=window, outcome=outcome,
            price_after=current_price, change_pct=change_pct, correct=correct,
        )

        outcome_symbol = {"tp1_hit": "🎯T1", "tp2_hit": "🎯T2", "sl_hit": "💔SL",
                          "correct_dir": "✓", "wrong_dir": "✗", "expired": "⏳",
                          "not_triggered": "⏳未触发"}
        logger.info(
            "回测[%s] %s: %s %.0f→%.0f (%+.2f%%) %s",
            window, signal["id"][:8], direction,
            entry_price, current_price, change_pct,
            outcome_symbol.get(outcome, outcome),
        )

    @staticmethod
    def _extract_plan_strategies(suggestion: dict) -> list[dict]:
        """从 suggestion_json 中提取 trade_plan 的策略列表。"""
        plan = suggestion.get("_plan", {})
        strategies = plan.get("strategies", [])
        return [s for s in strategies if s.get("position_size") not in (None, "skip")]

    @staticmethod
    def _evaluate_two_stage(
        direction: str,
        strategies: list[dict],
        candles: list,
    ) -> str:
        """两阶段状态机：先判定触发，再判定盈亏。

        对每个可执行策略（position_size != skip）：
        - 阶段 1：扫描 K 线，找到 trigger_price 被触及的 K 线索引
        - 阶段 2：从触发 K 线开始，逐 K 线检查 SL/TP
        取所有策略中最好的结果。
        """
        best_outcome = "expired"
        priority = {"tp2_hit": 4, "tp1_hit": 3, "sl_hit": 1, "expired": 0}

        for strat in strategies:
            trigger = strat.get("trigger_price", 0)
            sl = strat.get("stop_loss", 0)
            tp1 = strat.get("take_profit_1", 0)
            tp2 = strat.get("take_profit_2", 0)
            stype = strat.get("strategy_type", "")

            if not trigger:
                continue

            is_long = "long" in stype
            triggered_idx = None

            # 阶段 1：触发判定
            for i, candle in enumerate(candles):
                high, low = candle[2], candle[3]
                if is_long and low <= trigger:
                    triggered_idx = i
                    break
                if not is_long and high >= trigger:
                    triggered_idx = i
                    break

            if triggered_idx is None:
                continue

            # 阶段 2：盈亏判定（从触发 K 线开始）
            strat_dir = "bullish" if is_long else "bearish"
            outcome = JobScheduler._evaluate_candles_simple(
                strat_dir, sl, tp1, tp2, candles[triggered_idx:],
            )

            if priority.get(outcome, 0) > priority.get(best_outcome, 0):
                best_outcome = outcome

        return best_outcome

    @staticmethod
    def _evaluate_candles_simple(
        direction: str,
        sl: float, tp1: float, tp2: float,
        candles: list,
    ) -> str:
        """逐 K 线遍历，判断先触达止损还是止盈。

        K 线格式: [timestamp, open, high, low, close, volume]
        """
        if not sl and not tp1:
            return "expired"

        tp1_hit = False
        for candle in candles:
            high, low = candle[2], candle[3]

            if direction == "bullish":
                if sl and low <= sl:
                    return "sl_hit"
                if tp2 and high >= tp2:
                    return "tp2_hit"
                if tp1 and high >= tp1:
                    tp1_hit = True
            elif direction == "bearish":
                if sl and high >= sl:
                    return "sl_hit"
                if tp2 and low <= tp2:
                    return "tp2_hit"
                if tp1 and low <= tp1:
                    tp1_hit = True

        return "tp1_hit" if tp1_hit else "expired"

    @staticmethod
    def _iso_to_ms(iso_str: str) -> int | None:
        """将 ISO 时间字符串转为毫秒时间戳（ccxt 需要）"""
        try:
            dt = datetime.fromisoformat(iso_str)
            return int(dt.timestamp() * 1000)
        except (ValueError, TypeError):
            return None

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
        force_ai: bool = False,
        trigger_reason: str = "",
    ) -> dict | None:
        """执行单个交易对的完整分析流程。

        Args:
            force_ai: 手动触发时为 True，强制调用 AI 不受信号强度限制
            trigger_reason: 哨兵触发原因（空=定时分析）
        """
        try:
            logger.info("开始分析: %s (trigger=%s)", symbol, trigger_reason or "scheduled")

            # 1. 采集数据
            snapshot = await self._registry.collect_snapshot(symbol)

            # 2. 评分（scorer 内部调用 market_state 分类）
            report = self._scorer.evaluate(
                snapshot,
                strategy_mode=self._config.general.strategy_mode,
            )

            # 注入触发信息
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
                trade_suggestion=report.trade_suggestion,
                trade_plan=report.trade_plan,
                ai_analysis=report.ai_analysis,
                alert_type=alert_type,
                market_state=report.market_state,
                trigger_reason=trigger_reason,
            )

            # 3. AI 分析
            should_call_ai = (
                force_ai
                or alert_type == AlertType.DAILY_REPORT
                or report.signal_strength in (SignalStrength.STRONG, SignalStrength.MODERATE)
            )
            if should_call_ai:
                from analyzer.reporter import build_score_summary, build_trade_summary
                summary = build_score_summary(report)
                trade_summary = build_trade_summary(report)
                ai_text = await self._ai_reporter.analyze(
                    snapshot.to_dict(), summary, trade_summary
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
                    trade_suggestion=report.trade_suggestion,
                    trade_plan=report.trade_plan,
                    ai_analysis=ai_text,
                    alert_type=alert_type,
                    market_state=report.market_state,
                    trigger_reason=trigger_reason,
                )

            # 4. 存储
            is_actionable = self._is_signal_actionable(report)

            report_dict = self._serialize_report(report)
            report_dict["is_actionable"] = is_actionable
            self._db.save_report(report_dict)
            self._latest_reports[symbol] = report_dict

            # 5. 更新哨兵缓存的关键位
            self._sentinel.update_levels(symbol, report.key_levels)
            if self._executor and report.trade_plan and is_actionable:
                try:
                    await self._executor.on_new_plan(symbol, report)
                except Exception as e:
                    logger.warning("执行层接收计划失败: %s", e)

            # 6. 通知分发（仅可操作信号，日报场景由调用方单独处理）
            if not skip_dispatch:
                if is_actionable:
                    await self._dispatcher.dispatch(report)
                else:
                    threshold = self._config.general.actionable_min_confidence
                    if report.confidence < threshold:
                        reason = f"信心度{report.confidence:.0f}% < 门槛{threshold:.0f}%"
                    else:
                        reason = "所有策略盈亏比不足"
                    logger.info(
                        "观察信号 %s 跳过推送 (%s)",
                        report.id[:8], reason,
                    )
            else:
                report_dict["_report_obj"] = report

            action_label = "可操作" if is_actionable else "观察"
            logger.info(
                "分析完成: %s | 评分 %s | 信心度 %.0f%% | 状态 %s | %s | 触发 %s",
                symbol, report.score_display,
                report.confidence, report.market_state.value,
                action_label, trigger_reason or "定时",
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
        "nofx_signal": "NOFX交叉验证",
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
        "volume_profile": "成交密集区",
    }
    _STRENGTH_LABELS = {"critical": "关键共振", "strong": "强", "medium": "中", "weak": "弱"}

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
            "trade": JobScheduler._serialize_trade(report.trade_suggestion),
            "trade_plan": JobScheduler._serialize_trade_plan(report.trade_plan),
            "market_state": report.market_state.value,
            "market_state_label": {
                "strong_trend": "强趋势",
                "ranging": "震荡",
                "extreme_divergence": "极端背离",
            }.get(report.market_state.value, report.market_state.value),
            "trigger_reason": report.trigger_reason,
        }

    @staticmethod
    def _serialize_trade(ts) -> dict | None:
        """将 TradeSuggestion 序列化为模板可消费的字典（旧版兼容）"""
        if ts is None:
            return None
        direction_cn = {"bullish": "做多", "bearish": "做空", "neutral": "观望"}
        size_cn = {"skip": "不建议", "light": "轻仓", "normal": "标准", "heavy": "重仓"}
        return {
            "direction": ts.direction.value,
            "direction_label": direction_cn.get(ts.direction.value, ts.direction.value),
            "entry_low": ts.entry_low,
            "entry_high": ts.entry_high,
            "stop_loss": ts.stop_loss,
            "take_profit_1": ts.take_profit_1,
            "take_profit_2": ts.take_profit_2,
            "risk_reward_1": ts.risk_reward_1,
            "risk_reward_2": ts.risk_reward_2,
            "position_size": ts.position_size.value,
            "position_label": size_cn.get(ts.position_size.value, ts.position_size.value),
            "sl_source": ts.sl_source,
            "tp1_source": ts.tp1_source,
            "tp2_source": ts.tp2_source,
            "reasoning": ts.reasoning,
        }

    @staticmethod
    def _serialize_trade_plan(plan) -> dict | None:
        """将 TradePlan 序列化为模板可消费的字典"""
        if plan is None:
            return None

        bias_cn = {"bullish": "偏多", "bearish": "偏空", "neutral": "中性"}
        size_cn = {"skip": "盈亏比不足", "light": "轻仓", "normal": "标准", "heavy": "重仓"}
        sl = JobScheduler._SOURCE_LABELS

        strategies = []
        for s in plan.strategies:
            strategies.append({
                "strategy_type": s.strategy_type,
                "label": s.label,
                "trigger_price": s.trigger_price,
                "entry_low": s.entry_low,
                "entry_high": s.entry_high,
                "stop_loss": s.stop_loss,
                "take_profit_1": s.take_profit_1,
                "take_profit_2": s.take_profit_2,
                "risk_reward": s.risk_reward,
                "position_size": s.position_size.value,
                "position_label": size_cn.get(s.position_size.value, s.position_size.value),
                "sl_source": s.sl_source,
                "tp1_source": sl.get(s.tp1_source, s.tp1_source),
                "reasoning": s.reasoning,
                "valid_hours": s.valid_hours,
                "invalidation": s.invalidation,
                "tp_mode": s.tp_mode,
                "trailing_callback_pct": s.trailing_callback_pct,
                "tp1_close_ratio": s.tp1_close_ratio,
            })

        return {
            "market_bias": plan.market_bias.value,
            "market_bias_label": bias_cn.get(plan.market_bias.value, "未知"),
            "immediate_action": plan.immediate_action,
            "strategies": strategies,
            "analysis_note": plan.analysis_note,
        }
