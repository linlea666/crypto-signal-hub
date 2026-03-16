"""条件挂单策略推导器（纯计算，无 IO）。

基于评分方向、信心度、关键价位，始终生成 2-3 个条件策略：
- 回调做多 (pullback_long)：在支撑位挂买单
- 反弹做空 (bounce_short)：在阻力位挂卖单
- 突破追多 (breakout_long)：在阻力上方设条件单
- 突破追空 (breakout_short)：在支撑下方设条件单

核心原则：小亏大赚
- 每个策略独立计算盈亏比
- R:R < 1.5 时标记为 SKIP 但仍展示（告知用户该价位不划算）
- 根据 market_bias 对策略排序（偏多时回调做多优先）

旧版 derive_trade_suggestion 保留兼容，内部委托给新逻辑。
"""

from __future__ import annotations

from core.constants import Direction, MarketState, MIN_RISK_REWARD_RATIO, PositionSize
from core.models import (
    ConditionalStrategy,
    KeyLevel,
    KeyLevels,
    PriceData,
    TradePlan,
    TradeSuggestion,
)


# ══════════════════════════════════════
# 公开接口
# ══════════════════════════════════════

def derive_trade_plan(
    direction: Direction,
    confidence: float,
    price: PriceData,
    levels: KeyLevels,
    *,
    market_state: MarketState = MarketState.RANGING,
    strategy_mode: str = "adaptive",
) -> TradePlan:
    """始终生成包含多个条件策略的交易计划。

    根据 market_state 和 strategy_mode 对策略类型进行约束：
    - strong_trend / trend_only：只保留顺势策略
    - ranging：双向策略，逆势标注高风险
    - extreme_divergence：允许逆势但降仓位到 LIGHT
    """
    current = price.current
    if current <= 0:
        return TradePlan(
            market_bias=Direction.NEUTRAL,
            immediate_action="价格数据异常，暂停交易",
        )

    strategies: list[ConditionalStrategy] = []

    # 始终尝试生成四种策略
    pullback = _build_pullback_long(current, confidence, levels)
    if pullback:
        strategies.append(pullback)

    bounce = _build_bounce_short(current, confidence, levels)
    if bounce:
        strategies.append(bounce)

    breakout_long = _build_breakout_long(current, confidence, levels)
    if breakout_long:
        strategies.append(breakout_long)

    breakout_short = _build_breakout_short(current, confidence, levels)
    if breakout_short:
        strategies.append(breakout_short)

    # 根据市场状态过滤/约束策略
    strategies = _apply_state_constraints(
        strategies, direction, market_state, strategy_mode,
    )

    # 按 market_bias 排序：偏向方向的策略排前面
    strategies = _sort_by_bias(strategies, direction)

    # 取 top 3
    strategies = strategies[:3]

    # 即时建议
    immediate = _derive_immediate_action(direction, confidence, strategies)

    # 总体说明
    note = _derive_analysis_note(direction, confidence, levels, market_state)

    return TradePlan(
        market_bias=direction,
        immediate_action=immediate,
        strategies=strategies,
        analysis_note=note,
    )


def derive_trade_suggestion(
    direction: Direction,
    confidence: float,
    price: PriceData,
    levels: KeyLevels,
    *,
    plan: TradePlan | None = None,
) -> TradeSuggestion | None:
    """旧版兼容接口，从新版 TradePlan 中提取最优策略转换为 TradeSuggestion。"""
    if plan is None:
        plan = derive_trade_plan(direction, confidence, price, levels)

    # 找到第一个非 SKIP 的策略
    best = None
    for s in plan.strategies:
        if s.position_size != PositionSize.SKIP:
            best = s
            break

    if best is None:
        return None

    return TradeSuggestion(
        direction=Direction.BULLISH if "long" in best.strategy_type else Direction.BEARISH,
        entry_low=best.entry_low,
        entry_high=best.entry_high,
        stop_loss=best.stop_loss,
        take_profit_1=best.take_profit_1,
        take_profit_2=best.take_profit_2,
        risk_reward_1=best.risk_reward,
        risk_reward_2=best.risk_reward,
        position_size=best.position_size,
        sl_source=best.sl_source,
        tp1_source=best.tp1_source,
        tp2_source="",
        reasoning=best.reasoning,
    )


# ══════════════════════════════════════
# 四种条件策略构建
# ══════════════════════════════════════

