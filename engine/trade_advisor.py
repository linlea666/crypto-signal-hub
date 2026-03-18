"""条件挂单策略推导器（纯计算，无 IO）。

基于评分方向、信心度、关键价位，始终生成 2-3 个条件策略：
- 回调做多 (pullback_long)：在支撑位挂买单
- 反弹做空 (bounce_short)：在阻力位挂卖单
- 突破追多 (breakout_long)：在阻力上方设条件单
- 突破追空 (breakout_short)：在支撑下方设条件单

核心原则：小亏大赚
- 每个策略独立计算盈亏比
- R:R < 阈值时标记为 SKIP 但仍展示
- rr_at_trigger 无效（<=0）时直接丢弃策略
- SL 优先基于 ATR 动态计算，关键位作为参考
- 逆势策略需 rr_at_trigger >= 1.5
"""

from __future__ import annotations

from core.constants import (
    Direction,
    MarketState,
    PositionSize,
)
from core.models import (
    ConditionalStrategy,
    KeyLevel,
    KeyLevels,
    PriceData,
    TechnicalData,
    TradePlan,
    TradeSuggestion,
)

BREAKOUT_BUFFER_PCT = 0.008
HYBRID_TREND_MULTIPLIER = 1.25
MIN_TP1_DISTANCE_PCT = 0.015  # TP1 距 trigger 最少 1.5%
MIN_ENTRY_QUALITY_HARD = 15   # 低于此值在策略层直接 SKIP（绝对垃圾位置）
MAX_ENTRY_DISTANCE_ATR = 3.0  # 关键位搜索最大 ATR 距离


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
    technical: TechnicalData | None = None,
) -> TradePlan:
    """始终生成包含多个条件策略的交易计划。"""
    current = price.current
    if current <= 0:
        return TradePlan(
            market_bias=Direction.NEUTRAL,
            immediate_action="价格数据异常，暂停交易",
        )

    atr = _get_atr(technical, current)
    h24 = price.high_24h or current * 1.02
    l24 = price.low_24h or current * 0.98

    strategies: list[ConditionalStrategy] = []

    pullback = _build_pullback_long(current, confidence, levels, atr, h24, l24)
    if pullback:
        strategies.append(pullback)

    bounce = _build_bounce_short(current, confidence, levels, atr, h24, l24)
    if bounce:
        strategies.append(bounce)

    breakout_long = _build_breakout_long(current, confidence, levels, atr, h24, l24)
    if breakout_long:
        strategies.append(breakout_long)

    breakout_short = _build_breakout_short(current, confidence, levels, atr, h24, l24)
    if breakout_short:
        strategies.append(breakout_short)

    strategies = _apply_state_constraints(
        strategies, direction, market_state, strategy_mode,
    )
    strategies = _sort_by_bias(strategies, direction)
    strategies = strategies[:3]

    immediate = _derive_immediate_action(direction, confidence, strategies)
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
    """旧版兼容接口。"""
    if plan is None:
        plan = derive_trade_plan(direction, confidence, price, levels)

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
# ATR 辅助
# ══════════════════════════════════════

def _get_atr(tech: TechnicalData | None, current: float) -> float:
    """获取 ATR 值，无 ATR 时按价格 2% 兜底。"""
    if tech and tech.atr_4h and tech.atr_4h > 0:
        return tech.atr_4h
    return current * 0.02


def _atr_sl(base: float, atr: float, direction: str, multiplier: float = 1.5) -> float:
    """基于 ATR 计算 SL 并与关键位 SL 取更宽者。

    direction: "long"(SL 在下) / "short"(SL 在上)
    """
    if direction == "long":
        return round(base - atr * multiplier, 2)
    return round(base + atr * multiplier, 2)


# ══════════════════════════════════════
# 入场质量评分
# ══════════════════════════════════════

