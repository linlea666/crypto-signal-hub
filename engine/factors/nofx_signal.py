"""NOFX 外部信号交叉验证因子。

评估维度（满分 ±10）：
- AI300 量化信号（±5 分）：S/A 看多/看空共振加分，C/D 背离减分
- 机构资金净流（±3 分）：机构大量流入看多，流出看空
- 订单簿 Delta（±2 分）：买盘显著强于卖盘看多，反之看空

NOFX 数据不可用时返回 0 分，不影响其他因子。
"""

from core.constants import Direction, FactorName
from core.interfaces import ScoreFactor
from core.models import FactorScore, MarketSnapshot


class NofxSignalFactor(ScoreFactor):
    """NOFX 交叉验证因子（满分 ±10）"""

    def __init__(self, max_score_val: float = 10.0):
        self._max = max_score_val

    @property
    def name(self) -> str:
        return FactorName.NOFX_SIGNAL

    @property
    def max_score(self) -> float:
        return self._max

    def calculate(self, snapshot: MarketSnapshot) -> FactorScore:
        nofx = snapshot.nofx
        if nofx is None:
            return FactorScore(
                name=FactorName.NOFX_SIGNAL,
                score=0,
                max_score=self._max,
                direction=Direction.NEUTRAL,
                details="NOFX 数据不可用",
            )

        score = 0.0
        details_parts: list[str] = []

        # ── AI300 量化信号（±5 分）──
        signal = nofx.ai300_signal
        direction = nofx.ai300_direction
        if signal and direction:
            signal_weight = {"S": 5, "A": 4, "B": 2, "C": -1, "D": -2}.get(signal, 0)
            if direction == "long":
                score += signal_weight
            elif direction == "short":
                score -= signal_weight
            grade_label = f"AI300={signal}({direction})"
            if nofx.ai300_rank > 0:
                grade_label += f" 排名#{nofx.ai300_rank}"
            details_parts.append(grade_label)
        else:
            details_parts.append("AI300 无信号")

        # ── 机构资金净流（±3 分）──
        inst = nofx.netflow_inst
        if abs(inst) > 0:
            if inst > 1_000_000:
                score += 3
                details_parts.append(f"机构净流入${inst / 1e6:.1f}M(+3)")
            elif inst > 100_000:
                score += 1
                details_parts.append(f"机构净流入${inst / 1e6:.1f}M(+1)")
            elif inst < -1_000_000:
                score -= 3
                details_parts.append(f"机构净流出${inst / 1e6:.1f}M(-3)")
            elif inst < -100_000:
                score -= 1
                details_parts.append(f"机构净流出${inst / 1e6:.1f}M(-1)")
            else:
                details_parts.append(f"机构净流${inst / 1e6:.1f}M")
        else:
            details_parts.append("资金流数据不可用")

        # ── 订单簿 Delta（±2 分）──
        delta = nofx.heatmap_delta
        bid = nofx.heatmap_bid_total
        ask = nofx.heatmap_ask_total
        if bid > 0 and ask > 0:
            ratio = bid / ask
            if ratio > 1.3:
                score += 2
                details_parts.append(f"挂单偏买({ratio:.2f}x,+2)")
            elif ratio > 1.1:
                score += 1
                details_parts.append(f"挂单偏买({ratio:.2f}x,+1)")
            elif ratio < 0.7:
                score -= 2
                details_parts.append(f"挂单偏卖({ratio:.2f}x,-2)")
            elif ratio < 0.9:
                score -= 1
                details_parts.append(f"挂单偏卖({ratio:.2f}x,-1)")
            else:
                details_parts.append(f"挂单均衡({ratio:.2f}x)")
        elif delta != 0:
            if delta > 0:
                score += 1
                details_parts.append(f"Delta>0(买方偏强)")
            else:
                score -= 1
                details_parts.append(f"Delta<0(卖方偏强)")

        # ── 查询热度（信息项，不评分）──
        if nofx.query_rank > 0:
            details_parts.append(f"查询热度#{nofx.query_rank}")

        score = max(-self._max, min(self._max, score))
        dir_val = Direction.BULLISH if score > 0 else (
            Direction.BEARISH if score < 0 else Direction.NEUTRAL
        )
        return FactorScore(
            name=FactorName.NOFX_SIGNAL,
            score=round(score, 1),
            max_score=self._max,
            direction=dir_val,
            details="; ".join(details_parts),
        )
