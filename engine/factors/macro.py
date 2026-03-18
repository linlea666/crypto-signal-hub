"""宏观环境评分因子。

评估维度（满分 ±20）：
- 美股走势（纳斯达克/标普取信号较强者）（±5 分）
- DXY 美元指数（±4 分，反相关 BTC）
- VIX 恐慌指数（±3 分，优先新浪实时 VIX，降级 IV_rank）
- 10Y 美债收益率变化（±3 分，机会成本）
- COMEX 黄金走势（±2 分，避险情绪参考）
- BTC ETF 资金流（±3 分）
- 重大经济事件临近（扣分修正）
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

        # ── 美股走势（±5 分，纳指/标普取信号较强者）──
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
        us_score = nq_s if abs(nq_s) >= abs(sp_s) else sp_s
        score += us_score
        details_parts.append(f"纳指{nq:+.1f}%/标普{sp:+.1f}% → 美股{us_score:+d}")

        # ── DXY 美元指数（±4 分，反相关）──
        dxy = macro.dxy_change_pct
        if dxy > 1.0:
            dxy_s = -4
        elif dxy > 0.5:
            dxy_s = -2
        elif dxy > 0.3:
            dxy_s = -1
        elif dxy < -1.0:
            dxy_s = 4
        elif dxy < -0.5:
            dxy_s = 2
        elif dxy < -0.3:
            dxy_s = 1
        else:
            dxy_s = 0
        if dxy_s != 0:
            score += dxy_s
            details_parts.append(f"DXY{dxy:+.1f}%({dxy_s:+d})")

        # ── VIX 恐慌指数（±3 分，优先新浪实时 VIX） ──
        vix_scored = False
        if macro.vix_value is not None:
            vix = macro.vix_value
            if vix > 30:
                score -= 3
                details_parts.append(f"VIX={vix:.1f}(恐慌-3)")
            elif vix > 25:
                score -= 2
                details_parts.append(f"VIX={vix:.1f}(偏高-2)")
            elif vix > 20:
                score -= 1
                details_parts.append(f"VIX={vix:.1f}(偏高-1)")
            elif vix < 15:
                score += 2
                details_parts.append(f"VIX={vix:.1f}(平静+2)")
            else:
                details_parts.append(f"VIX={vix:.1f}(正常)")
            vix_scored = True

        if not vix_scored:
            opts = snapshot.options
            if opts is not None and opts.iv_rank is not None:
                iv = opts.iv_rank
                if iv > 80:
                    score -= 3
                    details_parts.append(f"IV_rank={iv:.0f}(VIX不可用,降级)(极高-3)")
                elif iv > 65:
                    score -= 1
                    details_parts.append(f"IV_rank={iv:.0f}(VIX不可用,降级)(偏高-1)")
                elif iv < 20:
                    score += 1
                    details_parts.append(f"IV_rank={iv:.0f}(VIX不可用,降级)(低波动+1)")

        # ── 10Y 美债收益率（±3 分，收益率上行利空风险资产） ──
        if macro.us10y_yield is not None:
            u10y = macro.us10y_change_pct
            if u10y > 3.0:
                score -= 3
                details_parts.append(f"10Y国债{u10y:+.1f}%(大幅上行-3)")
            elif u10y > 1.5:
                score -= 2
                details_parts.append(f"10Y国债{u10y:+.1f}%(上行-2)")
            elif u10y > 0.5:
                score -= 1
                details_parts.append(f"10Y国债{u10y:+.1f}%(微升-1)")
            elif u10y < -3.0:
                score += 3
                details_parts.append(f"10Y国债{u10y:+.1f}%(大幅下行+3)")
            elif u10y < -1.5:
                score += 2
                details_parts.append(f"10Y国债{u10y:+.1f}%(下行+2)")
            elif u10y < -0.5:
                score += 1
                details_parts.append(f"10Y国债{u10y:+.1f}%(微降+1)")

        # ── COMEX 黄金（±2 分，DXY+黄金同方向=避险情绪强化） ──
        if macro.gold_price is not None:
            gold_pct = macro.gold_change_pct
            gold_s = 0
            if gold_pct > 1.0:
                gold_s = -2  # 金价大涨 = 避险 = 风险资产承压
            elif gold_pct > 0.5:
                gold_s = -1
            elif gold_pct < -1.0:
                gold_s = 2   # 金价大跌 = 风险偏好 = 利好加密
            elif gold_pct < -0.5:
                gold_s = 1
            # DXY + 黄金同涨 = 避险情绪极强
            if gold_s < 0 and dxy_s < 0:
                pass  # 已各自扣分，不做额外处理（避免双重惩罚）
            if gold_s != 0:
                score += gold_s
                details_parts.append(f"黄金{gold_pct:+.1f}%({gold_s:+d})")

        # ── ETF 资金流（±3 分）──
        if macro.btc_etf_flow_3d_trend == "inflow":
            score += 3
            details_parts.append("ETF连续净流入(+3)")
        elif macro.btc_etf_flow_3d_trend == "outflow":
            score -= 3
            details_parts.append("ETF连续净流出(-3)")

        # ── 重大事件临近衰减评分 ──
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

        # ── 数据新鲜度衰减 ──
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