def _calc_entry_quality(
    trigger: float, current: float, atr: float,
    high_24h: float, low_24h: float,
    strength: str, side: str, strategy_type: str,
) -> float:
    """综合入场质量评分 (0-100)。

    三个维度：
    1. 区间位置 (0-40)：做多要在区间底部，做空要在区间顶部
    2. ATR 距离 (0-35)：回调/反弹越深越好，突破不宜太远
    3. 关键位强度 (0-25)：critical > strong > medium > weak
    """
    is_breakout = "breakout" in strategy_type

    # ── 维度 1：区间位置 (0-40) ──
    range_size = high_24h - low_24h
    if not is_breakout and range_size > 0 and low_24h > 0:
        position_pct = (trigger - low_24h) / range_size
        position_pct = max(0.0, min(1.0, position_pct))
        if side == "long":
            range_score = max(0.0, (0.45 - position_pct)) / 0.45 * 40
        else:
            range_score = max(0.0, (position_pct - 0.55)) / 0.45 * 40
    else:
        range_score = 20.0

    # ── 维度 2：ATR 距离 (0-35) ──
    distance_atr = abs(current - trigger) / atr if atr > 0 else 0.0
    if is_breakout:
        if distance_atr <= 1.0:
            dist_score = 35.0
        else:
            dist_score = max(0.0, 35.0 - (distance_atr - 1.0) * 15)
    else:
        dist_score = min(distance_atr / 1.5, 1.0) * 35

    # ── 维度 3：关键位强度 (0-25) ──
    strength_scores = {"critical": 25, "strong": 18, "medium": 10, "weak": 3}
    level_score = strength_scores.get(strength, 5)

    return round(range_score + dist_score + level_score, 1)


def _find_best_entry_level(
    levels: list[KeyLevel], current: float, atr: float,
    high_24h: float, low_24h: float,
    side: str, strategy_type: str,
) -> KeyLevel | None:
    """在所有候选关键位中选择入场质量最高的（而非最近的）。

    限制搜索范围在 MAX_ENTRY_DISTANCE_ATR 以内，
    并要求至少 medium 强度（无候选时放宽到所有强度）。
    """
    def _candidates(min_str: int) -> list[tuple[float, KeyLevel]]:
        result = []
        for lv in levels:
            s_val = _STRENGTH_ORDER.get(lv.strength, 0)
            if s_val < min_str:
                continue
            if side == "below" and lv.price >= current * 0.999:
                continue
            if side == "above" and lv.price <= current * 1.001:
                continue
            if abs(lv.price - current) > atr * MAX_ENTRY_DISTANCE_ATR:
                continue
            quality = _calc_entry_quality(
                lv.price, current, atr, high_24h, low_24h,
                lv.strength, "long" if side == "below" else "short",
                strategy_type,
            )
            result.append((quality, lv))
        return result

    candidates = _candidates(min_str=2)
    if not candidates:
        candidates = _candidates(min_str=0)
    if not candidates:
        return None

    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


# ══════════════════════════════════════
# 四种条件策略构建
# ══════════════════════════════════════

