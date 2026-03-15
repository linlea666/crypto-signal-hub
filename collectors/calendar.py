"""经济日历采集器。

获取即将到来的重大经济事件（如 FOMC、CPI、非农就业等），
这些事件对加密市场有显著影响。

使用 Trading Economics 公共日历页面解析，免费无需 Key。
备用方案：nyfed.org / ForexFactory RSS。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

import httpx

from core.interfaces import DataCollector
from core.models import UpcomingEvent

logger = logging.getLogger(__name__)

CALENDAR_API = "https://api.tradingeconomics.com/calendar"
FALLBACK_EVENTS_URL = "https://nyfed.org/markets/calendar"

HIGH_IMPACT_KEYWORDS = [
    "interest rate", "fed", "fomc", "cpi", "ppi", "nonfarm", "non-farm",
    "gdp", "unemployment", "retail sales", "pce", "jackson hole",
    "ecb", "boj", "inflation", "payroll",
]


class CalendarCollector(DataCollector):
    """经济日历采集器"""

    @property
    def name(self) -> str:
        return "calendar"

    async def collect(self, symbol: str, snapshot_data: dict) -> dict:
        events = await self._fetch_upcoming_events()
        snapshot_data["events"] = events
        return snapshot_data

    async def _fetch_upcoming_events(self) -> list[UpcomingEvent]:
        """获取未来 7 天的高影响力经济事件"""
        try:
            events = await self._fetch_from_api()
            if events:
                return events
        except Exception as e:
            logger.warning("经济日历 API 获取失败: %s", e)

        return self._get_static_known_events()

    async def _fetch_from_api(self) -> list[UpcomingEvent]:
        """通过公共 API 获取经济日历"""
        now = datetime.utcnow()
        start = now.strftime("%Y-%m-%d")
        end = (now + timedelta(days=7)).strftime("%Y-%m-%d")

        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{CALENDAR_API}?c=united states&d1={start}&d2={end}",
                headers={"User-Agent": "CryptoSignalHub/1.0"},
            )
            if resp.status_code != 200:
                return []

            data = resp.json()
            if not isinstance(data, list):
                return []

            events: list[UpcomingEvent] = []
            for item in data:
                event_name = str(item.get("Event", "")).lower()
                is_high = any(kw in event_name for kw in HIGH_IMPACT_KEYWORDS)
                if not is_high:
                    continue

                try:
                    event_time = datetime.fromisoformat(
                        str(item.get("Date", ""))[:19]
                    )
                except (ValueError, TypeError):
                    event_time = now

                events.append(UpcomingEvent(
                    name=item.get("Event", "Unknown"),
                    time=event_time,
                    impact="high",
                    description=f"前值: {item.get('Previous', 'N/A')}, "
                                f"预期: {item.get('Forecast', 'N/A')}",
                ))

            return events[:10]

    @staticmethod
    def _get_static_known_events() -> list[UpcomingEvent]:
        """回退方案：返回已知的常规经济日历（FOMC 等固定日程）"""
        now = datetime.utcnow()
        known: list[UpcomingEvent] = []

        fomc_dates_2026 = [
            "2026-01-28", "2026-03-18", "2026-05-06", "2026-06-17",
            "2026-07-29", "2026-09-16", "2026-11-04", "2026-12-16",
        ]
        for d in fomc_dates_2026:
            try:
                dt = datetime.strptime(d, "%Y-%m-%d")
                if dt > now and (dt - now).days <= 14:
                    known.append(UpcomingEvent(
                        name="FOMC Interest Rate Decision",
                        time=dt,
                        impact="high",
                        description="美联储利率决议",
                    ))
            except ValueError:
                continue

        return known
