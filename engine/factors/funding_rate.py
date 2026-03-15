"""资金费率评分因子。

核心逻辑（反向指标）：
- 费率极高 → 多头过于拥挤 → 看跌信号
- 费率极低 → 空头过于拥挤 → 看涨信号
- 资金费率是真金白银的成本，造假成本极高，可靠度最高的单一指标之一
"""

from core.constants import Direction, FactorName, FundingRateLevel
from core.interfaces import ScoreFactor
from core.models import FactorScore, MarketSnapshot


class FundingRateFactor(ScoreFactor):
    """资金费率因子（满分 ±15）"""

    def __init__(self, max_score_val: float = 15.0):
        self._max = max_score_val

    @property
    def name(self) -> str:
        return FactorName.FUNDING_RATE

    @property
    def max_score(self) -> float:
        return self._max

    def calculate(self, snapshot: MarketSnapshot) -> FactorScore:
        fr = snapshot.derivatives.funding_rate
        score = 0.0
        details_parts: list[str] = []

        rate_display = f"{fr.average * 100:.4f}%"
        rates_info = ", ".join(f"{k}:{v * 100:.4f}%" for k, v in fr.rates.items())

        # ── 资金费率等级评分（反向指标）──
        level_scores = {
            FundingRateLevel.EXTREME_HIGH: -15,  # 极端看多=反向做空
            FundingRateLevel.HIGH: -8,
            FundingRateLevel.NORMAL: 0,
            FundingRateLevel.LOW: 5,
            FundingRateLevel.EXTREME_LOW: 15,    # 极端看空=反向做多
        }
        score = float(level_scores.get(fr.level, 0))
        details_parts.append(f"费率均值{rate_display}({fr.level.value})")

        if rates_info:
            details_parts.append(f"各所: {rates_info}")

        # ── 两所费率差异异常检测（±2 分修正）──
        if len(fr.rates) >= 2:
            rates_list = list(fr.rates.values())
            spread = abs(rates_list[0] - rates_list[1])
            if spread > 0.0003:
                details_parts.append(f"两所费率差异{spread * 100:.4f}%(异常)")

        score = max(-self._max, min(self._max, score))
        direction = Direction.BULLISH if score > 0 else (
            Direction.BEARISH if score < 0 else Direction.NEUTRAL
        )
        return FactorScore(
            name=FactorName.FUNDING_RATE,
            score=round(score, 1),
            max_score=self._max,
            direction=direction,
            details="; ".join(details_parts),
        )