def _build_pullback_long(
    current: float, confidence: float, levels: KeyLevels, atr: float,
    high_24h: float = 0.0, low_24h: float = 0.0,
) -> ConditionalStrategy | None:
    """回调做多：在入场质量最高的支撑位挂买单。"""
    supports = levels.supports
    resistances = levels.resistances

    trigger_level = _find_best_entry_level(
        supports, current, atr, high_24h, low_24h,
        side="below", strategy_type="pullback_long",
    )
    if trigger_level is None:
        return None

    trigger = trigger_level.price

    # SL：关键位 SL 和 ATR SL 取更宽（更保守）者
    sl_level = _find_next_level(supports, trigger, side="below")
    level_sl = (sl_level.price * 0.99) if sl_level else (trigger * 0.98)
    atr_sl_val = _atr_sl(trigger, atr, "long")
    stop_loss = min(level_sl, atr_sl_val)

    tp1, tp2 = _find_tp_levels(resistances, current)
    if tp1 is None:
        tp1 = current * 1.03
    # TP1 最小距离保证
    tp1 = max(tp1, trigger * (1 + MIN_TP1_DISTANCE_PCT))
    if tp2 is None:
        tp2 = tp1 * 1.02

    entry_mid = (trigger + current) / 2 if trigger < current else trigger
    risk = entry_mid - stop_loss
    if risk <= 0:
        return None
    rr_fixed = round((tp1 - entry_mid) / risk, 2)

    risk_at_trigger = trigger - stop_loss
    rr_trigger = round((tp1 - trigger) / risk_at_trigger, 2) if risk_at_trigger > 0 else 0.0

    if rr_trigger <= 0:
        return None

    invalidation_price = sl_level.price if sl_level else trigger * 0.98
    invalidation = f"价格跌破${invalidation_price:.0f}则策略失效"

    quality = _calc_entry_quality(
        trigger, current, atr, high_24h, low_24h,
        trigger_level.strength, "long", "pullback_long",
    )

    return ConditionalStrategy(
        strategy_type="pullback_long",
        label="回调做多",
        trigger_price=round(trigger, 2),
        entry_low=round(trigger, 2),
        entry_high=round(current, 2),
        stop_loss=round(stop_loss, 2),
        take_profit_1=round(tp1, 2),
        take_profit_2=round(tp2, 2),
        risk_reward=rr_fixed,
        position_size=_determine_position_size(rr_fixed, confidence, "hybrid", trigger_level.strength, quality),
        sl_source=f"{trigger_level.source}下方ATR×1.5",
        tp1_source=_level_source_label(resistances, tp1),
        reasoning=f"在{trigger_level.source}支撑位${trigger:.0f}附近挂买单，止损${stop_loss:.0f}",
        valid_hours=24,
        invalidation=invalidation,
        tp_mode="hybrid",
        rr_at_trigger=rr_trigger,
        trigger_strength=trigger_level.strength,
        entry_quality=quality,
    )


def _build_bounce_short(
    current: float, confidence: float, levels: KeyLevels, atr: float,
    high_24h: float = 0.0, low_24h: float = 0.0,
) -> ConditionalStrategy | None:
    """反弹做空：在入场质量最高的阻力位挂卖单。"""
    supports = levels.supports
    resistances = levels.resistances

    trigger_level = _find_best_entry_level(
        resistances, current, atr, high_24h, low_24h,
        side="above", strategy_type="bounce_short",
    )
    if trigger_level is None:
        return None

    trigger = trigger_level.price

    sl_level = _find_next_level(resistances, trigger, side="above")
    level_sl = (sl_level.price * 1.01) if sl_level else (trigger * 1.02)
    atr_sl_val = _atr_sl(trigger, atr, "short")
    stop_loss = max(level_sl, atr_sl_val)

    tp1, tp2 = _find_tp_levels(supports, current)
    if tp1 is None:
        tp1 = current * 0.97
    tp1 = min(tp1, trigger * (1 - MIN_TP1_DISTANCE_PCT))
    if tp2 is None:
        tp2 = tp1 * 0.98

    entry_mid = (current + trigger) / 2 if trigger > current else trigger
    risk = stop_loss - entry_mid
    if risk <= 0:
        return None
    rr_fixed = round((entry_mid - tp1) / risk, 2)

    risk_at_trigger = stop_loss - trigger
    rr_trigger = round((trigger - tp1) / risk_at_trigger, 2) if risk_at_trigger > 0 else 0.0

    if rr_trigger <= 0:
        return None

    invalidation_price = sl_level.price if sl_level else trigger * 1.02
    invalidation = f"价格放量突破${invalidation_price:.0f}则策略失效"

    quality = _calc_entry_quality(
        trigger, current, atr, high_24h, low_24h,
        trigger_level.strength, "short", "bounce_short",
    )

    return ConditionalStrategy(
        strategy_type="bounce_short",
        label="反弹做空",
        trigger_price=round(trigger, 2),
        entry_low=round(current, 2),
        entry_high=round(trigger, 2),
        stop_loss=round(stop_loss, 2),
        take_profit_1=round(tp1, 2),
        take_profit_2=round(tp2, 2),
        risk_reward=rr_fixed,
        position_size=_determine_position_size(rr_fixed, confidence, "hybrid", trigger_level.strength, quality),
        sl_source=f"{trigger_level.source}上方ATR×1.5",
        tp1_source=_level_source_label(supports, tp1),
        reasoning=f"在{trigger_level.source}阻力位${trigger:.0f}附近挂卖单，止损${stop_loss:.0f}",
        valid_hours=24,
        invalidation=invalidation,
        tp_mode="hybrid",
        rr_at_trigger=rr_trigger,
        trigger_strength=trigger_level.strength,
        entry_quality=quality,
    )


