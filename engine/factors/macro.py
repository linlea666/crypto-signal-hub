"""宏观环境评分因子。

评估维度：
- 纳斯达克/标普走势（正相关 BTC）
- DXY 美元指数（反相关 BTC）
- VIX 恐慌指数（高 VIX = 风险资产下跌）
- BTC ETF 资金流
- 重大经济事件临近（降低信心度）
"""

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

        # ── 纳斯达克走势（±7 分，正相关）──
        nq = macro.nasdaq_change_pct
        if nq > 1.0:
            score += 7
            details_parts.append(f"纳指+{nq:.1f}%(利好)")
        elif nq > 0.3:
            score += 3
            details_parts.append(f"纳指+{nq:.1f}%(偏好)")
        elif nq < -1.0:
            score -= 7
            details_parts.append(f"纳指{nq:.1f}%(利空)")
        elif nq < -0.3:
            score -= 3
            details_parts.append(f"纳指{nq:.1f}%(偏空)")
        else:
            details_parts.append(f"纳指{nq:+.1f}%(中性)")

        # ── DXY 美元指数（±5 分，反相关）──
        dxy = macro.dxy_change_pct
        if dxy > 0.5:
            score -= 5
            details_parts.append(f"DXY+{dxy:.1f}%(美元走强,利空BTC)")
        elif dxy < -0.5:
            score += 5
            details_parts.append(f"DXY{dxy:.1f}%(美元走弱,利好BTC)")

        # ── VIX 恐慌指数（±5 分）──
        if macro.vix_value is not None:
            vix = macro.vix_value
            if vix > 30:
                score -= 5
                details_parts.append(f"VIX={vix:.1f}(恐慌,风险资产承压)")
            elif vix > 25:
                score -= 2
                details_parts.append(f"VIX={vix:.1f}(偏高)")
            elif vix < 15:
                score += 2
                details_parts.append(f"VIX={vix:.1f}(平静)")
            else:
                details_parts.append(f"VIX={vix:.1f}(正常)")

        # ── ETF 资金流（±5 分）──
        if macro.btc_etf_flow_3d_trend == "inflow":
            score += 5
            details_parts.append("ETF连续净流入(机构买入)")
        elif macro.btc_etf_flow_3d_trend == "outflow":
            score -= 5
            details_parts.append("ETF连续净流出(机构撤退)")

        # ── 重大事件临近（标记但不评分，信心度层处理）──
        if snapshot.events:
            high_impact = [e for e in snapshot.events if e.impact == "high"]
            if high_impact:
                names = ", ".join(e.name for e in high_impact[:2])
                details_parts.append(f"⚠️ 近期重大事件: {names}")

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
