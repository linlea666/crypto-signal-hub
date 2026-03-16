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

        # ── 散户账户多空比（反向指标，±10 分）──
        ratio = ls.account_ratio
        if ratio > 2.0:
            score = -10
            details_parts.append(f"散户多空比{ratio:.2f}(极度做多=反向做空)")
        elif ratio > 1.5:
            score = -6
            details_parts.append(f"散户多空比{ratio:.2f}(偏多)")
        elif ratio > 1.2:
            score = -3
            details_parts.append(f"散户多空比{ratio:.2f}(略偏多)")
        elif ratio < 0.5:
            score = 10
            details_parts.append(f"散户多空比{ratio:.2f}(极度做空=反向做多)")
        elif ratio < 0.8:
            score = 6
            details_parts.append(f"散户多空比{ratio:.2f}(偏空)")
        elif ratio < 0.9:
            score = 3
            details_parts.append(f"散户多空比{ratio:.2f}(略偏空)")
        else:
            details_parts.append(f"散户多空比{ratio:.2f}(均衡)")

        # ── 大户多空比（同向确认/背离警示，±3 分）──
        top = ls.top_trader_ratio
        if top != ratio:
            if top > 1.5:
                score -= 3
                details_parts.append(f"大户多空比{top:.2f}(偏多→反向)")
            elif top < 0.7:
                score += 3
                details_parts.append(f"大户多空比{top:.2f}(偏空→反向)")
            else:
                details_parts.append(f"大户多空比{top:.2f}")

        # ── Taker 买卖量比辅助（±2 分）──
        taker = ls.taker_buy_sell_ratio
        if taker > 1.15:
            score += 2
            details_parts.append(f"主动买入主导({taker:.2f})")
        elif taker < 0.85:
            score -= 2
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
