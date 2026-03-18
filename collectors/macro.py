"""宏观市场数据采集器。

获取与加密市场高度相关的宏观指标：
- 纳斯达克 / 标普 500 / 美元指数 (DXY)  ← ifnews 全球股市接口
- VIX 恐慌指数                            ← 新浪财经
- 美国 10 年期国债收益率                    ← 新浪债券
- COMEX 黄金价格（避险情绪参考）            ← 新浪财经期货
- 加密恐惧贪婪指数                          ← alternative.me
- BTC ETF 资金流                            ← 预留接口
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
SINA_VIX_URL = "https://gi.finance.sina.com.cn/hq/min?symbol=VIX"
SINA_US10Y_URL = "https://bond.finance.sina.com.cn/hq/gb/min?symbol=us10yt"
SINA_GOLD_URL = "https://hq.sinajs.cn/list=hf_GC"

_IFNEWS_NAME_MAP = {
    "纳斯达克指数": "nasdaq",
    "标普500指数": "sp500",
    "美元指数": "dxy",
}

_CACHE_TTL = 300  # 缓存 5 分钟
_SINA_HQ_HEADERS = {
    "Referer": "https://finance.sina.com.cn",
}


class MacroCollector(DataCollector):
    """宏观经济数据采集器"""

    def __init__(self):
        self._ifnews_cache: dict[str, dict] = {}
        self._ifnews_cache_ts: float = 0.0
        self._vix_cache: float | None = None
        self._vix_cache_ts: float = 0.0
        self._us10y_cache: dict = {}
        self._us10y_cache_ts: float = 0.0
        self._gold_cache: dict = {}
        self._gold_cache_ts: float = 0.0

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
        vix = await self._fetch_vix_sina()
        us10y = await self._fetch_us10y_sina()
        gold = await self._fetch_gold_sina()

        now = time.monotonic()
        data_age_hours = (now - self._ifnews_cache_ts) / 3600 if self._ifnews_cache_ts > 0 else 0.0

        snapshot_data["macro"] = MacroData(
            nasdaq_price=nasdaq.get("price"),
            nasdaq_change_pct=nasdaq.get("change_pct", 0),
            sp500_price=sp500.get("price"),
            sp500_change_pct=sp500.get("change_pct", 0),
            dxy_price=dxy.get("price"),
            dxy_change_pct=dxy.get("change_pct", 0),
            vix_value=vix,
            us10y_yield=us10y.get("yield"),
            us10y_change_pct=us10y.get("change_pct", 0),
            gold_price=gold.get("price"),
            gold_change_pct=gold.get("change_pct", 0),
            fear_greed_value=fg.get("value"),
            fear_greed_label=fg.get("label", "unknown"),
            btc_etf_flow_usd=etf_flow.get("flow_usd"),
            btc_etf_flow_3d_trend=etf_flow.get("trend", "unknown"),
            data_age_hours=round(data_age_hours, 2),
        )
        return snapshot_data

    # ── 新浪 VIX ──

    async def _fetch_vix_sina(self) -> float | None:
        now = time.monotonic()
        if self._vix_cache is not None and (now - self._vix_cache_ts) < _CACHE_TTL:
            return self._vix_cache
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(SINA_VIX_URL)
                resp.raise_for_status()
                data = resp.json()
            rows = data.get("result", {}).get("data", [])
            if rows and isinstance(rows[-1], list) and len(rows[-1]) >= 2:
                val = self._safe_float(rows[-1][1])
                if val is not None:
                    self._vix_cache = val
                    self._vix_cache_ts = now
                    return val
        except Exception as e:
            logger.warning("新浪 VIX 获取失败: %s", e)
        return self._vix_cache

    # ── 新浪 10Y 国债 ──

    async def _fetch_us10y_sina(self) -> dict:
        now = time.monotonic()
        if self._us10y_cache and (now - self._us10y_cache_ts) < _CACHE_TTL:
            return self._us10y_cache
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(SINA_US10Y_URL)
                resp.raise_for_status()
                data = resp.json()
            rows = data.get("result", {}).get("data", [])
            if rows and isinstance(rows[-1], list) and len(rows[-1]) >= 6:
                latest = rows[-1]
                current = self._safe_float(latest[1])
                prev_close = self._safe_float(latest[5])
                if current is not None:
                    change_pct = 0.0
                    if prev_close and prev_close > 0:
                        change_pct = round((current - prev_close) / prev_close * 100, 2)
                    result = {"yield": round(current, 3), "change_pct": change_pct}
                    self._us10y_cache = result
                    self._us10y_cache_ts = now
                    return result
        except Exception as e:
            logger.warning("新浪 10Y 国债获取失败: %s", e)
        return self._us10y_cache or {}

    # ── 新浪黄金期货 (COMEX) ──

    async def _fetch_gold_sina(self) -> dict:
        """从新浪财经获取 COMEX 黄金主力合约价格。

        hf_GC 返回格式：var hq_str_hf_GC="当前价,,开盘,最高,...,昨收,...";
        """
        now = time.monotonic()
        if self._gold_cache and (now - self._gold_cache_ts) < _CACHE_TTL:
            return self._gold_cache
        try:
            async with httpx.AsyncClient(timeout=10, headers=_SINA_HQ_HEADERS) as client:
                resp = await client.get(SINA_GOLD_URL)
                resp.raise_for_status()
                text = resp.text
            parts = text.split('"')
            if len(parts) >= 2:
                fields = parts[1].split(",")
                if len(fields) >= 8:
                    price = self._safe_float(fields[0])
                    prev_close = self._safe_float(fields[7])
                    if price and price > 0:
                        change_pct = 0.0
                        if prev_close and prev_close > 0:
                            change_pct = round((price - prev_close) / prev_close * 100, 2)
                        result = {"price": round(price, 2), "change_pct": change_pct}
                        self._gold_cache = result
                        self._gold_cache_ts = now
                        return result
        except Exception as e:
            logger.warning("新浪黄金获取失败: %s", e)
        return self._gold_cache or {}

    # ── 原有采集方法 ──

    async def _fetch_ifnews(self) -> dict[str, dict]:
        now = time.monotonic()
        if self._ifnews_cache and (now - self._ifnews_cache_ts) < _CACHE_TTL:
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
                change_pct = self._safe_float(item.get("priceLimit"))
                if price is not None:
                    results[internal_name] = {
                        "price": round(price, 2),
                        "change_pct": round(change_pct, 2) if change_pct is not None else 0.0,
                    }

            if results:
                self._ifnews_cache = results
                self._ifnews_cache_ts = now

            return results

        except Exception as e:
            logger.warning("ifnews 全球指数获取失败: %s", e)
            return self._ifnews_cache or {}

    @staticmethod
    def _safe_float(val) -> float | None:
        if val is None:
            return None
        try:
            return float(str(val).replace(",", "").replace("%", ""))
        except (ValueError, TypeError):
            return None

    async def _fetch_fear_greed(self) -> dict:
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
        return {"flow_usd": None, "trend": "unknown"}
