"""关键价位识别（增强版）。

综合多种数据源识别支撑位和阻力位：
- MA 均线（MA20/MA60）
- 24h 高低点
- 期权 Put/Call OI 密集区 + Max Pain
- 斐波那契回撤位（0.382 / 0.5 / 0.618）
- 整数心理关口
- 前期 Swing High / Swing Low
- Volume Profile 成交密集区（套牢区）

关键位来源越多则强度越高（共振）。
"""

from __future__ import annotations

import math

from core.models import KeyLevel, KeyLevels, MarketSnapshot


def identify_key_levels(snapshot: MarketSnapshot) -> KeyLevels:
    """从快照数据中提取关键支撑和阻力位"""
    supports: list[KeyLevel] = []
    resistances: list[KeyLevel] = []
    price = snapshot.price.current

    if price <= 0:
        return KeyLevels()

    # ── MA 作为动态支撑/阻力 ──
    tech = snapshot.technical
    if tech.ma20 and tech.ma20 < price:
        supports.append(KeyLevel(
            price=tech.ma20, level_type="support",
            source="MA20", strength="medium",
        ))
    elif tech.ma20 and tech.ma20 > price:
        resistances.append(KeyLevel(
            price=tech.ma20, level_type="resistance",
            source="MA20", strength="medium",
        ))

    if tech.ma60 and tech.ma60 < price:
        supports.append(KeyLevel(
            price=tech.ma60, level_type="support",
            source="MA60", strength="strong",
        ))
    elif tech.ma60 and tech.ma60 > price:
        resistances.append(KeyLevel(
            price=tech.ma60, level_type="resistance",
            source="MA60", strength="strong",
        ))

    # ── 24h 高低点 ──
    if snapshot.price.low_24h and snapshot.price.low_24h < price:
        supports.append(KeyLevel(
            price=snapshot.price.low_24h, level_type="support",
            source="24h_low", strength="medium",
        ))
    if snapshot.price.high_24h and snapshot.price.high_24h > price:
        resistances.append(KeyLevel(
            price=snapshot.price.high_24h, level_type="resistance",
            source="24h_high", strength="medium",
        ))

    # ── 斐波那契回撤位 ──
    _add_fibonacci_levels(snapshot.price.high_24h, snapshot.price.low_24h,
                          price, supports, resistances)

    # ── 整数心理关口 ──
    _add_round_number_levels(price, supports, resistances)

    # ── Swing High / Low ──
    for sh in tech.swing_highs:
        if sh > price * 1.001:
            resistances.append(KeyLevel(
                price=sh, level_type="resistance",
                source="swing_high", strength="medium",
            ))
        elif sh < price * 0.999:
            supports.append(KeyLevel(
                price=sh, level_type="support",
                source="swing_high", strength="weak",
            ))

    for sl in tech.swing_lows:
        if sl < price * 0.999:
            supports.append(KeyLevel(
                price=sl, level_type="support",
                source="swing_low", strength="medium",
            ))
        elif sl > price * 1.001:
            resistances.append(KeyLevel(
                price=sl, level_type="resistance",
                source="swing_low", strength="weak",
            ))

    # ── 期权 OI 密集区 ──
    opts = snapshot.options
    if opts:
        for strike in opts.put_oi_peaks:
            if strike < price:
                supports.append(KeyLevel(
                    price=strike, level_type="support",
                    source="options_put_oi", strength="strong",
                ))
        for strike in opts.call_oi_peaks:
            if strike > price:
                resistances.append(KeyLevel(
                    price=strike, level_type="resistance",
                    source="options_call_oi", strength="strong",
                ))

        if opts.max_pain:
            if opts.max_pain < price:
                supports.append(KeyLevel(
                    price=opts.max_pain, level_type="support",
                    source="max_pain", strength="strong",
                ))
            elif opts.max_pain > price:
                resistances.append(KeyLevel(
                    price=opts.max_pain, level_type="resistance",
                    source="max_pain", strength="strong",
                ))

    # ── Volume Profile 成交密集区（套牢区，基础 strong） ──
    for vp_price in tech.volume_profile_levels:
        if abs(vp_price - price) / price < 0.001:
            continue
        if vp_price > price * 1.001:
            resistances.append(KeyLevel(
                price=vp_price, level_type="resistance",
                source="volume_profile", strength="strong",
            ))
        elif vp_price < price * 0.999:
            supports.append(KeyLevel(
                price=vp_price, level_type="support",
                source="volume_profile", strength="strong",
            ))

    # ── 挂单簿深度密集区 ──
    ob = snapshot.orderbook_clusters
    if ob:
        for bid_price in ob.get("bid_clusters", []):
            if bid_price < price:
                supports.append(KeyLevel(
                    price=float(bid_price), level_type="support",
                    source="orderbook_bid", strength="medium",
                ))
        for ask_price in ob.get("ask_clusters", []):
            if ask_price > price:
                resistances.append(KeyLevel(
                    price=float(ask_price), level_type="resistance",
                    source="orderbook_ask", strength="medium",
                ))

    supports.sort(key=lambda x: x.price, reverse=True)
    resistances.sort(key=lambda x: x.price)

    supports = _deduplicate_levels(supports, price)
    resistances = _deduplicate_levels(resistances, price)

    return KeyLevels(supports=supports[:5], resistances=resistances[:5])


