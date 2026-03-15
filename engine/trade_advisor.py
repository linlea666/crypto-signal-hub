"""交易建议推导器（纯计算，无 IO）。

基于评分引擎输出的方向、信心度、关键价位，
自动推导开仓区间、止损、止盈和盈亏比。

核心原则：小亏大赚
- 止损放在最近的强支撑/阻力位外侧（方向相反的位置）
- 止盈对准阻力/支撑位（方向一致的位置）
- 盈亏比 < 1.5 时不建议开仓

所有计算均为纯函数，不依赖外部状态。
"""

from __future__ import annotations

from core.constants import Direction, MIN_RISK_REWARD_RATIO, PositionSize
from core.models import KeyLevel, KeyLevels, PriceData, TradeSuggestion


def derive_trade_suggestion(
    direction: Direction,
    confidence: float,
    price: PriceData,
    levels: KeyLevels,
) -> TradeSuggestion | None:
    """从信号方向和关键价位推导交易建议。

    Args:
        direction: 评分引擎判定的方向
        confidence: 信心度 (0-100)
        price: 当前价格快照
        levels: 已排序的关键价位集合

    Returns:
        TradeSuggestion 或 None（中性信号不给建议）
    """
    if direction == Direction.NEUTRAL:
        return None

    current = price.current
    if current <= 0:
        return None

    if direction == Direction.BULLISH:
        return _derive_long(current, confidence, levels)
    return _derive_short(current, confidence, levels)


def _derive_long(
    current: float, confidence: float, levels: KeyLevels,
) -> TradeSuggestion | None:
    """做多建议：止损在支撑下方，止盈在阻力位。"""
    supports = levels.supports       # 已按价格从高到低排序
    resistances = levels.resistances  # 已按价格从低到高排序

    # 止损：取最近的中/强支撑位下方 1%
    sl_level = _find_stop_level(supports, current, side="below")
    if sl_level is None:
        return None
    stop_loss = sl_level.price * 0.99  # 支撑位下方 1% 作为止损

    # 开仓区间：当前价 ~ 最近支撑位（回调买入）
    entry_high = current
    entry_low = supports[0].price if supports else current * 0.99
    entry_mid = (entry_high + entry_low) / 2

    # 止盈：取阻力位（保守=最近，激进=第二个或更远）
    tp1_level, tp2_level = _find_take_profit_levels(resistances, current)
    if tp1_level is None:
        return None

    tp1 = tp1_level.price
    tp2 = tp2_level.price if tp2_level else tp1 * 1.02

    # 盈亏比计算（以入场中点为基准）
    risk = entry_mid - stop_loss
    if risk <= 0:
        return None
    rr1 = (tp1 - entry_mid) / risk
    rr2 = (tp2 - entry_mid) / risk

    position = _determine_position_size(rr1, confidence)

    return TradeSuggestion(
        direction=Direction.BULLISH,
        entry_low=round(entry_low, 2),
        entry_high=round(entry_high, 2),
        stop_loss=round(stop_loss, 2),
        take_profit_1=round(tp1, 2),
        take_profit_2=round(tp2, 2),
        risk_reward_1=round(rr1, 2),
        risk_reward_2=round(rr2, 2),
        position_size=position,
        sl_source=f"{sl_level.source}下方1%",
        tp1_source=tp1_level.source,
        tp2_source=tp2_level.source if tp2_level else "",
        reasoning=_build_reasoning(Direction.BULLISH, rr1, position, confidence),
    )