def _build_breakout_long(
    current: float, confidence: float, levels: KeyLevels, atr: float,
    high_24h: float = 0.0, low_24h: float = 0.0,
) -> ConditionalStrategy | None:
    """突破追多：在最近阻力上方设条件买单。"""
    resistances = levels.resistances
    if not resistances:
        return None

    target_level = resistances[0]
    trigger = target_level.price * (1 + BREAKOUT_BUFFER_PCT)

    # SL = 突破阻力位下方 1 ATR
    level_sl = target_level.price * 0.99
    atr_sl_val = _atr_sl(trigger, atr, "long", multiplier=1.0)
    stop_loss = min(level_sl, atr_sl_val)

    # TP1：保证最低距离
    if len(resistances) >= 2:
        tp1 = resistances[1].price
    else:
        tp1 = trigger + (trigger - stop_loss) * 2
    tp1 = max(tp1, trigger * (1 + MIN_TP1_DISTANCE_PCT))
    tp2 = tp1 * 1.02

    risk = trigger - stop_loss
    if risk <= 0:
        return None
    rr_fixed = round((tp1 - trigger) / risk, 2)

    if rr_fixed <= 0:
        return None

    invalidation = f"突破后回落至${target_level.price:.0f}下方则为假突破"

    quality = _calc_entry_quality(
        trigger, current, atr, high_24h, low_24h,
        target_level.strength, "long", "breakout_long",
    )

    return ConditionalStrategy(
        strategy_type="breakout_long",
        label="突破追多",
        trigger_price=round(trigger, 2),
        entry_low=round(trigger, 2),
        entry_high=round(trigger * (1 + BREAKOUT_BUFFER_PCT / 2), 2),
        stop_loss=round(stop_loss, 2),
        take_profit_1=round(tp1, 2),
        take_profit_2=round(tp2, 2),
        risk_reward=rr_fixed,
        position_size=_determine_position_size(rr_fixed, confidence, "hybrid", target_level.strength, quality),
        sl_source=f"{target_level.source}(突破后变支撑)",
        tp1_source=_level_source_label(resistances[1:], tp1) if len(resistances) >= 2 else "ATR等距目标",
        reasoning=f"突破{target_level.source}阻力${target_level.price:.0f}后追多",
        valid_hours=12,
        invalidation=invalidation,
        tp_mode="hybrid",
        rr_at_trigger=rr_fixed,
        trigger_strength=target_level.strength,
        entry_quality=quality,
    )


def _build_breakout_short(
    current: float, confidence: float, levels: KeyLevels, atr: float,
    high_24h: float = 0.0, low_24h: float = 0.0,
) -> ConditionalStrategy | None:
    """突破追空：在最近支撑下方设条件卖单。"""
    supports = levels.supports
    if not supports:
        return None

    target_level = supports[0]
    trigger = target_level.price * (1 - BREAKOUT_BUFFER_PCT)

    level_sl = target_level.price * 1.01
    atr_sl_val = _atr_sl(trigger, atr, "short", multiplier=1.0)
    stop_loss = max(level_sl, atr_sl_val)

    if len(supports) >= 2:
        tp1 = supports[1].price
    else:
        tp1 = trigger - (stop_loss - trigger) * 2
    tp1 = min(tp1, trigger * (1 - MIN_TP1_DISTANCE_PCT))
    tp2 = tp1 * 0.98

    risk = stop_loss - trigger
    if risk <= 0:
        return None
    rr_fixed = round((trigger - tp1) / risk, 2)

    if rr_fixed <= 0:
        return None

    invalidation = f"跌破后反弹回${target_level.price:.0f}上方则为假跌破"

    quality = _calc_entry_quality(
        trigger, current, atr, high_24h, low_24h,
        target_level.strength, "short", "breakout_short",
    )

    return ConditionalStrategy(
        strategy_type="breakout_short",
        label="突破追空",
        trigger_price=round(trigger, 2),
        entry_low=round(trigger * (1 - BREAKOUT_BUFFER_PCT / 2), 2),
        entry_high=round(trigger, 2),
        stop_loss=round(stop_loss, 2),
        take_profit_1=round(tp1, 2),
        take_profit_2=round(tp2, 2),
        risk_reward=rr_fixed,
        position_size=_determine_position_size(rr_fixed, confidence, "hybrid", target_level.strength, quality),
        sl_source=f"{target_level.source}(跌破后变阻力)",
        tp1_source=_level_source_label(supports[1:], tp1) if len(supports) >= 2 else "ATR等距目标",
        reasoning=f"跌破{target_level.source}支撑${target_level.price:.0f}后追空",
        valid_hours=12,
        invalidation=invalidation,
        tp_mode="hybrid",
        rr_at_trigger=rr_fixed,
        trigger_strength=target_level.strength,
        entry_quality=quality,
    )


