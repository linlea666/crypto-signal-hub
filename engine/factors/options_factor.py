"""期权数据评分因子。

评估维度：
- Max Pain 与当前价格的距离和方向（按到期日加权）
- Put/Call Ratio 极端值（反向指标）
- IV Rank（波动率水平）

期权市场以机构为主，是"聪明钱"的信号。
到期前 7 天 Max Pain 磁吸效应最强，距到期越远权重越低。
"""

from __future__ import annotations

from datetime import datetime

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

        # ── Max Pain 方向评分（±10 分，按到期日加权）──
        if price > 0 and opts.max_pain:
            distance_pct = ((price - opts.max_pain) / price) * 100
            # 到期日权重：7 天内 1.0，14 天 0.6，21 天 0.3，>30 天 0.15
            expiry_weight = _expiry_weight(opts.nearest_expiry)
            raw_mp = 0
            if distance_pct > 3:
                raw_mp = -10
            elif distance_pct > 1:
                raw_mp = -5
            elif distance_pct < -3:
                raw_mp = 10
            elif distance_pct < -1:
                raw_mp = 5

            mp_score = round(raw_mp * expiry_weight)
            score += mp_score
            exp_label = f"权重{expiry_weight:.0%}" if expiry_weight < 1.0 else "到期临近"
            if raw_mp != 0:
                details_parts.append(
                    f"MaxPain={opts.max_pain:.0f},距{distance_pct:+.1f}%,{exp_label}({mp_score:+d})"
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


def _expiry_weight(nearest_expiry: str) -> float:
    """按距到期日天数计算 Max Pain 权重（到期越近越强）。"""
    if not nearest_expiry:
        return 0.5
    try:
        exp_date = datetime.strptime(nearest_expiry[:10], "%Y-%m-%d")
        days = (exp_date - datetime.utcnow()).days
        if days <= 0:
            return 1.0
        if days <= 7:
            return 1.0
        if days <= 14:
            return 0.6
        if days <= 21:
            return 0.3
        return 0.15
    except (ValueError, TypeError):
        return 0.5
