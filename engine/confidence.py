"""信心度计算。

信心度衡量的不是评分高低，而是各维度信号的一致性。
多个独立维度同时指向同一方向 = 高信心度（共振）。
信号矛盾 = 低信心度（观望）。
"""

from __future__ import annotations

from datetime import datetime, timezone

from core.constants import Direction
from core.models import FactorScore, UpcomingEvent


def calculate_confidence(
    factor_scores: list[FactorScore],
    events: list[UpcomingEvent] | None = None,
) -> float:
    """计算信号信心度（0-100%）。

    算法：
    1. 统计各因子的方向
    2. 计算多数方向占比
    3. 用加权一致性分数得出信心度
    4. 重大事件临近时衰减（24h 内高影响事件 × 0.85）

    Returns:
        0-100 的信心度百分比
    """
    if not factor_scores:
        return 0.0

    directional = [fs for fs in factor_scores if fs.direction != Direction.NEUTRAL]
    if not directional:
        return 30.0

    total_factors = len(directional)
    bullish_count = sum(1 for fs in directional if fs.direction == Direction.BULLISH)
    bearish_count = total_factors - bullish_count
    majority_count = max(bullish_count, bearish_count)

    basic_consistency = (majority_count / total_factors) * 100

    weighted_bull = sum(abs(fs.normalized) for fs in directional if fs.direction == Direction.BULLISH)
    weighted_bear = sum(abs(fs.normalized) for fs in directional if fs.direction == Direction.BEARISH)
    total_weight = weighted_bull + weighted_bear

    if total_weight > 0:
        weighted_consistency = (max(weighted_bull, weighted_bear) / total_weight) * 100
    else:
        weighted_consistency = basic_consistency

    confidence = basic_consistency * 0.25 + weighted_consistency * 0.75

    # 方向性因子过少时惩罚：避免 1 个因子有方向就得 100% 信心度
    if len(directional) < 3:
        confidence *= 0.4 + len(directional) * 0.2  # 1→0.6x, 2→0.8x

    all_count = len(factor_scores)
    if all_count < 4:
        confidence *= 0.7

    # 重大事件临近衰减（×0.90，宏观因子已有独立扣分，此处仅做置信度维度轻度抑制）
    if events:
        now = datetime.now(timezone.utc)
        for evt in events:
            if evt.impact != "high":
                continue
            evt_time = evt.time if evt.time.tzinfo else evt.time.replace(tzinfo=timezone.utc)
            hours_away = (evt_time - now).total_seconds() / 3600
            if 0 <= hours_away <= 24:
                confidence *= 0.90
                break

    return min(100.0, max(0.0, confidence))
