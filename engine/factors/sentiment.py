"""情绪与链上数据评分因子。

评估维度：
- 加密恐惧贪婪指数（极端值为反向指标）
- 爆仓分布数据（流动性磁铁）
"""

from core.constants import Direction, FactorName
from core.interfaces import ScoreFactor
from core.models import FactorScore, MarketSnapshot


class SentimentFactor(ScoreFactor):
    """情绪因子（满分 ±15）"""

    def __init__(self, max_score_val: float = 15.0):
        self._max = max_score_val

    @property
    def name(self) -> str:
        return FactorName.SENTIMENT

    @property
    def max_score(self) -> float:
        return self._max

    def calculate(self, snapshot: MarketSnapshot) -> FactorScore:
        score = 0.0
        details_parts: list[str] = []

        # ── 恐惧贪婪指数（反向指标，±10 分）──
        fg_value = snapshot.macro.fear_greed_value if snapshot.macro else None
        if fg_value is not None:
            fg_label = snapshot.macro.fear_greed_label if snapshot.macro else ""
            if fg_value <= 15:
                score += 10
                details_parts.append(f"恐惧贪婪={fg_value}({fg_label},极度恐惧=反向做多)")
            elif fg_value <= 25:
                score += 5
                details_parts.append(f"恐惧贪婪={fg_value}({fg_label},恐惧)")
            elif fg_value >= 85:
                score -= 10
                details_parts.append(f"恐惧贪婪={fg_value}({fg_label},极度贪婪=反向做空)")
            elif fg_value >= 75:
                score -= 5
                details_parts.append(f"恐惧贪婪={fg_value}({fg_label},贪婪)")
            else:
                details_parts.append(f"恐惧贪婪={fg_value}({fg_label})")
        else:
            details_parts.append("恐惧贪婪数据不可用")

        # ── 爆仓分布（Phase 2 补充，预留评分位）──
        # 未来接入 CoinAnk / Coinglass 爆仓热力图数据
        # 上方空头清算多 → 价格可能向上猎杀 → +5
        # 下方多头清算多 → 价格可能向下猎杀 → -5

        score = max(-self._max, min(self._max, score))
        direction = Direction.BULLISH if score > 0 else (
            Direction.BEARISH if score < 0 else Direction.NEUTRAL
        )
        return FactorScore(
            name=FactorName.SENTIMENT,
            score=round(score, 1),
            max_score=self._max,
            direction=direction,
            details="; ".join(details_parts),
        )
