"""技术面趋势评分因子。

评估维度：
- MA 均线排列与价格位置
- RSI 超买超卖
- K 线结构（高低点趋势）
"""

from core.constants import Direction, FactorName
from core.interfaces import ScoreFactor
from core.models import FactorScore, MarketSnapshot


class TechnicalFactor(ScoreFactor):
    """技术面趋势因子（满分 ±20）"""

    def __init__(self, max_score_val: float = 20.0):
        self._max = max_score_val

    @property
    def name(self) -> str:
        return FactorName.TECHNICAL

    @property
    def max_score(self) -> float:
        return self._max

    def calculate(self, snapshot: MarketSnapshot) -> FactorScore:
        tech = snapshot.technical
        score = 0.0
        details_parts: list[str] = []

        # ── MA 趋势评分（最高 ±14 分）──
        if tech.ma20 and tech.ma60:
            price = snapshot.price.current
            if tech.ma20 > tech.ma60:
                if price > tech.ma20:
                    score += 14
                    details_parts.append("MA20>MA60且价格在MA20上方(强多头)")
                else:
                    score += 6
                    details_parts.append("MA20>MA60但价格回落至MA20下方(弱多头)")
            else:
                if price < tech.ma20:
                    score -= 14
                    details_parts.append("MA20<MA60且价格在MA20下方(强空头)")
                else:
                    score -= 6
                    details_parts.append("MA20<MA60但价格反弹至MA20上方(弱空头)")
        else:
            details_parts.append("MA数据不足")

        # ── RSI 修正（±3 分）──
        if tech.rsi_4h is not None:
            if tech.rsi_4h > 80:
                score -= 3
                details_parts.append(f"RSI={tech.rsi_4h:.0f}(超买修正-3)")
            elif tech.rsi_4h < 20:
                score += 3
                details_parts.append(f"RSI={tech.rsi_4h:.0f}(超卖修正+3)")
            else:
                details_parts.append(f"RSI={tech.rsi_4h:.0f}(正常)")

        # ── K 线结构（±3 分）──
        if tech.structure == "higher_highs":
            score += 3
            details_parts.append("K线高点递增")
        elif tech.structure == "lower_lows":
            score -= 3
            details_parts.append("K线低点递降")

        # 限制在 [-max, +max] 范围
        score = max(-self._max, min(self._max, score))

        direction = Direction.BULLISH if score > 0 else (
            Direction.BEARISH if score < 0 else Direction.NEUTRAL
        )
        return FactorScore(
            name=FactorName.TECHNICAL,
            score=round(score, 1),
            max_score=self._max,
            direction=direction,
            details="; ".join(details_parts),
        )
