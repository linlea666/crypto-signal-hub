"""情绪与链上数据评分因子。

评估维度（满分 ±15）：
- 加密恐惧贪婪指数（反向指标，±5 分，从 ±10 降权避免持续恐惧锁死多头）
- 爆仓分布数据（±5 分，预留 Phase 2）
- 剩余 ±5 分预留给链上数据

降权理由：回测显示 FG 长期 20-30 时每小时给 +5~+10 多头，
导致 69% 信号为 bullish。降至 ±5 后，单一情绪指标不再有足够权重
覆盖其他因子的空头信号。
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

        # ── 恐惧贪婪指数（反向指标，±5 分，降权后避免锁死偏向）──
        fg_value = snapshot.macro.fear_greed_value if snapshot.macro else None
        if fg_value is not None:
            fg_label = snapshot.macro.fear_greed_label if snapshot.macro else ""
            if fg_value <= 15:
                score += 5
                details_parts.append(f"恐惧贪婪={fg_value}({fg_label},极度恐惧=反向做多+5)")
            elif fg_value <= 25:
                score += 3
                details_parts.append(f"恐惧贪婪={fg_value}({fg_label},恐惧+3)")
            elif fg_value <= 40:
                score += 1
                details_parts.append(f"恐惧贪婪={fg_value}({fg_label},偏恐惧+1)")
            elif fg_value >= 85:
                score -= 5
                details_parts.append(f"恐惧贪婪={fg_value}({fg_label},极度贪婪=反向做空-5)")
            elif fg_value >= 75:
                score -= 3
                details_parts.append(f"恐惧贪婪={fg_value}({fg_label},贪婪-3)")
            elif fg_value >= 60:
                score -= 1
                details_parts.append(f"恐惧贪婪={fg_value}({fg_label},偏贪婪-1)")
            else:
                details_parts.append(f"恐惧贪婪={fg_value}({fg_label})")
        else:
            details_parts.append("恐惧贪婪数据不可用")

        # ── 爆仓分布（Phase 2 补充，预留 ±5 评分位）──
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
