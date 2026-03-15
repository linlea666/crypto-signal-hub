"""关键价位识别。

综合技术面和衍生品数据识别支撑位和阻力位：
- K 线前高前低
- MA 均线位置
- 期权 Put OI 密集区（支撑）/ Call OI 密集区（阻力）
- Max Pain 位置

关键位来源越多则强度越高（共振）。
"""

from __future__ import annotations

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

        # Max Pain 位置
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

    # 按价格排序：支撑从高到低（最近的在前），阻力从低到高
    supports.sort(key=lambda x: x.price, reverse=True)
    resistances.sort(key=lambda x: x.price)

    # 去重：相近价位（0.5% 以内）只保留强度最高的
    supports = _deduplicate_levels(supports, price)
    resistances = _deduplicate_levels(resistances, price)

    return KeyLevels(supports=supports[:5], resistances=resistances[:5])


def _deduplicate_levels(levels: list[KeyLevel], reference_price: float) -> list[KeyLevel]:
    """合并相近价位的关键位，保留强度最高的"""
    if not levels or reference_price <= 0:
        return levels

    threshold = reference_price * 0.005  # 0.5% 以内视为同一价位
    result: list[KeyLevel] = []
    strength_order = {"strong": 3, "medium": 2, "weak": 1}

    for level in levels:
        merged = False
        for i, existing in enumerate(result):
            if abs(level.price - existing.price) < threshold:
                # 保留强度更高的
                if strength_order.get(level.strength, 0) > strength_order.get(existing.strength, 0):
                    result[i] = level
                merged = True
                break
        if not merged:
            result.append(level)

    return result
