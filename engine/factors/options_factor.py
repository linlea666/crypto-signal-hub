"""期权数据评分因子。

评估维度：
- Max Pain 与当前价格的距离和方向
- Put/Call Ratio 极端值（反向指标）
- IV Rank（波动率水平）
- 临近到期权重放大

期权市场以机构为主，是"聪明钱"的信号。
"""

from core.constants import Direction, FactorName
from core.interfaces import ScoreFactor
from core.models import FactorScore, MarketSnapshot


class OptionsFactor(ScoreFactor):
    """期权数据因子（满分 ±20）"""

    def __init__(self, max_score_val: float = 20.0):
        self._max = max_score_val

    @property
    def name(self) -> str:
        return FactorName.OPTIONS

    @property
    def max_score(self) -> float:
        return self._max

    def calculate(self, snapshot: MarketSnapshot) -> FactorScore:
        opts = snapshot.options
        if opts is None or opts.max_pain is None:
            return FactorScore(
                name=FactorName.OPTIONS,
                score=0,
                max_score=self._max,
                direction=Direction.NEUTRAL,
                details="期权数据不可用",
            )

        score = 0.0
        details_parts: list[str] = []
        price = snapshot.price.current

        # ── Max Pain 方向评分（±10 分）──
        # 当前价低于 Max Pain → 可能被拉上去（看多）
        # 当前价高于 Max Pain → 可能被拉下来（看空）
        if price > 0 and opts.max_pain:
            distance_pct = ((price - opts.max_pain) / price) * 100
            if distance_pct > 3:
                score -= 10
                details_parts.append(
                    f"当前价高于MaxPain({opts.max_pain:.0f}){distance_pct:.1f}%,到期前可能回调"
                )
            elif distance_pct > 1:
                score -= 5
                details_parts.append(
                    f"当前价略高于MaxPain({opts.max_pain:.0f}){distance_pct:.1f}%"
                )
            elif distance_pct < -3:
                score += 10
                details_parts.append(
                    f"当前价低于MaxPain({opts.max_pain:.0f}){abs(distance_pct):.1f}%,到期前可能上涨"
                )
            elif distance_pct < -1:
                score += 5
                details_parts.append(
                    f"当前价略低于MaxPain({opts.max_pain:.0f}){abs(distance_pct):.1f}%"
                )
            else:
                details_parts.append(f"当前价接近MaxPain({opts.max_pain:.0f})")

        # ── PCR 评分（反向指标，±5 分）──
        pcr = opts.put_call_ratio
        if pcr > 1.3:
            score += 5
            details_parts.append(f"PCR={pcr:.2f}(看跌情绪过重=反向做多)")
        elif pcr < 0.6:
            score -= 5
            details_parts.append(f"PCR={pcr:.2f}(看涨情绪过重=反向做空)")
        else:
            details_parts.append(f"PCR={pcr:.2f}(正常)")

        # ── IV Rank 评分（±3 分，辅助）──
        if opts.iv_rank < 20:
            details_parts.append(f"IV偏低({opts.iv_rank:.0f}%),可能酝酿大行情")
        elif opts.iv_rank > 80:
            details_parts.append(f"IV偏高({opts.iv_rank:.0f}%),市场预期高波动")

        # ── 期权 OI 峰值作为关键位信息（不加分，但记录）──
        if opts.call_oi_peaks:
            details_parts.append(f"Call OI密集: {[f'{p:.0f}' for p in opts.call_oi_peaks[:2]]}")
        if opts.put_oi_peaks:
            details_parts.append(f"Put OI密集: {[f'{p:.0f}' for p in opts.put_oi_peaks[:2]]}")

        score = max(-self._max, min(self._max, score))
        direction = Direction.BULLISH if score > 0 else (
            Direction.BEARISH if score < 0 else Direction.NEUTRAL
        )
        return FactorScore(
            name=FactorName.OPTIONS,
            score=round(score, 1),
            max_score=self._max,
            direction=direction,
            details="; ".join(details_parts),
        )