def _add_fibonacci_levels(
    high: float, low: float, price: float,
    supports: list[KeyLevel], resistances: list[KeyLevel],
) -> None:
    """基于 24h 高低点计算斐波那契回撤位"""
    if not high or not low or high <= low:
        return
    span = high - low
    if span / price < 0.005:
        return

    for ratio, label in [(0.382, "fib_0.382"), (0.500, "fib_0.500"), (0.618, "fib_0.618")]:
        level = high - span * ratio
        if abs(level - price) / price < 0.001:
            continue
        if level < price:
            supports.append(KeyLevel(
                price=round(level, 2), level_type="support",
                source=label, strength="weak",
            ))
        else:
            resistances.append(KeyLevel(
                price=round(level, 2), level_type="resistance",
                source=label, strength="weak",
            ))


def _add_round_number_levels(
    price: float,
    supports: list[KeyLevel], resistances: list[KeyLevel],
) -> None:
    """在当前价附近找整数心理关口"""
    if price <= 0:
        return

    magnitude = 10 ** max(0, int(math.log10(price)) - 1)
    step = max(magnitude, 1000) if price > 5000 else max(magnitude, 100)

    base = int(price / step) * step
    for mult in range(-2, 4):
        level = base + mult * step
        if level <= 0:
            continue
        dist = abs(level - price) / price
        if dist < 0.002 or dist > 0.08:
            continue
        if level < price:
            supports.append(KeyLevel(
                price=float(level), level_type="support",
                source="round_number", strength="weak",
            ))
        else:
            resistances.append(KeyLevel(
                price=float(level), level_type="resistance",
                source="round_number", strength="weak",
            ))


_VP_SOURCES = frozenset({"volume_profile", "orderbook_bid", "orderbook_ask"})

def _deduplicate_levels(levels: list[KeyLevel], reference_price: float) -> list[KeyLevel]:
    """合并相近价位，多来源汇聚时提升强度（共振）。

    共振规则：
    - 2 来源汇聚 → strong
    - 3+ 来源汇聚 → strong
    - volume_profile/orderbook + 至少 1 个其他独立来源 → critical（密集成交区共振）
    """
    if not levels or reference_price <= 0:
        return levels

    threshold = reference_price * 0.005
    strength_order = {"critical": 4, "strong": 3, "medium": 2, "weak": 1}
    result: list[KeyLevel] = []
    source_sets: list[set[str]] = []

    for level in levels:
        merged = False
        for i, existing in enumerate(result):
            if abs(level.price - existing.price) < threshold:
                source_sets[i].add(level.source)
                sources = source_sets[i]

                has_vp = bool(sources & _VP_SOURCES)
                non_vp_count = len(sources - _VP_SOURCES)

                if has_vp and non_vp_count >= 1:
                    new_strength = "critical"
                elif len(sources) >= 2:
                    new_strength = "strong"
                else:
                    new_strength = existing.strength

                if strength_order.get(new_strength, 0) > strength_order.get(existing.strength, 0):
                    best_source = level.source if has_vp and level.source in _VP_SOURCES else existing.source
                    result[i] = KeyLevel(
                        price=existing.price, level_type=existing.level_type,
                        source=best_source, strength=new_strength,
                    )
                merged = True
                break
        if not merged:
            result.append(level)
            source_sets.append({level.source})

    return result
