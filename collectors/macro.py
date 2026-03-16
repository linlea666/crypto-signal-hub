"""宏观市场数据采集器。

获取与加密市场高度相关的宏观指标：
- 纳斯达克 / 标普 500 / 美元指数 (DXY)  ← ifnews 全球股市接口
- 加密恐惧贪婪指数                        ← alternative.me
- BTC ETF 资金流                          ← 预留接口

数据源选择理由：
- ifnews 提供全球主要股市指数实时数据，无需认证，无限流限制
- 替换原 Yahoo Finance v8 API（频繁 429 限流）
- VIX 改由 BTC 期权 IV_rank 在评分层替代（更贴近加密市场）
"""

from __future__ import annotations

import logging
import time

import httpx

from core.interfaces import DataCollector
from core.models import MacroData

logger = logging.getLogger(__name__)

IFNEWS_URL = "http://worldmap.ifnews.com/chinamap/china/financialData?type=all"
FEAR_GREED_API = "https://api.alternative.me/fng/?limit=1"

# ifnews 返回中文 name → 内部名称映射（精确匹配）
_IFNEWS_NAME_MAP = {
    "纳斯达克指数": "nasdaq",
    "标普500指数": "sp500",
    "美元指数": "dxy",
}

_CACHE_TTL = 300  # 缓存 5 分钟


class MacroCollector(DataCollector):
    """宏观经济数据采集器"""

    def __init__(self):
        self._ifnews_cache: dict[str, dict] = {}
        self._ifnews_cache_ts: float = 0.0

    @property
    def name(self) -> str:
        return "macro"

    async def collect(self, symbol: str, snapshot_data: dict) -> dict:
        indices = await self._fetch_ifnews()
        nasdaq = indices.get("nasdaq", {})
        sp500 = indices.get("sp500", {})
        dxy = indices.get("dxy", {})
        fg = await self._fetch_fear_greed()
        etf_flow = await self._fetch_etf_flow()

        snapshot_data["macro"] = MacroData(
            nasdaq_price=nasdaq.get("price"),
            nasdaq_change_pct=nasdaq.get("change_pct", 0),
            sp500_price=sp500.get("price"),
            sp500_change_pct=sp500.get("change_pct", 0),
            dxy_price=dxy.get("price"),
            dxy_change_pct=dxy.get("change_pct", 0),
            vix_value=None,  # ifnews 不含 VIX，评分层用 BTC IV_rank 替代
            fear_greed_value=fg.get("value"),
            fear_greed_label=fg.get("label", "unknown"),
            btc_etf_flow_usd=etf_flow.get("flow_usd"),
            btc_etf_flow_3d_trend=etf_flow.get("trend", "unknown"),
        )
        return snapshot_data

    async def _fetch_ifnews(self) -> dict[str, dict]:
        """从 ifnews 获取全球股市指数，带缓存。

        返回 {"nasdaq": {"price": ..., "change_pct": ...}, "sp500": ..., "dxy": ...}
        """
        now = time.monotonic()
        if self._ifnews_cache and (now - self._ifnews_cache_ts) < _CACHE_TTL:
            logger.debug("使用 ifnews 缓存数据（%.0f 秒前）", now - self._ifnews_cache_ts)
            return self._ifnews_cache

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(IFNEWS_URL)
                resp.raise_for_status()
                raw_list = resp.json()

            results: dict[str, dict] = {}
            if not isinstance(raw_list, list):
                logger.warning("ifnews 返回格式异常: %s", type(raw_list))
                return results

            for item in raw_list:
                name = item.get("name", "")
                internal_name = _IFNEWS_NAME_MAP.get(name)
                if internal_name is None:
                    continue

                price = self._safe_float(item.get("price"))
                # ifnews 涨跌幅字段为 priceLimit（百分比值）
                change_pct = self._safe_float(item.get("priceLimit"))

                if price is not None:
                    results[internal_name] = {
                        "price": round(price, 2),
                        "change_pct": round(change_pct, 2) if change_pct is not None else 0.0,
                    }

            if results:
                self._ifnews_cache = results
                self._ifnews_cache_ts = now
                logger.debug("ifnews 数据更新: %s", list(results.keys()))

            return results

        except Exception as e:
            logger.warning("ifnews 全球指数获取失败: %s", e)
            return self._ifnews_cache or {}

    @staticmethod
    def _safe_float(val) -> float | None:
        """安全将字符串/数值转为 float，无效值返回 None"""
        if val is None:
            return None
        try:
            return float(str(val).replace(",", "").replace("%", ""))
        except (ValueError, TypeError):
            return None

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