def _build_pullback_long(
    current: float, confidence: float, levels: KeyLevels,
) -> ConditionalStrategy | None:
    """回调做多：在支撑位挂买单。"""
    supports = levels.supports
    resistances = levels.resistances

    # 触发价 = 最近的中/强支撑
    trigger_level = _find_nearest_level(supports, current, side="below", min_strength=2)
    if trigger_level is None:
        return None

    trigger = trigger_level.price

    # 止损 = 下一个支撑的下方 1%
    sl_level = _find_next_level(supports, trigger, side="below")
    stop_loss = (sl_level.price * 0.99) if sl_level else (trigger * 0.98)

    # 止盈 = 最近的阻力位
    tp1, tp2 = _find_tp_levels(resistances, current)
    if tp1 is None:
        tp1 = current * 1.03
    if tp2 is None:
        tp2 = tp1 * 1.02

    entry_mid = (trigger + current) / 2 if trigger < current else trigger
    risk = entry_mid - stop_loss
    if risk <= 0:
        return None
    rr = (tp1 - entry_mid) / risk

    # 失效条件
    invalidation_price = sl_level.price if sl_level else trigger * 0.98
    invalidation = f"价格跌破${invalidation_price:.0f}则策略失效"

    return ConditionalStrategy(
        strategy_type="pullback_long",
        label="回调做多",
        trigger_price=round(trigger, 2),
        entry_low=round(trigger, 2),
        entry_high=round(current, 2),
        stop_loss=round(stop_loss, 2),
        take_profit_1=round(tp1, 2),
        take_profit_2=round(tp2, 2),
        risk_reward=round(rr, 2),
        position_size=_determine_position_size(rr, confidence),
        sl_source=f"{trigger_level.source}下方1%",
        tp1_source=_level_source_label(resistances, tp1),
        reasoning=f"在{trigger_level.source}支撑位${trigger:.0f}附近挂买单，止损${stop_loss:.0f}",
        valid_hours=24,
        invalidation=invalidation,
    )


def _build_bounce_short(
    current: float, confidence: float, levels: KeyLevels,
) -> ConditionalStrategy | None:
    """反弹做空：在阻力位挂卖单。"""
    supports = levels.supports
    resistances = levels.resistances

    trigger_level = _find_nearest_level(resistances, current, side="above", min_strength=2)
    if trigger_level is None:
        return None

    trigger = trigger_level.price

    # 止损 = 下一个阻力的上方 1%
    sl_level = _find_next_level(resistances, trigger, side="above")
    stop_loss = (sl_level.price * 1.01) if sl_level else (trigger * 1.02)

    # 止盈 = 最近的支撑位
    tp1, tp2 = _find_tp_levels(supports, current)
    if tp1 is None:
        tp1 = current * 0.97
    if tp2 is None:
        tp2 = tp1 * 0.98

    entry_mid = (current + trigger) / 2 if trigger > current else trigger
    risk = stop_loss - entry_mid
    if risk <= 0:
        return None
    rr = (entry_mid - tp1) / risk

    invalidation_price = sl_level.price if sl_level else trigger * 1.02
    invalidation = f"价格放量突破${invalidation_price:.0f}则策略失效"

    return ConditionalStrategy(
        strategy_type="bounce_short",
        label="反弹做空",
        trigger_price=round(trigger, 2),
        entry_low=round(current, 2),
        entry_high=round(trigger, 2),
        stop_loss=round(stop_loss, 2),
        take_profit_1=round(tp1, 2),
        take_profit_2=round(tp2, 2),
        risk_reward=round(rr, 2),
        position_size=_determine_position_size(rr, confidence),
        sl_source=f"{trigger_level.source}上方1%",
        tp1_source=_level_source_label(supports, tp1),
        reasoning=f"在{trigger_level.source}阻力位${trigger:.0f}附近挂卖单，止损${stop_loss:.0f}",
        valid_hours=24,
        invalidation=invalidation,
    )


def _build_breakout_long(
    current: float, confidence: float, levels: KeyLevels,
) -> ConditionalStrategy | None:
    """突破追多：在最近阻力上方设条件买单。"""
    resistances = levels.resistances
    if not resistances:
        return None

    # 找最近的强阻力作为突破目标
    target_level = resistances[0]
    trigger = target_level.price * 1.005  # 阻力上方 0.5% buffer

    # 止损 = 突破阻力位本身（突破后变支撑）
    stop_loss = target_level.price * 0.99

    # 止盈 = 第二个阻力或 trigger + 同等距离
    if len(resistances) >= 2:
        tp1 = resistances[1].price
    else:
        tp1 = trigger + (trigger - stop_loss) * 2
    tp2 = tp1 * 1.02

    risk = trigger - stop_loss
    if risk <= 0:
        return None
    rr = (tp1 - trigger) / risk

    invalidation = f"突破后回落至${target_level.price:.0f}下方则为假突破"

    return ConditionalStrategy(
        strategy_type="breakout_long",
        label="突破追多",
        trigger_price=round(trigger, 2),
        entry_low=round(trigger, 2),
        entry_high=round(trigger * 1.005, 2),
        stop_loss=round(stop_loss, 2),
        take_profit_1=round(tp1, 2),
        take_profit_2=round(tp2, 2),
        risk_reward=round(rr, 2),
        position_size=_determine_position_size(rr, confidence),
        sl_source=f"{target_level.source}(突破后变支撑)",
        tp1_source=_level_source_label(resistances[1:], tp1) if len(resistances) >= 2 else "等距目标",
        reasoning=f"突破{target_level.source}阻力${target_level.price:.0f}后追多",
        valid_hours=12,
        invalidation=invalidation,
    )


