"""期权数据采集器。

从 Deribit（全球最大加密期权交易所）获取：
- 期权链持仓量分布
- Max Pain 计算
- Put/Call Ratio
- IV Rank（隐含波动率百分位）

Deribit 公共 API 免费无需 Key。
"""

from __future__ import annotations

import logging

import ccxt.async_support as ccxt

from config.schema import ExchangeConfig
from core.interfaces import DataCollector
from core.models import OptionsData

logger = logging.getLogger(__name__)


class OptionsCollector(DataCollector):
    """期权数据采集器（基于 Deribit / OKX）"""

    def __init__(self, config: ExchangeConfig):
        self._config = config
        self._exchange: ccxt.Exchange | None = None

    @property
    def name(self) -> str:
        return "options"

    async def initialize(self) -> None:
        exchange_id = self._config.options_source
        exchange_class = getattr(ccxt, exchange_id, None)
        if exchange_class is None:
            logger.warning("期权数据源 %s 不可用，跳过", exchange_id)
            return
        self._exchange = exchange_class({"enableRateLimit": True})
        logger.info("期权采集器初始化: %s", exchange_id)

    async def cleanup(self) -> None:
        if self._exchange:
            await self._exchange.close()

    async def collect(self, symbol: str, snapshot_data: dict) -> dict:
        if not self._exchange:
            return snapshot_data

        base_currency = symbol.split("/")[0]  # "BTC"
        try:
            options_data = await self._fetch_options_data(base_currency)
            snapshot_data["options"] = options_data
        except Exception as e:
            logger.warning("期权数据采集失败: %s", e)

        return snapshot_data

    async def _fetch_options_data(self, base: str) -> OptionsData:
        """获取期权链数据并计算 Max Pain"""
        ex = self._exchange
        assert ex is not None

        # 加载期权市场信息
        await ex.load_markets()

        # 筛选出该币种的期权合约
        option_markets = [
            m for m in ex.markets.values()
            if m.get("option") and m.get("base") == base
        ]

        if not option_markets:
            return OptionsData()

        # 按到期日分组，取最近一期
        by_expiry: dict[str, list] = {}
        for m in option_markets:
            expiry = m.get("expiry", "")
            if expiry:
                by_expiry.setdefault(expiry, []).append(m)

        if not by_expiry:
            return OptionsData()

        nearest_expiry = sorted(by_expiry.keys())[0]
        nearest_options = by_expiry[nearest_expiry]

        # 获取各行权价的 OI
        call_oi: dict[float, float] = {}
        put_oi: dict[float, float] = {}
        total_call_oi = 0.0
        total_put_oi = 0.0
        oi_success = 0
        oi_fail = 0

        for opt in nearest_options:
            raw_strike = opt.get("strike")
            if raw_strike is None:
                continue
            try:
                strike = float(raw_strike)
            except (TypeError, ValueError):
                continue
            opt_type = opt.get("optionType", "")
            opt_symbol = opt.get("symbol", "")
            if not strike or not opt_symbol:
                continue

            try:
                oi_data = await ex.fetch_open_interest(opt_symbol)
                oi_val = float(oi_data.get("openInterest") or 0)
                if opt_type == "call":
                    call_oi[strike] = oi_val
                    total_call_oi += oi_val
                elif opt_type == "put":
                    put_oi[strike] = oi_val
                    total_put_oi += oi_val
                oi_success += 1
            except Exception:
                oi_fail += 1
                continue

        # 计算 Max Pain
        max_pain = self._calculate_max_pain(call_oi, put_oi)

        # 找出 OI 峰值行权价（取前 3）
        call_peaks = sorted(call_oi, key=call_oi.get, reverse=True)[:3]  # type: ignore
        put_peaks = sorted(put_oi, key=put_oi.get, reverse=True)[:3]  # type: ignore

        pcr = (total_put_oi / total_call_oi) if total_call_oi > 0 else 1.0

        # 诊断日志：数据质量
        total_attempts = oi_success + oi_fail
        if total_attempts > 0 and oi_success / total_attempts < 0.5:
            logger.warning(
                "期权OI采集成功率低: %d/%d (%.0f%%), %s %s",
                oi_success, total_attempts,
                oi_success / total_attempts * 100, base, nearest_expiry,
            )
        if pcr == 1.0 and total_call_oi > 0:
            logger.warning("期权PCR恰好1.0 (put=%.0f, call=%.0f)，数据可能异常", total_put_oi, total_call_oi)
        if call_peaks and put_peaks and call_peaks == put_peaks:
            logger.warning("期权Call/Put OI峰值完全相同: %s，数据质量存疑", call_peaks)

        logger.debug(
            "期权采集完成: %s 到期=%s 合约=%d OI成功=%d/%d PCR=%.4f call_peaks=%s put_peaks=%s",
            base, nearest_expiry, len(nearest_options), oi_success, total_attempts,
            pcr, call_peaks, put_peaks,
        )

        return OptionsData(
            max_pain=max_pain,
            max_pain_distance_pct=0.0,
            nearest_expiry=nearest_expiry,
            call_oi_peaks=call_peaks,
            put_oi_peaks=put_peaks,
            put_call_ratio=round(pcr, 4),
            iv_rank=None,  # 尚无 IV 历史数据，由评分层按 None 处理
        )

    @staticmethod
    def _calculate_max_pain(
        call_oi: dict[float, float], put_oi: dict[float, float]
    ) -> float | None:
        """计算最大痛点：使所有期权买方总亏损最大的价格。

        遍历每个行权价作为到期价格，计算在该价格下
        所有 Call 和 Put 买方的总损失，取总损失最大的行权价。
        """
        all_strikes = sorted(set(list(call_oi.keys()) + list(put_oi.keys())))
        if not all_strikes:
            return None

        max_pain_strike = all_strikes[0]
        max_total_loss = 0.0

        for settle_price in all_strikes:
            total_loss = 0.0
            # Call 买方在 settle_price 下的损失
            for strike, oi in call_oi.items():
                if settle_price > strike:
                    # Call in the money: 买方盈利，不计入痛点损失
                    pass
                else:
                    # Call out of money: 买方损失全部权利金（以 OI 代替）
                    total_loss += oi

            # Put 买方在 settle_price 下的损失
            for strike, oi in put_oi.items():
                if settle_price < strike:
                    pass
                else:
                    total_loss += oi

            if total_loss > max_total_loss:
                max_total_loss = total_loss
                max_pain_strike = settle_price

        return max_pain_strike
