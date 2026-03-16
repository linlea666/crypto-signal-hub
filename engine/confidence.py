"""信心度计算。

信心度衡量的不是评分高低，而是各维度信号的一致性。
多个独立维度同时指向同一方向 = 高信心度（共振）。
信号矛盾 = 低信心度（观望）。
"""

from __future__ import annotations

from core.constants import Direction
from core.models import FactorScore


def calculate_confidence(factor_scores: list[FactorScore]) -> float:
    """计算信号信心度（0-100%）。

    算法：
    1. 统计各因子的方向
    2. 计算多数方向占比
    3. 用加权一致性分数得出信心度

    Returns:
        0-100 的信心度百分比
    """
    if not factor_scores:
        return 0.0

    # 过滤掉中性因子（不参与信心度计算）
    directional = [fs for fs in factor_scores if fs.direction != Direction.NEUTRAL]
    if not directional:
        return 30.0  # 全部中性 = 低信心度

    total_factors = len(directional)
    bullish_count = sum(1 for fs in directional if fs.direction == Direction.BULLISH)
    bearish_count = total_factors - bullish_count
    majority_count = max(bullish_count, bearish_count)

    # 基础一致性：多数方向占比（如 5/7 = 71%）
    basic_consistency = (majority_count / total_factors) * 100

    # 加权一致性：强信号的一致性更重要
    # 每个因子的 |normalized score| 作为权重
    weighted_bull = sum(abs(fs.normalized) for fs in directional if fs.direction == Direction.BULLISH)
    weighted_bear = sum(abs(fs.normalized) for fs in directional if fs.direction == Direction.BEARISH)
    total_weight = weighted_bull + weighted_bear

    if total_weight > 0:
        weighted_consistency = (max(weighted_bull, weighted_bear) / total_weight) * 100
    else:
        weighted_consistency = basic_consistency

    # 综合信心度 = 基础一致性 25% + 加权一致性 75%
    # 强信号的方向比弱信号的数量更重要
    confidence = basic_consistency * 0.25 + weighted_consistency * 0.75

    # 因子数量不足时降低信心度
    all_count = len(factor_scores)
    if all_count < 4:
        confidence *= 0.7  # 数据不全则打折

    return min(100.0, max(0.0, confidence))
