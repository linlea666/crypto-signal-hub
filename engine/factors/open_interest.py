"""持仓量 (OI) + 价格配合评分因子。

核心逻辑：
- 价涨 + OI涨 → 新多头入场，趋势强劲 → 看多
- 价涨 + OI跌 → 空头平仓推动，动力弱 → 弱看多
- 价跌 + OI涨 → 新空头入场，下跌趋势强 → 看空
- 价跌 + OI跌 → 多头平仓推动，动力弱 → 弱看空
"""

from core.constants import Direction, FactorName, OIPriceSignal
from core.interfaces import ScoreFactor
from core.models import FactorScore, MarketSnapshot


class OpenInterestFactor(ScoreFactor):
    """OI + 价格组合因子（满分 ±15）"""

    def __init__(self, max_score_val: float = 15.0):
        self._max = max_score_val

    @property
    def name(self) -> str:
        return FactorName.OPEN_INTEREST

    @property
    def max_score(self) -> float:
        return self._max

    def calculate(self, snapshot: MarketSnapshot) -> FactorScore:
        oi = snapshot.derivatives.open_interest
        score = 0.0
        details_parts: list[str] = []

        signal_labels = {
            OIPriceSignal.NEW_LONGS: "价涨+OI涨=新多头入场",
            OIPriceSignal.SHORT_COVERING: "价涨+OI跌=空头平仓",
            OIPriceSignal.NEW_SHORTS: "价跌+OI涨=新空头入场",
            OIPriceSignal.LONG_LIQUIDATION: "价跌+OI跌=多头平仓",
        }
        details_parts.append(signal_labels.get(oi.price_oi_signal, "信号不明"))

        # 按 OI 变化幅度分级评分（替代固定满分）
        oi_abs = abs(oi.change_pct_24h)
        if oi_abs < 1.0:
            magnitude = 0.0  # 微波动不计入
        elif oi_abs < 3.0:
            magnitude = 5.0
        elif oi_abs < 10.0:
            magnitude = 10.0
        else:
            magnitude = 15.0

        strong_signals = {OIPriceSignal.NEW_LONGS, OIPriceSignal.NEW_SHORTS}
        if oi.price_oi_signal in strong_signals:
            base = magnitude
        else:
            base = min(magnitude * 0.4, 5.0)

        if oi.price_oi_signal in (OIPriceSignal.NEW_LONGS, OIPriceSignal.SHORT_COVERING):
            score = base
        else:
            score = -base

        strength_label = "弱" if oi_abs < 3 else ("中" if oi_abs < 10 else "强")
        details_parts.append(f"OI变化{oi.change_pct_24h:+.1f}%({strength_label})")

        if oi.total_usd > 0:
            oi_display = f"${oi.total_usd / 1e9:.2f}B" if oi.total_usd > 1e9 else f"${oi.total_usd / 1e6:.0f}M"
            details_parts.append(f"OI总量{oi_display}")

        score = max(-self._max, min(self._max, score))
        direction = Direction.BULLISH if score > 0 else (
            Direction.BEARISH if score < 0 else Direction.NEUTRAL
        )
        return FactorScore(
            name=FactorName.OPEN_INTEREST,
            score=round(score, 1),
            max_score=self._max,
            direction=direction,
            details="; ".join(details_parts),
        )
