"""宏观市场数据采集器。

获取与加密市场高度相关的宏观指标：
- 纳斯达克 / 标普 500
- 美元指数 (DXY)
- VIX 恐慌指数
- 加密恐惧贪婪指数
- BTC ETF 资金流（预留接口）

使用 yfinance 获取美股数据（免费，无需 Key）。
使用 alternative.me API 获取恐惧贪婪指数（免费，无需 Key）。
"""

from __future__ import annotations

import logging

import httpx
import yfinance as yf

from core.interfaces import DataCollector
from core.models import MacroData

logger = logging.getLogger(__name__)

# yfinance ticker 映射
_TICKER_MAP = {
    "nasdaq": "^IXIC",
    "sp500": "^GSPC",
    "dxy": "DX-Y.NYB",
    "vix": "^VIX",
}

FEAR_GREED_API = "https://api.alternative.me/fng/?limit=1"


class MacroCollector(DataCollector):
    """宏观经济数据采集器"""

    @property
    def name(self) -> str:
        return "macro"

    async def collect(self, symbol: str, snapshot_data: dict) -> dict:
        nasdaq = await self._fetch_yahoo_quote("nasdaq")
        dxy = await self._fetch_yahoo_quote("dxy")
        vix = await self._fetch_yahoo_quote("vix")
        fg = await self._fetch_fear_greed()
        etf_flow = await self._fetch_etf_flow()

        snapshot_data["macro"] = MacroData(
            nasdaq_price=nasdaq.get("price"),
            nasdaq_change_pct=nasdaq.get("change_pct", 0),
            dxy_price=dxy.get("price"),
            dxy_change_pct=dxy.get("change_pct", 0),
            vix_value=vix.get("price"),
            fear_greed_value=fg.get("value"),
            fear_greed_label=fg.get("label", "unknown"),
            btc_etf_flow_usd=etf_flow.get("flow_usd"),
            btc_etf_flow_3d_trend=etf_flow.get("trend", "unknown"),
        )
        return snapshot_data

    async def _fetch_yahoo_quote(self, key: str) -> dict:
        """通过 yfinance 获取行情快照"""
        ticker_symbol = _TICKER_MAP.get(key)
        if not ticker_symbol:
            return {}

        try:
            ticker = yf.Ticker(ticker_symbol)
            info = ticker.fast_info
            price = float(info.last_price) if hasattr(info, "last_price") else None
            prev = float(info.previous_close) if hasattr(info, "previous_close") else None
            change_pct = 0.0
            if price and prev and prev > 0:
                change_pct = round(((price - prev) / prev) * 100, 2)
            return {"price": round(price, 2) if price else None, "change_pct": change_pct}
        except Exception as e:
            logger.warning("yfinance 获取 %s 失败: %s", key, e)
            return {}

    async def _fetch_fear_greed(self) -> dict:
        """获取加密恐惧贪婪指数"""
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(FEAR_GREED_API)
                resp.raise_for_status()
                data = resp.json()
                item = data.get("data", [{}])[0]
                return {
                    "value": int(item.get("value", 50)),
                    "label": item.get("value_classification", "unknown"),
                }
        except Exception as e:
            logger.warning("获取恐惧贪婪指数失败: %s", e)
            return {}

    async def _fetch_etf_flow(self) -> dict:
        """获取 BTC ETF 资金流（预留接口，后续对接 SoSoValue 等数据源）"""
        # Phase 2: 实现 SoSoValue / Farside 数据采集
        return {"flow_usd": None, "trend": "unknown"}
