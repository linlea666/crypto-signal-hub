"""宏观环境评分因子。

评估维度（满分 ±20）：
- 纳斯达克走势（±4 分，正相关 BTC）
- 标普 500 走势（±4 分，正相关 BTC，与纳指互补）
- DXY 美元指数（±5 分，反相关 BTC）
- 加密市场波动率（±5 分，用 BTC 期权 IV_rank 替代传统 VIX）
- BTC ETF 资金流（±5 分，预留）
- 重大经济事件临近（标记，信心度层处理）
"""

from datetime import datetime, timezone

from core.constants import Direction, FactorName
from core.interfaces import ScoreFactor
from core.models import FactorScore, MarketSnapshot


class MacroFactor(ScoreFactor):
    """宏观环境因子（满分 ±20）"""

    def __init__(self, max_score_val: float = 20.0):
        self._max = max_score_val

    @property
    def name(self) -> str:
        return FactorName.MACRO

    @property
    def max_score(self) -> float:
        return self._max

    def calculate(self, snapshot: MarketSnapshot) -> FactorScore:
        macro = snapshot.macro
        if macro is None:
            return FactorScore(
                name=FactorName.MACRO,
                score=0,
                max_score=self._max,
                direction=Direction.NEUTRAL,
                details="宏观数据不可用",
            )

        score = 0.0
        details_parts: list[str] = []

        # ── 美股走势（±5 分，纳指/标普取信号较强者，避免高相关双重计分）──
        nq = macro.nasdaq_change_pct
        sp = macro.sp500_change_pct or 0.0

        def _us_equity_score(pct: float) -> int:
            if pct > 1.5:
                return 5
            if pct > 1.0:
                return 4
            if pct > 0.3:
                return 2
            if pct < -1.5:
                return -5
            if pct < -1.0:
                return -4
            if pct < -0.3:
                return -2
            return 0

        nq_s, sp_s = _us_equity_score(nq), _us_equity_score(sp)
        # 取绝对值更大的那个（信号更强）
        us_score = nq_s if abs(nq_s) >= abs(sp_s) else sp_s
        score += us_score
        details_parts.append(f"纳指{nq:+.1f}%/标普{sp:+.1f}% → 美股{us_score:+d}")

        # ── DXY 美元指数（±5 分，反相关，渐进分级）──
        dxy = macro.dxy_change_pct
        if dxy > 1.0:
            dxy_s = -5
        elif dxy > 0.5:
            dxy_s = -3
        elif dxy > 0.3:
            dxy_s = -1
        elif dxy < -1.0:
            dxy_s = 5
        elif dxy < -0.5:
            dxy_s = 3
        elif dxy < -0.3:
            dxy_s = 1
        else:
            dxy_s = 0
        if dxy_s != 0:
            score += dxy_s
            details_parts.append(f"DXY{dxy:+.1f}%({dxy_s:+d})")

        # ── 加密市场波动率（±5 分，用 BTC IV_rank 替代 VIX） ──
        # IV_rank 高 = 市场恐慌/波动大 → 风险资产承压
        # IV_rank 低 = 市场平静 → 有利于趋势延续
        opts = snapshot.options
        if opts is not None and opts.iv_rank is not None:
            iv = opts.iv_rank
            if iv > 80:
                score -= 5
                details_parts.append(f"BTC IV_rank={iv:.0f}(极高波动,风险资产承压)")
            elif iv > 65:
                score -= 2
                details_parts.append(f"BTC IV_rank={iv:.0f}(偏高)")
            elif iv < 20:
                score += 2
                details_parts.append(f"BTC IV_rank={iv:.0f}(低波动)")
            else:
                details_parts.append(f"BTC IV_rank={iv:.0f}(正常)")
        elif macro.vix_value is not None:
            # 兼容旧数据中的 VIX 值
            vix = macro.vix_value
            if vix > 30:
                score -= 5
                details_parts.append(f"VIX={vix:.1f}(恐慌)")
            elif vix > 25:
                score -= 2
                details_parts.append(f"VIX={vix:.1f}(偏高)")
            elif vix < 15:
                score += 2
                details_parts.append(f"VIX={vix:.1f}(平静)")

        # ── ETF 资金流（±5 分）──
        if macro.btc_etf_flow_3d_trend == "inflow":
            score += 5
            details_parts.append("ETF连续净流入(机构买入)")
        elif macro.btc_etf_flow_3d_trend == "outflow":
            score -= 5
            details_parts.append("ETF连续净流出(机构撤退)")

        # ── 重大事件临近衰减评分（高影响事件越近扣分越多）──
        if snapshot.events:
            high_impact = [e for e in snapshot.events if e.impact == "high"]
            if high_impact:
                now = datetime.now(timezone.utc)
                worst_penalty = 0
                event_names = []
                for evt in high_impact[:3]:
                    evt_time = evt.time if evt.time.tzinfo else evt.time.replace(tzinfo=timezone.utc)
                    hours_away = (evt_time - now).total_seconds() / 3600
                    if hours_away < 0:
                        hours_away = 0
                    if hours_away <= 24:
                        penalty = -3
                    elif hours_away <= 48:
                        penalty = -2
                    elif hours_away <= 72:
                        penalty = -1
                    else:
                        penalty = 0
                    if penalty < worst_penalty:
                        worst_penalty = penalty
                    label = f"{evt.name}({hours_away:.0f}h后)"
                    event_names.append(label)

                if worst_penalty < 0:
                    score += worst_penalty
                    details_parts.append(f"⚠️ 重大事件临近({worst_penalty:+d}): {', '.join(event_names)}")
                else:
                    details_parts.append(f"近期事件: {', '.join(event_names)}")

        # ── 数据新鲜度衰减：仅衰减美股相关部分，DXY/事件/IV 独立更新不受影响 ──
        age = macro.data_age_hours
        if age > 6 and us_score != 0:
            now_utc = datetime.now(timezone.utc)
            hour_utc = now_utc.hour
            us_market_closed = hour_utc >= 21 or hour_utc < 14
            if us_market_closed:
                decay = 0.5
                old_us = us_score
                decayed_us = round(us_score * decay)
                score += (decayed_us - old_us)
                details_parts.append(f"⏳ 美股休市且数据{age:.1f}h未更新,美股分{old_us:+d}→{decayed_us:+d}")

        score = max(-self._max, min(self._max, score))
        direction = Direction.BULLISH if score > 0 else (
            Direction.BEARISH if score < 0 else Direction.NEUTRAL
        )
        return FactorScore(
            name=FactorName.MACRO,
            score=round(score, 1),
            max_score=self._max,
            direction=direction,
            details="; ".join(details_parts),
        )