# ══════════════════════════════════════
# 内部辅助函数
# ══════════════════════════════════════

_STRENGTH_ORDER = {"critical": 4, "strong": 3, "medium": 2, "weak": 1}


def _find_nearest_level(
    levels: list[KeyLevel], current: float, side: str, min_strength: int = 2,
) -> KeyLevel | None:
    for lv in levels:
        strength = _STRENGTH_ORDER.get(lv.strength, 0)
        if strength < min_strength:
            continue
        if side == "below" and lv.price < current * 0.999:
            return lv
        if side == "above" and lv.price > current * 1.001:
            return lv
    for lv in levels:
        if side == "below" and lv.price < current * 0.999:
            return lv
        if side == "above" and lv.price > current * 1.001:
            return lv
    return None


def _find_next_level(
    levels: list[KeyLevel], reference: float, side: str,
) -> KeyLevel | None:
    for lv in levels:
        if side == "below" and lv.price < reference * 0.999:
            return lv
        if side == "above" and lv.price > reference * 1.001:
            return lv
    return None


def _find_tp_levels(
    levels: list[KeyLevel], current: float,
) -> tuple[float | None, float | None]:
    if not levels:
        return None, None
    valid = [lv for lv in levels if abs(lv.price - current) / max(current, 1) > 0.002]
    if not valid:
        return None, None
    tp1 = valid[0].price
    tp2 = valid[1].price if len(valid) >= 2 else None
    return tp1, tp2


def _level_source_label(levels: list[KeyLevel], target_price: float) -> str:
    if not levels:
        return "计算目标"
    best = min(levels, key=lambda lv: abs(lv.price - target_price))
    if abs(best.price - target_price) / target_price < 0.01:
        return best.source
    return "计算目标"


def _determine_position_size(
    risk_reward: float, confidence: float, tp_mode: str = "hybrid",
    level_strength: str = "medium", entry_quality: float = 50.0,
) -> PositionSize:
    """根据入场质量 + 盈亏比 + 信心度综合计算仓位。

    入场质量低于硬底线 → SKIP（策略层拦截）；
    其他情况由 quality + R:R + confidence 共同决定仓位大小。
    R:R 不再是准入门槛，只是仓位权重因子。
    """
    if entry_quality < MIN_ENTRY_QUALITY_HARD:
        return PositionSize.SKIP

    rr_factor = max(0.5, min(risk_reward / 1.5, 2.0))
    quality_factor = min(entry_quality / 60.0, 1.5)
    conf_factor = confidence / 70.0

    composite = rr_factor * quality_factor * conf_factor
    if tp_mode == "hybrid":
        composite *= HYBRID_TREND_MULTIPLIER
    if level_strength == "critical":
        composite *= 1.3
    if composite >= 1.8:
        return PositionSize.HEAVY
    if composite >= 1.1:
        return PositionSize.NORMAL
    return PositionSize.LIGHT


