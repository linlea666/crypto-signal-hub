"""NOFX 外部数据采集器。

对接 nofxos.ai API，采集：
- AI300 量化信号（资金流 AI 模型 S/A/B/C/D 分级）
- 资金净流（机构/散户分离）
- 订单簿热力图（深度 Delta、大单密集区）
- 社区查询热度排名

所有接口仅读取公开市场数据，需 API Key 认证。
若 API 不可用自动降级返回空数据，不影响其他采集器。
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from config.schema import NofxConfig
from core.interfaces import DataCollector
from core.models import NofxData

logger = logging.getLogger(__name__)

_TIMEOUT = 10


class NofxCollector(DataCollector):
    """NOFX API 数据采集器（含缓存与降级）"""

    def __init__(self, config: NofxConfig):
        self._config = config
        self._cache: dict[str, tuple[float, Any]] = {}

    @property
    def name(self) -> str:
        return "nofx"

    def update_config(self, config: NofxConfig) -> None:
        self._config = config

    async def collect(self, symbol: str, snapshot_data: dict) -> dict:
        if not self._config.enabled or not self._config.api_key:
            return snapshot_data

        coin = _symbol_to_coin(symbol)
        ai300 = await self._fetch_cached("ai300", "/api/ai300/list")
        netflow_top = await self._fetch_cached("netflow_top", "/api/netflow/top-ranking")
        netflow_low = await self._fetch_cached("netflow_low", "/api/netflow/low-ranking")
        heatmap = await self._fetch_cached("heatmap", "/api/heatmap/list")
        query_rank = await self._fetch_cached("query_rank", "/api/query-rank/list")

        snapshot_data["nofx"] = _build_nofx_data(
            coin, ai300, netflow_top, netflow_low, heatmap, query_rank,
        )
        return snapshot_data

    async def _fetch_cached(self, cache_key: str, path: str) -> list[dict]:
        now = time.monotonic()
        cached = self._cache.get(cache_key)
        if cached and (now - cached[0]) < self._config.cache_ttl:
            return cached[1]

        data = await self._api_get(path)
        self._cache[cache_key] = (now, data)
        return data

    async def _api_get(self, path: str) -> list[dict]:
        url = f"{self._config.base_url.rstrip('/')}{path}"
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.get(url, params={"auth": self._config.api_key})
                resp.raise_for_status()
                body = resp.json()
                if isinstance(body, list):
                    return body
                # API 返回 {"success":true,"data":{"heatmaps":[...]}} 嵌套结构
                data = body.get("data", body.get("list", []))
                if isinstance(data, list):
                    return data
                # data 是 dict 时，取第一个包含列表的值
                if isinstance(data, dict):
                    for v in data.values():
                        if isinstance(v, list):
                            return v
                return []
        except Exception as e:
            logger.warning("NOFX API %s 请求失败: %s", path, e)
            return []


def _symbol_to_coin(symbol: str) -> str:
    """BTC/USDT → BTC, ETH/USDT → ETH"""
    return symbol.split("/")[0].upper()


def _find_coin(items: list[dict], coin: str) -> dict | None:
    for item in items:
        sym = (item.get("symbol") or item.get("coin") or "").upper()
        if sym == coin or sym.startswith(coin):
            return item
    return None


def _build_nofx_data(
    coin: str,
    ai300: list[dict],
    netflow_top: list[dict],
    netflow_low: list[dict],
    heatmap: list[dict],
    query_rank: list[dict],
) -> NofxData:
    # AI300
    ai_signal, ai_dir, ai_rank = "", "", 0
    ai_item = _find_coin(ai300, coin)
    if ai_item:
        ai_signal = str(ai_item.get("signal", ai_item.get("grade", ""))).upper()
        ai_dir = str(ai_item.get("direction", "")).lower()
        ai_rank = int(ai_item.get("rank", 0) or 0)

    # Netflow
    nf_total, nf_inst, nf_retail = 0.0, 0.0, 0.0
    nf_item = _find_coin(netflow_top, coin) or _find_coin(netflow_low, coin)
    if nf_item:
        nf_total = float(nf_item.get("netflow", nf_item.get("totalNetflow", 0)) or 0)
        nf_inst = float(nf_item.get("institutionNetflow", nf_item.get("instNetflow", 0)) or 0)
        nf_retail = float(nf_item.get("retailNetflow", nf_item.get("personalNetflow", 0)) or 0)

    # Heatmap
    bid_total, ask_total, delta = 0.0, 0.0, 0.0
    large_bids: list[float] = []
    large_asks: list[float] = []
    hm_item = _find_coin(heatmap, coin)
    if hm_item:
        bid_total = float(hm_item.get("bid_volume", hm_item.get("bidTotal", hm_item.get("bid_total", 0))) or 0)
        ask_total = float(hm_item.get("ask_volume", hm_item.get("askTotal", hm_item.get("ask_total", 0))) or 0)
        delta = float(hm_item.get("delta", 0) or 0)
        for b in (hm_item.get("largeBids") or hm_item.get("large_bids") or [])[:5]:
            if isinstance(b, (int, float)):
                large_bids.append(float(b))
            elif isinstance(b, dict):
                large_bids.append(float(b.get("price", 0)))
        for a in (hm_item.get("largeAsks") or hm_item.get("large_asks") or [])[:5]:
            if isinstance(a, (int, float)):
                large_asks.append(float(a))
            elif isinstance(a, dict):
                large_asks.append(float(a.get("price", 0)))

    # Query Rank
    qr = 0
    for i, item in enumerate(query_rank):
        sym = (item.get("symbol") or item.get("coin") or "").upper()
        if sym == coin or sym.startswith(coin):
            qr = i + 1
            break

    return NofxData(
        ai300_signal=ai_signal,
        ai300_direction=ai_dir,
        ai300_rank=ai_rank,
        netflow_total=nf_total,
        netflow_inst=nf_inst,
        netflow_retail=nf_retail,
        heatmap_bid_total=bid_total,
        heatmap_ask_total=ask_total,
        heatmap_delta=delta,
        heatmap_large_bids=large_bids,
        heatmap_large_asks=large_asks,
        query_rank=qr,
    )
