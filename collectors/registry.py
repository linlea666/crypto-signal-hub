"""采集器注册与编排。

管理所有 DataCollector 实例，按需调用并组装 MarketSnapshot。
采集器之间互相独立——单个采集器失败不影响其他采集器。
"""

from __future__ import annotations

import asyncio
import logging

from core.interfaces import DataCollector
from core.time_utils import now_beijing
from core.models import (
    DerivativesData,
    FundingRateData,
    LongShortData,
    MarketSnapshot,
    OpenInterestData,
    PriceData,
    TechnicalData,
    NofxData,
)

logger = logging.getLogger(__name__)


class CollectorRegistry:
    """采集器注册表，负责编排数据采集流程。"""

    def __init__(self):
        self._collectors: list[DataCollector] = []
        self._status: dict[str, dict] = {}  # {name: {ok, last_run, error}}

    def register(self, collector: DataCollector) -> None:
        self._collectors.append(collector)
        self._status[collector.name] = {
            "ok": True, "last_run": None, "error": ""
        }
        logger.info("注册采集器: %s", collector.name)

    @property
    def status(self) -> dict[str, dict]:
        return dict(self._status)

    async def initialize_all(self) -> None:
        for c in self._collectors:
            try:
                await c.initialize()
            except Exception as e:
                logger.error("采集器 %s 初始化失败: %s", c.name, e)
                self._status[c.name] = {"ok": False, "last_run": None, "error": str(e)}

    async def collect_snapshot(self, symbol: str) -> MarketSnapshot:
        """并行执行所有采集器，组装完整的市场快照。

        每个采集器向共享的 snapshot_data 字典写入自己负责的字段。
        单个采集器异常时记录错误并跳过，保证其他数据可用。
        """
        snapshot_data: dict = {}
        now = now_beijing()

        tasks = []
        for collector in self._collectors:
            tasks.append(self._run_collector(collector, symbol, snapshot_data))
        await asyncio.gather(*tasks)

        return self._build_snapshot(symbol, now, snapshot_data)

    async def _run_collector(
        self, collector: DataCollector, symbol: str, snapshot_data: dict
    ) -> None:
        """执行单个采集器，捕获异常避免影响整体"""
        try:
            await collector.collect(symbol, snapshot_data)
            self._status[collector.name] = {
                "ok": True,
                "last_run": now_beijing().isoformat(),
                "error": "",
            }
        except Exception as e:
            logger.error("采集器 %s 执行失败: %s", collector.name, e, exc_info=True)
            self._status[collector.name] = {
                "ok": False,
                "last_run": now_beijing().isoformat(),
                "error": str(e),
            }

    def _build_snapshot(
        self, symbol: str, timestamp: datetime, data: dict
    ) -> MarketSnapshot:
        """从采集结果字典构建类型安全的 MarketSnapshot"""
        return MarketSnapshot(
            timestamp=timestamp,
            symbol=symbol,
            price=data.get("price", PriceData(
                current=0, high_24h=0, low_24h=0, change_pct_24h=0, volume_24h=0
            )),
            technical=data.get("technical", TechnicalData()),
            derivatives=DerivativesData(
                funding_rate=data.get("funding_rate", FundingRateData()),
                open_interest=data.get("open_interest", OpenInterestData()),
                long_short=data.get("long_short", LongShortData()),
            ),
            options=data.get("options"),
            macro=data.get("macro"),
            nofx=data.get("nofx"),
            events=data.get("events", []),
            orderbook_clusters=data.get("orderbook_clusters", {}),
        )

    async def cleanup_all(self) -> None:
        for c in self._collectors:
            try:
                await c.cleanup()
            except Exception as e:
                logger.error("采集器 %s 清理失败: %s", c.name, e)