def _apply_state_constraints(
    strategies: list[ConditionalStrategy],
    direction: Direction,
    market_state: MarketState,
    strategy_mode: str,
) -> list[ConditionalStrategy]:
    """根据市场状态和用户策略模式过滤/修改策略。

    对逆势策略额外要求 rr_at_trigger >= 1.5。
    """
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

    result = []
    for s in strategies:
        if s.strategy_type in counter_types:
            # 逆势策略需 rr_at_trigger >= 1.5
            if s.rr_at_trigger < 1.5:
                s = _replace_strategy(
                    s,
                    position_size=PositionSize.SKIP,
                    label=s.label + "⚠️逆势R:R不足",
                )
            elif market_state == MarketState.EXTREME_DIVERGENCE:
                if s.position_size not in (PositionSize.SKIP, PositionSize.LIGHT):
                    s = _replace_strategy(
                        s,
                        position_size=PositionSize.LIGHT,
                        label=s.label + "⚠️逆势轻仓",
                        reasoning=s.reasoning + "（极端背离，限轻仓）",
                    )
            elif market_state == MarketState.TREND_WEAKENING:
                s = _replace_strategy(s, label=s.label + "⚠️趋势衰减逆势")
            else:
                s = _replace_strategy(s, label=s.label + "⚠️逆势")
        result.append(s)
    return result


def _replace_strategy(s: ConditionalStrategy, **kwargs) -> ConditionalStrategy:
    """创建策略副本并替换指定字段。"""
    fields = {
        "strategy_type": s.strategy_type,
        "label": s.label,
        "trigger_price": s.trigger_price,
        "entry_low": s.entry_low,
        "entry_high": s.entry_high,
        "stop_loss": s.stop_loss,
        "take_profit_1": s.take_profit_1,
        "take_profit_2": s.take_profit_2,
        "risk_reward": s.risk_reward,
        "position_size": s.position_size,
        "sl_source": s.sl_source,
        "tp1_source": s.tp1_source,
        "reasoning": s.reasoning,
        "valid_hours": s.valid_hours,
        "invalidation": s.invalidation,
        "tp_mode": s.tp_mode,
        "trailing_callback_pct": s.trailing_callback_pct,
        "tp1_close_ratio": s.tp1_close_ratio,
        "market_state": s.market_state,
        "rr_at_trigger": s.rr_at_trigger,
        "trigger_strength": s.trigger_strength,
        "entry_quality": s.entry_quality,
    }
    fields.update(kwargs)
    return ConditionalStrategy(**fields)


def _sort_by_bias(
    strategies: list[ConditionalStrategy], bias: Direction,
) -> list[ConditionalStrategy]:
    if bias == Direction.BULLISH:
        order = {"pullback_long": 0, "breakout_long": 1, "bounce_short": 2, "breakout_short": 3}
    elif bias == Direction.BEARISH:
        order = {"bounce_short": 0, "breakout_short": 1, "pullback_long": 2, "breakout_long": 3}
    else:
        return sorted(strategies, key=lambda s: s.risk_reward, reverse=True)

    return sorted(strategies, key=lambda s: order.get(s.strategy_type, 99))


def _derive_immediate_action(
    direction: Direction, confidence: float,
    strategies: list[ConditionalStrategy],
) -> str:
    viable = [s for s in strategies if s.position_size != PositionSize.SKIP]

    if not viable:
        skipped = [s for s in strategies if s.position_size == PositionSize.SKIP]
        if skipped:
            best_skip = max(skipped, key=lambda s: s.risk_reward)
            return (
                f"当前最优策略「{best_skip.label}」R:R仅{best_skip.risk_reward:.1f}:1，"
                f"不足开仓门槛，建议等待回调或突破后再入场"
            )
        return "当前无有效策略，建议观望"

    if direction == Direction.NEUTRAL:
        return "方向不明确，建议挂条件单等待触发，不主动追单"

    d_label = "做多" if direction == Direction.BULLISH else "做空"
    best = viable[0]
    tp_label = "混合止盈" if best.tp_mode == "hybrid" else "固定止盈"
    return f"偏{d_label}，优先关注「{best.label}」策略（R:R {best.risk_reward:.1f}:1，{tp_label}）"


def _derive_analysis_note(
    direction: Direction, confidence: float, levels: KeyLevels,
    market_state: MarketState = MarketState.RANGING,
) -> str:
    state_labels = {
        MarketState.STRONG_TREND: "强趋势",
        MarketState.TREND_WEAKENING: "趋势衰减",
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
