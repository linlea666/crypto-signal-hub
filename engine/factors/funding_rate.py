"""资金费率 + 期现基差评分因子。

评估维度：
- 资金费率等级（反向指标，±12 分）：费率极高→多头拥挤→看跌
- 两所费率差异异常（信息项）
- 期现基差 / Premium（±3 分）：升水过高→过热修正，贴水→恐慌修正

资金费率是真金白银的成本，造假成本极高，是最可靠的单一指标之一。
基差提供与费率正交的市场情绪读数（当期 vs 远期定价偏差）。
"""

from core.constants import Direction, FactorName, FundingRateLevel
from core.interfaces import ScoreFactor
from core.models import FactorScore, MarketSnapshot


class FundingRateFactor(ScoreFactor):
    """资金费率 + 基差因子（满分 ±15）"""

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

        # ── 1. 资金费率等级评分（反向指标，±12 分）──
        level_scores = {
            FundingRateLevel.EXTREME_HIGH: -12,
            FundingRateLevel.HIGH: -6,
            FundingRateLevel.NORMAL: 0,
            FundingRateLevel.LOW: 4,
            FundingRateLevel.EXTREME_LOW: 12,
        }
        score = float(level_scores.get(fr.level, 0))
        details_parts.append(f"费率均值{rate_display}({fr.level.value})")

        if rates_info:
            details_parts.append(f"各所: {rates_info}")

        # ── 2. 两所费率差异异常检测（信息项）──
        if len(fr.rates) >= 2:
            rates_list = list(fr.rates.values())
            spread = abs(rates_list[0] - rates_list[1])
            if spread > 0.0003:
                details_parts.append(f"两所差异{spread * 100:.4f}%(异常)")

        # ── 3. 期现基差 / Premium（±3 分）──
        # 正基差（升水）= 合约 > 现货 = 市场偏多情绪
        # 但作为反向指标：升水过高 → 过热 → 扣分；贴水 → 恐慌 → 加分
        basis = fr.basis_rate
        if abs(basis) > 0.0001:
            basis_bps = basis * 10000  # 转为基点方便展示
            if basis > 0.001:
                score -= 3
                details_parts.append(f"基差+{basis_bps:.0f}bp(升水过热-3)")
            elif basis > 0.0003:
                score -= 1
                details_parts.append(f"基差+{basis_bps:.0f}bp(温和升水-1)")
            elif basis < -0.001:
                score += 3
                details_parts.append(f"基差{basis_bps:.0f}bp(贴水恐慌+3)")
            elif basis < -0.0003:
                score += 1
                details_parts.append(f"基差{basis_bps:.0f}bp(温和贴水+1)")

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
