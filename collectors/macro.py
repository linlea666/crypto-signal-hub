"""宏观市场数据采集器。

获取与加密市场高度相关的宏观指标：
- 纳斯达克 / 标普 500
- 美元指数 (DXY)
- VIX 恐慌指数
- 加密恐惧贪婪指数
- BTC ETF 资金流（预留接口）

使用 Yahoo Finance v8 chart API（httpx 直连，无需第三方库）。
使用 alternative.me API 获取恐惧贪婪指数（免费，无需 Key）。
"""

from __future__ import annotations

import asyncio
import logging
import time

import httpx

from core.interfaces import DataCollector
from core.models import MacroData

logger = logging.getLogger(__name__)

# Yahoo Finance v8 chart API（直连 httpx，无需 yfinance 库）
_TICKER_MAP = {
    "nasdaq": "%5EIXIC",
    "sp500": "%5EGSPC",
    "dxy": "DX-Y.NYB",
    "vix": "%5EVIX",
}

_YAHOO_CHART_URL = "https://query2.finance.yahoo.com/v8/finance/chart/{symbol}?range=2d&interval=1d"

FEAR_GREED_API = "https://api.alternative.me/fng/?limit=1"

# 缓存有效期（秒）：Yahoo 数据刷新慢，缓存 5 分钟避免 429
_YAHOO_CACHE_TTL = 300


class MacroCollector(DataCollector):
    """宏观经济数据采集器"""

    def __init__(self):
        self._yahoo_cache: dict[str, dict] = {}
        self._yahoo_cache_ts: float = 0.0

    @property
    def name(self) -> str:
        return "macro"

    async def collect(self, symbol: str, snapshot_data: dict) -> dict:
        yahoo_data = await self._fetch_all_yahoo()
        nasdaq = yahoo_data.get("nasdaq", {})
        dxy = yahoo_data.get("dxy", {})
        vix = yahoo_data.get("vix", {})
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

    async def _fetch_all_yahoo(self) -> dict[str, dict]:
        """批量获取 Yahoo 数据，带缓存避免 429"""
        now = time.monotonic()
        if self._yahoo_cache and (now - self._yahoo_cache_ts) < _YAHOO_CACHE_TTL:
            logger.debug("使用 Yahoo 缓存数据（%.0f 秒前）", now - self._yahoo_cache_ts)
            return self._yahoo_cache

        results: dict[str, dict] = {}
        for key in ("nasdaq", "dxy", "vix"):
            data = await self._fetch_yahoo_quote(key)
            results[key] = data
            await asyncio.sleep(0.5)

        if any(results.values()):
            self._yahoo_cache = results
            self._yahoo_cache_ts = now

        return results

    async def _fetch_yahoo_quote(self, key: str, retries: int = 2) -> dict:
        """通过 Yahoo v8 chart API 直连获取行情（比 yfinance 库更稳定）"""
        ticker_symbol = _TICKER_MAP.get(key)
        if not ticker_symbol:
            return {}

        url = _YAHOO_CHART_URL.format(symbol=ticker_symbol)
        headers = {"User-Agent": "Mozilla/5.0 CryptoSignalHub/1.0"}

        for attempt in range(retries + 1):
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    resp = await client.get(url, headers=headers)
                    if resp.status_code == 429 and attempt < retries:
                        wait = 2 ** (attempt + 1)
                        logger.info("Yahoo %s 限流，%d 秒后重试...", key, wait)
                        await asyncio.sleep(wait)
                        continue
                    resp.raise_for_status()
                    data = resp.json()

                result = data.get("chart", {}).get("result", [])
                if not result:
                    return {}

                meta = result[0].get("meta", {})
                price = meta.get("regularMarketPrice")
                prev = meta.get("chartPreviousClose") or meta.get("previousClose")

                change_pct = 0.0
                if price and prev and prev > 0:
                    change_pct = round(((price - prev) / prev) * 100, 2)

                return {
                    "price": round(price, 2) if price else None,
                    "change_pct": change_pct,
                }
            except Exception as e:
                if attempt < retries:
                    await asyncio.sleep(1)
                    continue
                logger.warning("Yahoo %s 获取失败: %s", key, e)
                return {}
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
        return {"flow_usd": None, "trend": "unknown"}
