"""哨兵监控模块。

轻量级 REST 轮询替代"每 N 分钟无差别分析"，实现"低频巡检 + 事件驱动"。

监控维度（全部基于 REST，无 WebSocket 依赖）：
- 价格突破/跌破关键支撑阻力位（每 30-60 秒）
- 短时大幅波动（每 30-60 秒）
- OI 异动（每 5 分钟）
- 资金费率极端（每 5 分钟）

设计原则：
- 哨兵本身只做轻量检查（1-2 次 API 调用/轮）
- 发现异常后回调 JobScheduler 触发全量分析
- 冷却机制防止频繁触发（全局 30 分钟 + 事件级 60 分钟）
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from core.constants import AlertType
from core.models import KeyLevels
from core.time_utils import now_beijing

if TYPE_CHECKING:
    from config.schema import AppConfig

logger = logging.getLogger(__name__)

TriggerCallback = Callable[[str, str, AlertType], Awaitable[Any]]
PriceTickCallback = Callable[[str, float], Awaitable[Any]]


class SentinelMonitor:
    """哨兵监控器：轻量 REST 轮询 + 事件驱动触发全量分析。"""

    def __init__(
        self,
        config: "AppConfig",
        on_trigger: TriggerCallback,
        on_price_tick: PriceTickCallback | None = None,
    ):
        self._config = config
        self._sentinel_cfg = config.sentinel
        self._on_trigger = on_trigger
        self._on_price_tick = on_price_tick

        self._exchange: Any = None
        self._running = False
        self._tasks: list[asyncio.Task] = []

        self._cached_levels: dict[str, KeyLevels] = {}
        self._price_history: dict[str, deque] = {}
        self._oi_baseline: dict[str, float] = {}
        self._cooldowns: dict[str, datetime] = {}

        self._tick_count = 0
        self._trigger_count = 0

    def update_config(self, config: "AppConfig") -> None:
        """热重载配置（由 JobScheduler.reload_config 调用）"""
        self._config = config
        self._sentinel_cfg = config.sentinel
        logger.info("哨兵配置已更新")

    async def start(self) -> None:
        if not self._sentinel_cfg.enabled:
            logger.info("哨兵监控已禁用")
            return

        if self._running:
            logger.warning("哨兵已在运行，忽略重复启动")
            return

        import ccxt.async_support as ccxt

        ex_id = self._config.exchanges.primary
        ex_class = getattr(ccxt, ex_id, None)
        if ex_class is None:
            logger.error("哨兵: 不支持的交易所 %s", ex_id)
            return

        self._exchange = ex_class({"enableRateLimit": True})
        self._running = True

        self._tasks = [
            asyncio.create_task(self._price_loop()),
            asyncio.create_task(self._derivatives_loop()),
        ]

        logger.info(
            "哨兵启动: 价格 %ds / 衍生品 %ds / 冷却 %dm",
            self._sentinel_cfg.price_check_interval,
            self._sentinel_cfg.derivatives_check_interval,
            self._sentinel_cfg.cooldown_minutes,
        )

    async def stop(self) -> None:
        self._running = False
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except asyncio.CancelledError:
                pass
        self._tasks.clear()
        if self._exchange:
            try:
                await self._exchange.close()
            except Exception:
                pass
            self._exchange = None

    def update_levels(self, symbol: str, levels: KeyLevels) -> None:
        """全量分析完成后更新缓存关键位（由 JobScheduler 调用）。"""
        self._cached_levels[symbol] = levels

    # ── 价格监控循环 ──

    async def _price_loop(self) -> None:
        interval = self._sentinel_cfg.price_check_interval
        await asyncio.sleep(10)
        while self._running:
            for symbol in self._config.general.symbols:
                try:
                    await self._check_price(symbol)
                except Exception as e:
                    logger.debug("哨兵价格检查异常 %s: %s", symbol, e)
            self._tick_count += 1
            await asyncio.sleep(interval)

    async def _check_price(self, symbol: str) -> None:
        if not self._exchange:
            return

        ticker = await self._exchange.fetch_ticker(symbol)
        price = float(ticker.get("last") or 0)
        if not price:
            return

        now = now_beijing()

        if symbol not in self._price_history:
            self._price_history[symbol] = deque(maxlen=60)
        self._price_history[symbol].append((now, price))

        await self._check_level_breakout(symbol, price, now)
        await self._check_rapid_move(symbol, price, now)

        if self._on_price_tick:
            try:
                await self._on_price_tick(symbol, price)
            except Exception as e:
                logger.warning("on_price_tick 回调异常 %s: %s", symbol, e)

    async def _check_level_breakout(
        self, symbol: str, price: float, now: datetime,
    ) -> None:
        levels = self._cached_levels.get(symbol)
        if not levels:
            return

        buf = self._sentinel_cfg.breakout_buffer_pct / 100

        for lv in levels.resistances[:3]:
            if price > lv.price * (1 + buf):
                key = f"brk_up_{symbol}_{lv.price:.0f}"
                reason = f"价格${price:.0f}突破阻力${lv.price:.0f}({lv.source})"
                await self._try_trigger(key, symbol, reason, AlertType.BREAKOUT, now)
                break

        for lv in levels.supports[:3]:
            if price < lv.price * (1 - buf):
                key = f"brk_dn_{symbol}_{lv.price:.0f}"
                reason = f"价格${price:.0f}跌破支撑${lv.price:.0f}({lv.source})"
                await self._try_trigger(key, symbol, reason, AlertType.BREAKOUT, now)
                break

    async def _check_rapid_move(
        self, symbol: str, price: float, now: datetime,
    ) -> None:
        history = self._price_history.get(symbol)
        if not history or len(history) < 3:
            return

        cutoff = now - timedelta(minutes=15)
        old_price = None
        for ts, p in history:
            if ts >= cutoff:
                old_price = p
                break

        if old_price is None or old_price <= 0:
            return

        change_pct = abs(price - old_price) / old_price * 100
        if change_pct >= self._sentinel_cfg.rapid_move_pct:
            direction = "暴涨" if price > old_price else "暴跌"
            key = f"rapid_{symbol}"
            reason = f"15分钟{direction}{change_pct:.1f}%（${old_price:.0f}→${price:.0f}）"
            await self._try_trigger(key, symbol, reason, AlertType.RAPID_MOVE, now)

    # ── 衍生品监控循环 ──

    async def _derivatives_loop(self) -> None:
        interval = self._sentinel_cfg.derivatives_check_interval
        await asyncio.sleep(90)
        while self._running:
            for symbol in self._config.general.symbols:
                try:
                    await self._check_derivatives(symbol)
                except Exception as e:
                    logger.debug("哨兵衍生品检查异常 %s: %s", symbol, e)
            await asyncio.sleep(interval)

    async def _check_derivatives(self, symbol: str) -> None:
        if not self._exchange:
            return
        now = now_beijing()
        swap = symbol.replace("/USDT", "/USDT:USDT")

        await self._check_funding_rate(symbol, swap, now)
        await self._check_oi(symbol, swap, now)

    async def _check_funding_rate(
        self, symbol: str, swap: str, now: datetime,
    ) -> None:
        try:
            fr_data = await self._exchange.fetch_funding_rate(swap)
            rate = fr_data.get("fundingRate", 0) or 0
        except Exception:
            return

        if abs(rate) >= self._sentinel_cfg.funding_extreme_rate:
            label = "极高" if rate > 0 else "极低"
            key = f"funding_{symbol}"
            reason = f"资金费率{label}（{rate * 100:.4f}%）"
            await self._try_trigger(key, symbol, reason, AlertType.FUNDING_EXTREME, now)

    async def _check_oi(
        self, symbol: str, swap: str, now: datetime,
    ) -> None:
        try:
            oi_data = await self._exchange.fetch_open_interest(swap)
            current_oi = float(oi_data.get("openInterestValue", 0) or 0)
        except Exception:
            return

        if not current_oi:
            return

        baseline = self._oi_baseline.get(symbol, 0)
        if baseline <= 0:
            self._oi_baseline[symbol] = current_oi
            return

        change_pct = abs(current_oi - baseline) / baseline * 100
        if change_pct >= self._sentinel_cfg.oi_change_threshold_pct:
            label = "暴增" if current_oi > baseline else "暴跌"
            key = f"oi_{symbol}"
            reason = f"OI{label}{change_pct:.1f}%"
            await self._try_trigger(key, symbol, reason, AlertType.OI_ANOMALY, now)

        self._oi_baseline[symbol] = current_oi

    # ── 冷却与触发 ──

    async def _try_trigger(
        self, event_key: str, symbol: str, reason: str,
        alert_type: AlertType, now: datetime,
    ) -> None:
        global_key = f"_global_{symbol}"
        global_cd = self._cooldowns.get(global_key)
        if global_cd:
            elapsed = (now - global_cd).total_seconds()
            if elapsed < self._sentinel_cfg.cooldown_minutes * 60:
                return

        event_cd = self._cooldowns.get(event_key)
        if event_cd:
            elapsed = (now - event_cd).total_seconds()
            if elapsed < self._sentinel_cfg.level_cooldown_minutes * 60:
                return

        self._cooldowns[event_key] = now
        self._cooldowns[global_key] = now
        self._trigger_count += 1

        logger.info("🚨 哨兵触发 [%s] %s: %s", alert_type.value, symbol, reason)
        try:
            await self._on_trigger(symbol, reason, alert_type)
        except Exception as e:
            logger.error("哨兵触发分析失败: %s", e)

    @property
    def stats(self) -> dict:
        return {
            "running": self._running,
            "tick_count": self._tick_count,
            "trigger_count": self._trigger_count,
            "cached_symbols": list(self._cached_levels.keys()),
        }
