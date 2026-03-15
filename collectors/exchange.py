"""交易所数据采集器（CCXT 主干 + httpx 精准补充）。

数据获取策略：
- CCXT：价格、K 线、资金费率等级、OI、多空比、挂单簿（统一抽象层）
- httpx 直调 Binance klines：获取 CCXT 丢弃的 taker 买入量（K 线第 9 字段）
- httpx 直调 OKX funding-rate：获取 CCXT 丢弃的 premium（期现基差）

纯计算（零额外 API）：VWAP 和量价比从现有 K 线数据推导。
"""

from __future__ import annotations

import logging

import ccxt.async_support as ccxt
import httpx
import pandas as pd
import ta

from config.schema import ExchangeConfig
from core.constants import Direction, FundingRateLevel, OIPriceSignal
from core.interfaces import DataCollector
from core.models import (
    FundingRateData,
    LongShortData,
    OpenInterestData,
    PriceData,
    TechnicalData,
)

logger = logging.getLogger(__name__)


class ExchangeCollector(DataCollector):
    """基于 CCXT 的交易所数据采集器。

    同时从主交易所和辅交易所获取数据，用于交叉验证。
    """

    def __init__(self, config: ExchangeConfig):
        self._config = config
        self._primary: ccxt.Exchange | None = None
        self._secondary: ccxt.Exchange | None = None

    @property
    def name(self) -> str:
        return "exchange"

    async def initialize(self) -> None:
        self._primary = self._create_exchange(self._config.primary)
        self._secondary = self._create_exchange(self._config.secondary)
        logger.info(
            "交易所采集器初始化: primary=%s, secondary=%s",
            self._config.primary, self._config.secondary,
        )

    async def cleanup(self) -> None:
        for ex in (self._primary, self._secondary):
            if ex:
                await ex.close()

    async def collect(self, symbol: str, snapshot_data: dict) -> dict:
        if not self._primary:
            raise RuntimeError("交易所采集器未初始化")

        # 1. 价格和 K 线数据 → 技术指标
        price_data, technical_data = await self._fetch_price_and_technical(symbol)
        snapshot_data["price"] = price_data
        snapshot_data["technical"] = technical_data

        # 2. 资金费率（从两个交易所获取）
        snapshot_data["funding_rate"] = await self._fetch_funding_rates(symbol)

        # 3. 持仓量
        snapshot_data["open_interest"] = await self._fetch_open_interest(
            symbol, price_data.change_pct_24h
        )

        # 4. 多空比（币安独有接口更丰富）
        snapshot_data["long_short"] = await self._fetch_long_short(symbol)

        # 5. 挂单簿深度密集区
        snapshot_data["orderbook_clusters"] = await self._fetch_orderbook_clusters(
            symbol, price_data.current
        )

        return snapshot_data

    # ── 私有方法 ──

    def _create_exchange(self, exchange_id: str) -> ccxt.Exchange:
        exchange_class = getattr(ccxt, exchange_id, None)
        if exchange_class is None:
            raise ValueError(f"不支持的交易所: {exchange_id}")
        return exchange_class({"enableRateLimit": True})

    async def _fetch_price_and_technical(
        self, symbol: str
    ) -> tuple[PriceData, TechnicalData]:
        """获取价格快照和基于 K 线的技术指标"""
        ex = self._primary
        assert ex is not None

        # 获取 Ticker
        ticker = await ex.fetch_ticker(symbol)
        price = PriceData(
            current=ticker.get("last", 0) or 0,
            high_24h=ticker.get("high", 0) or 0,
            low_24h=ticker.get("low", 0) or 0,
            change_pct_24h=ticker.get("percentage", 0) or 0,
            volume_24h=ticker.get("quoteVolume", 0) or 0,
        )

        # 获取 4h K 线用于技术分析
        ohlcv = await ex.fetch_ohlcv(symbol, timeframe="4h", limit=60)
        technical = self._calculate_technical(ohlcv)

        return price, technical

    def _calculate_technical(self, ohlcv: list) -> TechnicalData:
        """基于 K 线数据计算技术指标"""
        if not ohlcv or len(ohlcv) < 20:
            return TechnicalData()

        df = pd.DataFrame(ohlcv, columns=["ts", "open", "high", "low", "close", "volume"])

        close = df["close"]
        highs = df["high"]
        lows = df["low"]
        ma20 = close.rolling(window=20).mean().iloc[-1]
        ma60_val = close.rolling(window=min(60, len(close))).mean().iloc[-1] if len(close) >= 30 else None

        current_price = close.iloc[-1]
        if ma60_val and ma20 > ma60_val and current_price > ma20:
            ma_trend = Direction.BULLISH
        elif ma60_val and ma20 < ma60_val and current_price < ma20:
            ma_trend = Direction.BEARISH
        else:
            ma_trend = Direction.NEUTRAL

        rsi_series = ta.momentum.rsi(close, window=14)
        rsi_val = rsi_series.iloc[-1] if not rsi_series.empty else None

        recent = close.tail(10).values
        structure = "range"
        if len(recent) >= 4:
            highs_rising = recent[-1] > recent[-3] and recent[-2] > recent[-4]
            lows_falling = recent[-1] < recent[-3] and recent[-2] < recent[-4]
            if highs_rising:
                structure = "higher_highs"
            elif lows_falling:
                structure = "lower_lows"

        swing_h = self._find_swing_points(highs.values, mode="high")
        swing_l = self._find_swing_points(lows.values, mode="low")

        # ── VWAP：成交量加权平均价（从 K 线纯计算，无额外 API） ──
        vwap_val = self._calculate_vwap(df)

        # ── 量价比：最近 1 根 K 线量 / 近 20 根均量 ──
        volume_ratio = self._calculate_volume_ratio(df["volume"])

        # ── MACD：动量方向与金叉/死叉（ta 库计算） ──
        macd_hist, macd_cross = self._calculate_macd(close)

        # ── 布林带 %B：价格在带内的位置（ta 库计算） ──
        bb_pct = self._calculate_bollinger(close)

        return TechnicalData(
            ma20=round(ma20, 2),
            ma60=round(ma60_val, 2) if ma60_val else None,
            ma_trend=ma_trend,
            rsi_4h=round(rsi_val, 2) if rsi_val and pd.notna(rsi_val) else None,
            structure=structure,
            swing_highs=[round(v, 2) for v in swing_h[:5]],
            swing_lows=[round(v, 2) for v in swing_l[:5]],
            vwap=vwap_val,
            volume_ratio=volume_ratio,
            macd_histogram=macd_hist,
            macd_cross=macd_cross,
            bb_percent=bb_pct,
        )

    @staticmethod
    def _calculate_vwap(df: pd.DataFrame) -> float | None:
        """VWAP = Σ(典型价 × 成交量) / Σ(成交量)，使用近 20 根 K 线"""
        if len(df) < 5:
            return None
        recent = df.tail(20).copy()
        typical_price = (recent["high"] + recent["low"] + recent["close"]) / 3
        vol = recent["volume"]
        total_vol = vol.sum()
        if total_vol <= 0:
            return None
        return round(float((typical_price * vol).sum() / total_vol), 2)

    @staticmethod
    def _calculate_volume_ratio(volume_series: pd.Series) -> float | None:
        """当前 K 线量 / 近 20 根均量，>1.5=放量，<0.5=缩量"""
        if len(volume_series) < 5:
            return None
        avg_vol = volume_series.tail(20).mean()
        if avg_vol <= 0:
            return None
        return round(float(volume_series.iloc[-1] / avg_vol), 2)

    @staticmethod
    def _calculate_macd(close: pd.Series) -> tuple[float | None, str]:
        """计算 MACD 柱状图值及金叉/死叉状态。

        返回 (histogram, cross_type):
        - histogram: 正=多头动量，负=空头动量
        - cross_type: "golden"=金叉（MACD上穿信号线），
                      "death"=死叉（MACD下穿信号线），
                      "none"=无交叉
        """
        if len(close) < 35:  # MACD(12,26,9) 至少需要 ~34 根 K 线
            return None, "none"

        macd_indicator = ta.trend.MACD(close, window_slow=26, window_fast=12, window_sign=9)
        hist = macd_indicator.macd_diff()

        if hist.empty or len(hist) < 2:
            return None, "none"

        current = hist.iloc[-1]
        previous = hist.iloc[-2]

        if pd.isna(current) or pd.isna(previous):
            return None, "none"

        # 柱状图由负转正 = 金叉，由正转负 = 死叉
        cross = "none"
        if previous <= 0 < current:
            cross = "golden"
        elif previous >= 0 > current:
            cross = "death"

        return round(float(current), 2), cross

    @staticmethod
    def _calculate_bollinger(close: pd.Series) -> float | None:
        """计算布林带 %B 指标。

        %B = (价格 - 下轨) / (上轨 - 下轨)
        >1 = 突破上轨（超买/强势突破）
        <0 = 跌破下轨（超卖/弱势破位）
        0.5 = 中轨附近
        """
        if len(close) < 20:
            return None

        bb = ta.volatility.BollingerBands(close, window=20, window_dev=2)
        upper = bb.bollinger_hband().iloc[-1]
        lower = bb.bollinger_lband().iloc[-1]

        if pd.isna(upper) or pd.isna(lower) or upper == lower:
            return None

        price = close.iloc[-1]
        return round(float((price - lower) / (upper - lower)), 3)

    @staticmethod
    def _find_swing_points(arr, mode="high", window=3):
        """从价格序列中找出局部极值（swing high/low）"""
        points = []
        if len(arr) < window * 2 + 1:
            return points
        for i in range(window, len(arr) - window):
            segment = arr[i - window: i + window + 1]
            if mode == "high" and arr[i] == max(segment):
                points.append(float(arr[i]))
            elif mode == "low" and arr[i] == min(segment):
                points.append(float(arr[i]))
        points.sort(reverse=(mode == "high"))
        return points

    async def _fetch_funding_rates(self, symbol: str) -> FundingRateData:
        """从主/辅交易所获取资金费率并计算均值。

        同时通过 OKX REST API 获取 premium（期现基差），
        这是 CCXT 标准化时丢弃的字段。
        """
        swap_symbol = symbol.replace("/USDT", "/USDT:USDT")
        rates: dict[str, float] = {}

        for label, ex in [("primary", self._primary), ("secondary", self._secondary)]:
            if ex is None:
                continue
            try:
                result = await ex.fetch_funding_rate(swap_symbol)
                rate = result.get("fundingRate")
                if rate is not None:
                    rates[ex.id] = float(rate)
            except Exception as e:
                logger.warning("获取 %s 资金费率失败 (%s): %s", ex.id, symbol, e)

        if not rates:
            return FundingRateData()

        avg = sum(rates.values()) / len(rates)
        level = self._classify_funding_rate(avg)

        # 期现基差：OKX funding-rate 接口的 premium 字段（CCXT 丢弃了该字段）
        basis = await self._fetch_basis_from_okx(symbol)

        return FundingRateData(
            rates=rates, average=avg, level=level, basis_rate=basis,
        )

    @staticmethod
    def _classify_funding_rate(rate: float) -> FundingRateLevel:
        if rate > 0.001:
            return FundingRateLevel.EXTREME_HIGH
        if rate > 0.0005:
            return FundingRateLevel.HIGH
        if rate < -0.001:
            return FundingRateLevel.EXTREME_LOW
        if rate < -0.0005:
            return FundingRateLevel.LOW
        return FundingRateLevel.NORMAL

    async def _fetch_open_interest(
        self, symbol: str, price_change_pct: float
    ) -> OpenInterestData:
        """获取持仓量并结合价格变动判断信号"""
        ex = self._primary
        assert ex is not None

        swap_symbol = symbol.replace("/USDT", "/USDT:USDT")
        try:
            oi = await ex.fetch_open_interest(swap_symbol)
            oi_usd = float(oi.get("openInterestValue", 0) or 0)

            # 获取历史 OI 计算变化率（简化：用 OI 历史接口或记忆上次值）
            # MVP 阶段通过 CCXT 的 OI history 估算
            oi_change = 0.0
            try:
                oi_history = await ex.fetch_open_interest_history(
                    swap_symbol, timeframe="1h", limit=25
                )
                if oi_history and len(oi_history) >= 2:
                    old_val = float(oi_history[0].get("openInterestValue", 0) or 0)
                    if old_val > 0:
                        oi_change = ((oi_usd - old_val) / old_val) * 100
            except Exception:
                pass  # 部分交易所不支持 OI 历史

            signal = self._classify_oi_price(oi_change, price_change_pct)
            return OpenInterestData(
                total_usd=oi_usd,
                change_pct_24h=round(oi_change, 2),
                price_oi_signal=signal,
            )
        except Exception as e:
            logger.warning("获取 OI 失败 (%s): %s", symbol, e)
            return OpenInterestData()

    @staticmethod
    def _classify_oi_price(oi_change: float, price_change: float) -> OIPriceSignal:
        if price_change > 0 and oi_change > 0:
            return OIPriceSignal.NEW_LONGS
        if price_change > 0 and oi_change <= 0:
            return OIPriceSignal.SHORT_COVERING
        if price_change <= 0 and oi_change > 0:
            return OIPriceSignal.NEW_SHORTS
        return OIPriceSignal.LONG_LIQUIDATION

    async def _fetch_long_short(self, symbol: str) -> LongShortData:
        """获取多空比 + 真实 taker 买卖比。

        多空比：通过 CCXT 从 Binance 获取。
        Taker 买卖比：通过 httpx 直调 Binance klines API 获取
        第 9 字段（taker_buy_base_asset_volume），这是 CCXT 丢弃的数据。
        """
        ex = self._secondary or self._primary
        assert ex is not None

        swap_symbol = symbol.replace("/USDT", "/USDT:USDT")
        account_ratio = 1.0
        top_ratio = 1.0

        try:
            ls_data = await ex.fetch_long_short_ratio_history(
                swap_symbol, timeframe="1h", limit=1
            )
            if ls_data:
                latest = ls_data[-1]
                ratio_val = float(latest.get("longShortRatio", 1.0))
                account_ratio = ratio_val
                top_ratio = ratio_val
        except Exception as e:
            logger.warning("获取多空比失败 (%s): %s", symbol, e)

        # 真实 taker 买卖比：Binance K 线第 9 字段
        taker_ratio = await self._fetch_taker_ratio_from_binance(symbol)

        return LongShortData(
            account_ratio=round(account_ratio, 4),
            top_trader_ratio=round(top_ratio, 4),
            taker_buy_sell_ratio=round(taker_ratio, 4),
        )

    async def _fetch_orderbook_clusters(
        self, symbol: str, current_price: float
    ) -> dict:
        """从挂单簿中找出大额挂单密集区（不需要 API key）"""
        ex = self._primary
        assert ex is not None
        result = {"bid_clusters": [], "ask_clusters": []}

        if current_price <= 0:
            return result

        try:
            ob = await ex.fetch_order_book(symbol, limit=50)
            bids = ob.get("bids", [])
            asks = ob.get("asks", [])

            range_pct = 0.05
            lo = current_price * (1 - range_pct)
            hi = current_price * (1 + range_pct)

            bid_volumes: dict[float, float] = {}
            for entry in bids:
                price, vol = float(entry[0]), float(entry[1])
                if price < lo:
                    break
                bucket = round(price / 100) * 100 if current_price > 5000 else round(price / 10) * 10
                bid_volumes[bucket] = bid_volumes.get(bucket, 0) + price * vol

            ask_volumes: dict[float, float] = {}
            for entry in asks:
                price, vol = float(entry[0]), float(entry[1])
                if price > hi:
                    break
                bucket = round(price / 100) * 100 if current_price > 5000 else round(price / 10) * 10
                ask_volumes[bucket] = ask_volumes.get(bucket, 0) + price * vol

            if bid_volumes:
                avg_bid = sum(bid_volumes.values()) / len(bid_volumes)
                result["bid_clusters"] = sorted(
                    [p for p, v in bid_volumes.items() if v > avg_bid * 1.5],
                    reverse=True,
                )[:3]

            if ask_volumes:
                avg_ask = sum(ask_volumes.values()) / len(ask_volumes)
                result["ask_clusters"] = sorted(
                    [p for p, v in ask_volumes.items() if v > avg_ask * 1.5],
                )[:3]

        except Exception as e:
            logger.warning("挂单簿深度分析失败 (%s): %s", symbol, e)

        return result

    # ══════════════════════════════════════════════
    # httpx 精准补充：获取 CCXT 丢弃的字段
    # ══════════════════════════════════════════════

    async def _fetch_taker_ratio_from_binance(self, symbol: str) -> float:
        """从 Binance K 线获取真实 taker 买卖比。

        Binance klines 返回 12 个字段，第 5 字段是总成交量，
        第 9 字段（index=9）是 taker_buy_base_asset_volume。
        taker_buy_ratio = taker_buy_volume / total_volume。
        >0.5 表示主动买入占优，<0.5 表示主动卖出占优。
        使用近 6 根 4h K 线（24 小时）的聚合值。
        """
        # BTC/USDT → BTCUSDT
        bn_symbol = symbol.replace("/", "")
        url = "https://api.binance.com/api/v3/klines"
        params = {"symbol": bn_symbol, "interval": "4h", "limit": 6}

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(url, params=params)
                resp.raise_for_status()
                klines = resp.json()

            total_vol = sum(float(k[5]) for k in klines)
            taker_buy_vol = sum(float(k[9]) for k in klines)
            if total_vol <= 0:
                return 1.0
            taker_sell_vol = total_vol - taker_buy_vol
            # 返回 买/卖 比值（与 LongShortData.taker_buy_sell_ratio 语义一致）
            if taker_sell_vol <= 0:
                return 2.0
            return round(taker_buy_vol / taker_sell_vol, 4)
        except Exception as e:
            logger.warning("Binance taker 买卖比获取失败 (%s): %s", symbol, e)
            return 1.0

    async def _fetch_basis_from_okx(self, symbol: str) -> float:
        """从 OKX funding-rate 接口获取 premium（期现基差）。

        OKX 返回的 premium 字段是 CCXT 丢弃的，公式：
        [max(0, 买价-指数价) - max(0, 指数价-卖价)] / 指数价
        正值=合约升水（看多偏向），负值=贴水（看空偏向）。
        """
        # BTC/USDT → BTC-USDT-SWAP
        base = symbol.split("/")[0]
        inst_id = f"{base}-USDT-SWAP"
        url = "https://www.okx.com/api/v5/public/funding-rate"
        params = {"instId": inst_id}

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(url, params=params)
                resp.raise_for_status()
                data = resp.json()

            items = data.get("data", [])
            if items:
                premium_str = items[0].get("premium", "0")
                return round(float(premium_str), 6) if premium_str else 0.0
            return 0.0
        except Exception as e:
            logger.warning("OKX 期现基差获取失败 (%s): %s", symbol, e)
            return 0.0