def _derive_short(
    current: float, confidence: float, levels: KeyLevels,
) -> TradeSuggestion | None:
    """做空建议：止损在阻力上方，止盈在支撑位。"""
    supports = levels.supports
    resistances = levels.resistances

    # 止损：取最近的中/强阻力位上方 1%
    sl_level = _find_stop_level(resistances, current, side="above")
    if sl_level is None:
        return None
    stop_loss = sl_level.price * 1.01

    # 开仓区间：最近阻力位 ~ 当前价（反弹卖出）
    entry_low = current
    entry_high = resistances[0].price if resistances else current * 1.01
    entry_mid = (entry_high + entry_low) / 2

    # 止盈：取支撑位
    tp1_level, tp2_level = _find_take_profit_levels(supports, current)
    if tp1_level is None:
        return None

    tp1 = tp1_level.price
    tp2 = tp2_level.price if tp2_level else tp1 * 0.98

    risk = stop_loss - entry_mid
    if risk <= 0:
        return None
    rr1 = (entry_mid - tp1) / risk
    rr2 = (entry_mid - tp2) / risk

    position = _determine_position_size(rr1, confidence)

    return TradeSuggestion(
        direction=Direction.BEARISH,
        entry_low=round(entry_low, 2),
        entry_high=round(entry_high, 2),
        stop_loss=round(stop_loss, 2),
        take_profit_1=round(tp1, 2),
        take_profit_2=round(tp2, 2),
        risk_reward_1=round(rr1, 2),
        risk_reward_2=round(rr2, 2),
        position_size=position,
        sl_source=f"{sl_level.source}上方1%",
        tp1_source=tp1_level.source,
        tp2_source=tp2_level.source if tp2_level else "",
        reasoning=_build_reasoning(Direction.BEARISH, rr1, position, confidence),
    )


# ── 内部辅助函数 ──

def _find_stop_level(
    levels: list[KeyLevel], current: float, side: str,
) -> KeyLevel | None:
    """找到适合做止损参考的关键位（优先中/强级别）。

    side="below": 找当前价下方的支撑（做多止损参考）
    side="above": 找当前价上方的阻力（做空止损参考）
    """
    strength_priority = {"strong": 3, "medium": 2, "weak": 1}
    candidates = []
    for lv in levels:
        if side == "below" and lv.price < current:
            candidates.append(lv)
        elif side == "above" and lv.price > current:
            candidates.append(lv)

    if not candidates:
        return None

    # 优先选强度 >= medium 且最接近当前价的
    medium_plus = [c for c in candidates if strength_priority.get(c.strength, 0) >= 2]
    if medium_plus:
        return medium_plus[0]  # levels 已按距离排序
    return candidates[0]


def _find_take_profit_levels(
    levels: list[KeyLevel], current: float,
) -> tuple[KeyLevel | None, KeyLevel | None]:
    """从关键位中选出两个止盈目标（保守 + 激进）。

    返回 (tp1_level, tp2_level)，tp2 可能为 None。
    """
    if not levels:
        return None, None
    tp1 = levels[0]
    tp2 = levels[1] if len(levels) >= 2 else None
    return tp1, tp2


def _determine_position_size(risk_reward: float, confidence: float) -> PositionSize:
    """根据盈亏比和信心度决定仓位大小。"""
    if risk_reward < MIN_RISK_REWARD_RATIO:
        return PositionSize.SKIP
    if risk_reward >= 3.0 and confidence >= 75:
        return PositionSize.HEAVY
    if risk_reward >= 2.0:
        return PositionSize.NORMAL
    return PositionSize.LIGHT


_POSITION_LABELS = {
    PositionSize.SKIP: "不建议开仓（盈亏比不足）",
    PositionSize.LIGHT: "轻仓",
    PositionSize.NORMAL: "标准仓位",
    PositionSize.HEAVY: "可适当加仓",
}

_DIRECTION_LABELS = {
    Direction.BULLISH: "做多",
    Direction.BEARISH: "做空",
}


def _build_reasoning(
    direction: Direction, rr: float, position: PositionSize, confidence: float,
) -> str:
    """生成人类可读的交易建议理由。"""
    d = _DIRECTION_LABELS.get(direction, "")
    p = _POSITION_LABELS.get(position, "")
    if position == PositionSize.SKIP:
        return f"信号{d}，但当前盈亏比 {rr:.1f}:1 不足 {MIN_RISK_REWARD_RATIO}:1，建议等待更好入场位。"
    return f"信号{d}，盈亏比 {rr:.1f}:1，信心度 {confidence:.0f}%，建议{p}。"
