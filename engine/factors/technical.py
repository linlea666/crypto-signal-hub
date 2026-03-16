"""技术面趋势评分因子。

8 个评估维度（总分 ±20，从现有 K 线纯计算，零额外 API）：
- MA 均线排列与价格位置（±8 分）—— 趋势方向基础
- RSI 超买超卖（±2 分）—— 动能极端修正
- K 线结构：高低点趋势（±2 分）—— 价格结构确认
- VWAP 位置（±1 分）—— 成交量加权公允价
- 量价配合（±2 分）—— 趋势可信度
- MACD 动量（±2 分）—— 金叉/死叉 + 零轴位置
- 布林带位置（±2 分）—— 波动率与价格极端
- 日线收盘强度（±1 分）—— 趋势确认信号
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

        # ── 1. MA 趋势评分（±8 分）──
        ma_direction = 0
        if tech.ma20 and tech.ma60:
            if tech.ma20 > tech.ma60:
                if price > tech.ma20:
                    score += 8
                    ma_direction = 1
                    details_parts.append("MA20>MA60且价格在上方(强多头+8)")
                else:
                    score += 4
                    ma_direction = 1
                    details_parts.append("MA20>MA60但价格回落(弱多头+4)")
            else:
                if price < tech.ma20:
                    score -= 8
                    ma_direction = -1
                    details_parts.append("MA20<MA60且价格在下方(强空头-8)")
                else:
                    score -= 4
                    ma_direction = -1
                    details_parts.append("MA20<MA60但价格反弹(弱空头-4)")
        else:
            details_parts.append("MA数据不足")

        # ── 2. RSI 修正（±2 分）──
        if tech.rsi_4h is not None:
            if tech.rsi_4h > 80:
                score -= 2
                details_parts.append(f"RSI={tech.rsi_4h:.0f}(超买-2)")
            elif tech.rsi_4h > 70:
                score -= 1
                details_parts.append(f"RSI={tech.rsi_4h:.0f}(偏超买-1)")
            elif tech.rsi_4h < 20:
                score += 2
                details_parts.append(f"RSI={tech.rsi_4h:.0f}(超卖+2)")
            elif tech.rsi_4h < 30:
                score += 1
                details_parts.append(f"RSI={tech.rsi_4h:.0f}(偏超卖+1)")
            else:
                details_parts.append(f"RSI={tech.rsi_4h:.0f}")

        # ── 3. K 线结构（±2 分）──
        if tech.structure == "higher_highs":
            score += 2
            details_parts.append("高点递增+2")
        elif tech.structure == "lower_lows":
            score -= 2
            details_parts.append("低点递降-2")

        # ── 4. VWAP 位置（±1 分）──
        if tech.vwap and price > 0:
            vwap_dist = (price - tech.vwap) / tech.vwap
            if vwap_dist > 0.005:
                score += 1
                details_parts.append(f"价格在VWAP上方{vwap_dist:.1%}(+1)")
            elif vwap_dist < -0.005:
                score -= 1
                details_parts.append(f"价格在VWAP下方{vwap_dist:.1%}(-1)")
            else:
                details_parts.append("价格贴近VWAP")

        # ── 5. 量价配合（±2 分）──
        if tech.volume_ratio is not None:
            vr = tech.volume_ratio
            if vr > 1.5 and ma_direction != 0:
                s = 2 * ma_direction
                score += s
                label = "多头" if ma_direction > 0 else "空头"
                details_parts.append(f"放量{vr:.1f}x确认{label}({s:+d})")
            elif vr < 0.5 and ma_direction != 0:
                s = -1 * ma_direction  # 缩量削弱方向
                score += s
                label = "多头乏力" if ma_direction > 0 else "空头乏力"
                details_parts.append(f"缩量{vr:.1f}x{label}({s:+d})")

        # ── 6. MACD 动量（±2 分）──
        if tech.macd_histogram is not None:
            hist = tech.macd_histogram
            cross = tech.macd_cross
            if cross == "golden":
                score += 2
                details_parts.append(f"MACD金叉(柱状{hist:+.1f},+2)")
            elif cross == "death":
                score -= 2
                details_parts.append(f"MACD死叉(柱状{hist:+.1f},-2)")
            elif hist > 0:
                score += 1
                details_parts.append(f"MACD多头动量(柱状{hist:+.1f},+1)")
            elif hist < 0:
                score -= 1
                details_parts.append(f"MACD空头动量(柱状{hist:+.1f},-1)")

        # ── 7. 布林带位置（±2 分）──
        if tech.bb_percent is not None:
            bb = tech.bb_percent
            if bb > 1.0:
                # 突破上轨：强势突破 or 超买，结合趋势判断
                if ma_direction > 0:
                    score += 2
                    details_parts.append(f"突破布林上轨%B={bb:.2f}(强势+2)")
                else:
                    score -= 1
                    details_parts.append(f"触及布林上轨%B={bb:.2f}(超买-1)")
            elif bb < 0.0:
                # 跌破下轨：弱势破位 or 超卖
                if ma_direction < 0:
                    score -= 2
                    details_parts.append(f"跌破布林下轨%B={bb:.2f}(弱势-2)")
                else:
                    score += 1
                    details_parts.append(f"触及布林下轨%B={bb:.2f}(超卖+1)")
            elif 0.4 <= bb <= 0.6:
                details_parts.append(f"布林中轨附近%B={bb:.2f}")

        # ── 8. 日线收盘强度（±1 分，趋势确认） ──
        if tech.daily_close_strength is not None:
            dcs = tech.daily_close_strength
            if dcs > 0.7 and ma_direction > 0:
                score += 1
                details_parts.append(f"日线收盘偏强{dcs:.2f}(确认多头+1)")
            elif dcs < 0.3 and ma_direction < 0:
                score -= 1
                details_parts.append(f"日线收盘偏弱{dcs:.2f}(确认空头-1)")
            else:
                details_parts.append(f"日线收盘强度{dcs:.2f}")

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
