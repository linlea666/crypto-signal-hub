"""市场状态识别层（纯计算，无 IO）。

根据评分方向、信心度、量价配合度、资金费率、OI 变化等多因子
将市场分为三档状态，决定策略生成约束：

- strong_trend：强趋势 → 只生成顺势策略
- ranging：震荡/弱趋势 → 双向策略，逆势标注高风险
- extreme_divergence：极端背离 → 允许逆势但限轻仓

所有输入数据来自现有采集，零额外 API 调用。
"""

from __future__ import annotations

from core.constants import Direction, FundingRateLevel, MarketState
from core.models import DerivativesData, MarketSnapshot, TechnicalData


def classify_market_state(
    total_score: float,
    confidence: float,
    technical: TechnicalData,
    derivatives: DerivativesData,
) -> MarketState:
    """基于多因子判断当前市场状态。

    判定优先级：极端背离 > 强趋势 > 默认震荡
    """
    if _is_extreme_divergence(derivatives):
        return MarketState.EXTREME_DIVERGENCE

    if _is_strong_trend(total_score, confidence, technical):
        return MarketState.STRONG_TREND

    return MarketState.RANGING


def classify_from_snapshot(
    total_score: float,
    confidence: float,
    snapshot: MarketSnapshot,
) -> MarketState:
    return classify_market_state(
        total_score, confidence,
        snapshot.technical, snapshot.derivatives,
    )


def get_trend_direction(total_score: float, max_possible: float = 120.0) -> Direction:
    """从评分中提取趋势方向（用于策略约束）。"""
    threshold = max(8.0, max_possible * 0.08)
    if total_score > threshold:
        return Direction.BULLISH
    if total_score < -threshold:
        return Direction.BEARISH
    return Direction.NEUTRAL


# ── 内部判定函数 ──

def _is_strong_trend(
    total_score: float,
    confidence: float,
    tech: TechnicalData,
) -> bool:
    """强趋势：评分幅度大 + 信心度高 + 量价配合。"""
    abs_score = abs(total_score)
    vol_ratio = tech.volume_ratio or 0

    if abs_score > 15 and confidence > 70 and vol_ratio > 0.8:
        return True

    if abs_score > 20 and confidence > 65:
        return True

    if abs_score > 12 and confidence > 75 and vol_ratio > 1.0:
        return True

    return False


def _is_extreme_divergence(derivatives: DerivativesData) -> bool:
    """极端背离：资金费率极端 + OI 异动同时出现。"""
    fr = derivatives.funding_rate
    oi = derivatives.open_interest

    if fr is None or oi is None:
        return False

    fr_extreme = fr.level in (FundingRateLevel.EXTREME_HIGH, FundingRateLevel.EXTREME_LOW)
    oi_anomaly = abs(oi.change_pct_24h) > 20

    return fr_extreme and oi_anomaly


# ── 状态描述（供 UI/AI 使用） ──

MARKET_STATE_DESCRIPTIONS: dict[MarketState, str] = {
    MarketState.STRONG_TREND: "强趋势行情，只生成顺势策略，逆势策略已过滤",
    MarketState.RANGING: "震荡/弱趋势行情，双向策略均可参考，逆势标注高风险",
    MarketState.EXTREME_DIVERGENCE: "极端背离信号，允许轻仓逆势试探反转",
}