def _build_breakout_short(
    current: float, confidence: float, levels: KeyLevels,
) -> ConditionalStrategy | None:
    """突破追空：在最近支撑下方设条件卖单。"""
    supports = levels.supports
    if not supports:
        return None

    target_level = supports[0]
    trigger = target_level.price * 0.995  # 支撑下方 0.5% buffer

    # 止损 = 支撑位本身（跌破后变阻力）
    stop_loss = target_level.price * 1.01

    # 止盈
    if len(supports) >= 2:
        tp1 = supports[1].price
    else:
        tp1 = trigger - (stop_loss - trigger) * 2
    tp2 = tp1 * 0.98

    risk = stop_loss - trigger
    if risk <= 0:
        return None
    rr = (trigger - tp1) / risk

    invalidation = f"跌破后反弹回${target_level.price:.0f}上方则为假跌破"

    return ConditionalStrategy(
        strategy_type="breakout_short",
        label="突破追空",
        trigger_price=round(trigger, 2),
        entry_low=round(trigger * 0.995, 2),
        entry_high=round(trigger, 2),
        stop_loss=round(stop_loss, 2),
        take_profit_1=round(tp1, 2),
        take_profit_2=round(tp2, 2),
        risk_reward=round(rr, 2),
        position_size=_determine_position_size(rr, confidence),
        sl_source=f"{target_level.source}(跌破后变阻力)",
        tp1_source=_level_source_label(supports[1:], tp1) if len(supports) >= 2 else "等距目标",
        reasoning=f"跌破{target_level.source}支撑${target_level.price:.0f}后追空",
        valid_hours=12,
        invalidation=invalidation,
    )


# ══════════════════════════════════════
# 内部辅助函数
# ══════════════════════════════════════

_STRENGTH_ORDER = {"strong": 3, "medium": 2, "weak": 1}


def _find_nearest_level(
    levels: list[KeyLevel], current: float, side: str, min_strength: int = 2,
) -> KeyLevel | None:
    """找到离当前价最近且强度 >= min_strength 的关键位。"""
    for lv in levels:
        strength = _STRENGTH_ORDER.get(lv.strength, 0)
        if strength < min_strength:
            continue
        if side == "below" and lv.price < current * 0.999:
            return lv
        if side == "above" and lv.price > current * 1.001:
            return lv
    # 放宽强度要求
    for lv in levels:
        if side == "below" and lv.price < current * 0.999:
            return lv
        if side == "above" and lv.price > current * 1.001:
            return lv
    return None


def _find_next_level(
    levels: list[KeyLevel], reference: float, side: str,
) -> KeyLevel | None:
    """找到参考价之后的下一个关键位。"""
    for lv in levels:
        if side == "below" and lv.price < reference * 0.999:
            return lv
        if side == "above" and lv.price > reference * 1.001:
            return lv
    return None


def _find_tp_levels(
    levels: list[KeyLevel], current: float,
) -> tuple[float | None, float | None]:
    """从关键位中取 2 个止盈目标。"""
    if not levels:
        return None, None
    tp1 = levels[0].price
    tp2 = levels[1].price if len(levels) >= 2 else None
    return tp1, tp2


def _level_source_label(levels: list[KeyLevel], target_price: float) -> str:
    """为止盈价找到最接近的关键位来源名称。"""
    if not levels:
        return "计算目标"
    best = min(levels, key=lambda lv: abs(lv.price - target_price))
    if abs(best.price - target_price) / target_price < 0.01:
        return best.source
    return "计算目标"


def _determine_position_size(risk_reward: float, confidence: float) -> PositionSize:
    """根据盈亏比和信心度决定仓位大小。"""
    if risk_reward < MIN_RISK_REWARD_RATIO:
        return PositionSize.SKIP
    if risk_reward >= 3.0 and confidence >= 75:
        return PositionSize.HEAVY
    if risk_reward >= 2.0:
        return PositionSize.NORMAL
    return PositionSize.LIGHT


