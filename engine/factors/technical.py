"""技术面趋势评分因子。

评估维度：
- MA 均线排列与价格位置（±10 分）
- RSI 超买超卖（±3 分）
- K 线结构：高低点趋势（±2 分）
- VWAP 位置：价格相对成交量加权价（±2 分）
- 量价配合：放量/缩量对趋势的确认或背离（±3 分）
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
        price = snapshot.price.current
        score = 0.0
        details_parts: list[str] = []

        # ── 1. MA 趋势评分（±10 分）──
        ma_direction = 0  # 记录 MA 判断的方向，供量价配合使用
        if tech.ma20 and tech.ma60:
            if tech.ma20 > tech.ma60:
                if price > tech.ma20:
                    score += 10
                    ma_direction = 1
                    details_parts.append("MA20>MA60且价格在上方(强多头+10)")
                else:
                    score += 5
                    ma_direction = 1
                    details_parts.append("MA20>MA60但价格回落(弱多头+5)")
            else:
                if price < tech.ma20:
                    score -= 10
                    ma_direction = -1
                    details_parts.append("MA20<MA60且价格在下方(强空头-10)")
                else:
                    score -= 5
                    ma_direction = -1
                    details_parts.append("MA20<MA60但价格反弹(弱空头-5)")
        else:
            details_parts.append("MA数据不足")

        # ── 2. RSI 修正（±3 分）──
        if tech.rsi_4h is not None:
            if tech.rsi_4h > 80:
                score -= 3
                details_parts.append(f"RSI={tech.rsi_4h:.0f}(超买-3)")
            elif tech.rsi_4h < 20:
                score += 3
                details_parts.append(f"RSI={tech.rsi_4h:.0f}(超卖+3)")
            else:
                details_parts.append(f"RSI={tech.rsi_4h:.0f}")

        # ── 3. K 线结构（±2 分）──
        if tech.structure == "higher_highs":
            score += 2
            details_parts.append("高点递增+2")
        elif tech.structure == "lower_lows":
            score -= 2
            details_parts.append("低点递降-2")

        # ── 4. VWAP 位置（±2 分）──
        if tech.vwap and price > 0:
            vwap_dist = (price - tech.vwap) / tech.vwap
            if vwap_dist > 0.005:
                score += 2
                details_parts.append(f"价格在VWAP上方{vwap_dist:.1%}(+2)")
            elif vwap_dist < -0.005:
                score -= 2
                details_parts.append(f"价格在VWAP下方{vwap_dist:.1%}(-2)")
            else:
                details_parts.append("价格贴近VWAP")

        # ── 5. 量价配合（±3 分）──
        if tech.volume_ratio is not None:
            vr = tech.volume_ratio
            if vr > 1.5:
                # 放量：确认当前趋势方向
                if ma_direction > 0:
                    score += 3
                    details_parts.append(f"放量{vr:.1f}x确认多头(+3)")
                elif ma_direction < 0:
                    score -= 3
                    details_parts.append(f"放量{vr:.1f}x确认空头(-3)")
                else:
                    details_parts.append(f"放量{vr:.1f}x(方向不明)")
            elif vr < 0.5:
                # 缩量：当前趋势可能衰竭，削弱方向得分
                if ma_direction > 0:
                    score -= 2
                    details_parts.append(f"缩量{vr:.1f}x多头乏力(-2)")
                elif ma_direction < 0:
                    score += 2
                    details_parts.append(f"缩量{vr:.1f}x空头乏力(+2)")

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
