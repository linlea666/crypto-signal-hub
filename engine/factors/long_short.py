"""多空比 + 主动买卖量评分因子。

核心逻辑（反向指标）：
- 散户极度看多（比值高）→ 反向做空
- 散户极度看空（比值低）→ 反向做多
- Taker 主动买卖量比作为辅助确认
"""

from core.constants import Direction, FactorName
from core.interfaces import ScoreFactor
from core.models import FactorScore, MarketSnapshot


class LongShortFactor(ScoreFactor):
    """多空比因子（满分 ±15）"""

    def __init__(self, max_score_val: float = 15.0):
        self._max = max_score_val

    @property
    def name(self) -> str:
        return FactorName.LONG_SHORT_RATIO

    @property
    def max_score(self) -> float:
        return self._max

    def calculate(self, snapshot: MarketSnapshot) -> FactorScore:
        ls = snapshot.derivatives.long_short
        score = 0.0
        details_parts: list[str] = []

        # ── 账户多空比评分（反向指标，主体 ±12 分）──
        ratio = ls.account_ratio
        if ratio > 2.0:
            score = -12
            details_parts.append(f"多空比{ratio:.2f}(散户极度做多=反向做空)")
        elif ratio > 1.5:
            score = -6
            details_parts.append(f"多空比{ratio:.2f}(偏多,警惕)")
        elif ratio < 0.5:
            score = 12
            details_parts.append(f"多空比{ratio:.2f}(散户极度做空=反向做多)")
        elif ratio < 0.8:
            score = 6
            details_parts.append(f"多空比{ratio:.2f}(偏空)")
        else:
            details_parts.append(f"多空比{ratio:.2f}(均衡)")

        # ── Taker 买卖量比辅助（±3 分）──
        taker = ls.taker_buy_sell_ratio
        if taker > 1.1:
            score += 3
            details_parts.append(f"主动买入主导({taker:.2f})")
        elif taker < 0.9:
            score -= 3
            details_parts.append(f"主动卖出主导({taker:.2f})")

        score = max(-self._max, min(self._max, score))
        direction = Direction.BULLISH if score > 0 else (
            Direction.BEARISH if score < 0 else Direction.NEUTRAL
        )
        return FactorScore(
            name=FactorName.LONG_SHORT_RATIO,
            score=round(score, 1),
            max_score=self._max,
            direction=direction,
            details="; ".join(details_parts),
        )