def _apply_state_constraints(
    strategies: list[ConditionalStrategy],
    direction: Direction,
    market_state: MarketState,
    strategy_mode: str,
) -> list[ConditionalStrategy]:
    """根据市场状态和用户策略模式过滤/修改策略。"""
    force_trend_only = (
        strategy_mode == "trend_only"
        or market_state == MarketState.STRONG_TREND
    )

    if direction == Direction.NEUTRAL:
        return strategies

    is_bullish = direction == Direction.BULLISH
    long_types = {"pullback_long", "breakout_long"}
    short_types = {"bounce_short", "breakout_short"}
    trend_types = long_types if is_bullish else short_types
    counter_types = short_types if is_bullish else long_types

    if force_trend_only:
        return [s for s in strategies if s.strategy_type in trend_types]

    if market_state == MarketState.EXTREME_DIVERGENCE:
        result = []
        for s in strategies:
            if s.strategy_type in counter_types:
                if s.position_size not in (PositionSize.SKIP, PositionSize.LIGHT):
                    s = ConditionalStrategy(
                        strategy_type=s.strategy_type,
                        label=s.label + "⚠️逆势轻仓",
                        trigger_price=s.trigger_price,
                        entry_low=s.entry_low,
                        entry_high=s.entry_high,
                        stop_loss=s.stop_loss,
                        take_profit_1=s.take_profit_1,
                        take_profit_2=s.take_profit_2,
                        risk_reward=s.risk_reward,
                        position_size=PositionSize.LIGHT,
                        sl_source=s.sl_source,
                        tp1_source=s.tp1_source,
                        reasoning=s.reasoning + "（极端背离，限轻仓）",
                        valid_hours=s.valid_hours,
                        invalidation=s.invalidation,
                    )
            result.append(s)
        return result

    # ranging: 保留全部，逆势策略标注高风险
    result = []
    for s in strategies:
        if s.strategy_type in counter_types:
            s = ConditionalStrategy(
                strategy_type=s.strategy_type,
                label=s.label + "⚠️逆势",
                trigger_price=s.trigger_price,
                entry_low=s.entry_low,
                entry_high=s.entry_high,
                stop_loss=s.stop_loss,
                take_profit_1=s.take_profit_1,
                take_profit_2=s.take_profit_2,
                risk_reward=s.risk_reward,
                position_size=s.position_size,
                sl_source=s.sl_source,
                tp1_source=s.tp1_source,
                reasoning=s.reasoning,
                valid_hours=s.valid_hours,
                invalidation=s.invalidation,
            )
        result.append(s)
    return result


def _sort_by_bias(
    strategies: list[ConditionalStrategy], bias: Direction,
) -> list[ConditionalStrategy]:
    """按市场偏向排序策略：偏向方向的策略优先。"""
    # 偏多时：pullback_long > breakout_long > bounce_short > breakout_short
    # 偏空时：bounce_short > breakout_short > pullback_long > breakout_long
    # 中性时：按 R:R 排序
    if bias == Direction.BULLISH:
        order = {"pullback_long": 0, "breakout_long": 1, "bounce_short": 2, "breakout_short": 3}
    elif bias == Direction.BEARISH:
        order = {"bounce_short": 0, "breakout_short": 1, "pullback_long": 2, "breakout_long": 3}
    else:
        # 中性：按盈亏比从高到低
        return sorted(strategies, key=lambda s: s.risk_reward, reverse=True)

    return sorted(strategies, key=lambda s: order.get(s.strategy_type, 99))


def _derive_immediate_action(
    direction: Direction, confidence: float,
    strategies: list[ConditionalStrategy],
) -> str:
    """生成即时行动建议。"""
    # 有无 R:R 达标的策略
    viable = [s for s in strategies if s.position_size != PositionSize.SKIP]

    if not viable:
        return "当前各价位盈亏比均不足1.5:1，建议等待更好位置"

    if direction == Direction.NEUTRAL:
        return "方向不明确，建议挂条件单等待触发，不主动追单"

    d_label = "做多" if direction == Direction.BULLISH else "做空"
    best = viable[0]
    return f"偏{d_label}，优先关注「{best.label}」策略（R:R {best.risk_reward:.1f}:1）"


def _derive_analysis_note(
    direction: Direction, confidence: float, levels: KeyLevels,
    market_state: MarketState = MarketState.RANGING,
) -> str:
    """生成策略总体说明。"""
    state_labels = {
        MarketState.STRONG_TREND: "强趋势",
        MarketState.RANGING: "震荡",
        MarketState.EXTREME_DIVERGENCE: "极端背离",
    }
    parts = [f"市场状态：{state_labels.get(market_state, '未知')}"]

    if direction == Direction.NEUTRAL:
        parts.append("多空信号矛盾，以条件挂单代替主观判断")
    else:
        d = "多" if direction == Direction.BULLISH else "空"
        parts.append(f"信号偏{d}（信心度{confidence:.0f}%）")

    n_sup = len(levels.supports)
    n_res = len(levels.resistances)
    parts.append(f"识别{n_sup}个支撑位/{n_res}个阻力位")

    parts.append("所有策略均为条件单，价格到达触发位后执行")

    return "；".join(parts)
