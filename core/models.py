"""核心领域模型定义。

所有模块共享的数据结构。模型只承载数据和基础派生计算，
不包含 IO 操作或业务逻辑，确保可测试和序列化。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from core.constants import (
    AlertType,
    Direction,
    FactorName,
    FundingRateLevel,
    OIPriceSignal,
    SignalStrength,
)


# ══════════════════════════════════════
# 数据采集层模型 —— 原始市场数据
# ══════════════════════════════════════

@dataclass(frozen=True)
class PriceData:
    """价格快照"""
    current: float
    high_24h: float
    low_24h: float
    change_pct_24h: float
    volume_24h: float


@dataclass(frozen=True)
class TechnicalData:
    """技术指标数据"""
    ma20: float | None = None
    ma60: float | None = None
    ma_trend: Direction = Direction.NEUTRAL
    rsi_4h: float | None = None
    # K 线结构：higher_highs / lower_lows / range
    structure: str = "unknown"


@dataclass(frozen=True)
class FundingRateData:
    """多交易所资金费率"""
    rates: dict[str, float] = field(default_factory=dict)  # {exchange: rate}
    average: float = 0.0
    level: FundingRateLevel = FundingRateLevel.NORMAL


@dataclass(frozen=True)
class OpenInterestData:
    """持仓量数据"""
    total_usd: float = 0.0
    change_pct_24h: float = 0.0
    price_oi_signal: OIPriceSignal = OIPriceSignal.NEW_LONGS


@dataclass(frozen=True)
class LongShortData:
    """多空比数据"""
    account_ratio: float = 1.0     # 账户多空比
    top_trader_ratio: float = 1.0  # 大户多空比
    taker_buy_sell_ratio: float = 1.0  # 主动买卖比


@dataclass(frozen=True)
class OptionsData:
    """期权数据"""
    max_pain: float | None = None
    max_pain_distance_pct: float = 0.0
    nearest_expiry: str = ""
    call_oi_peaks: list[float] = field(default_factory=list)
    put_oi_peaks: list[float] = field(default_factory=list)
    put_call_ratio: float = 1.0
    iv_rank: float = 50.0  # 0-100 百分位


@dataclass(frozen=True)
class MacroData:
    """宏观市场数据"""
    nasdaq_price: float | None = None
    nasdaq_change_pct: float = 0.0
    dxy_price: float | None = None
    dxy_change_pct: float = 0.0
    vix_value: float | None = None
    btc_etf_flow_usd: float | None = None      # 当日 ETF 净流入（美元）
    btc_etf_flow_3d_trend: str = "unknown"      # inflow / outflow / mixed
    fear_greed_value: int | None = None         # 0-100
    fear_greed_label: str = "unknown"


@dataclass(frozen=True)
class UpcomingEvent:
    """即将到来的经济事件"""
    name: str
    time: datetime
    impact: str  # high / medium / low
    description: str = ""


@dataclass(frozen=True)
class DerivativesData:
    """衍生品综合数据"""
    funding_rate: FundingRateData = field(default_factory=FundingRateData)
    open_interest: OpenInterestData = field(default_factory=OpenInterestData)
    long_short: LongShortData = field(default_factory=LongShortData)


@dataclass(frozen=True)
class MarketSnapshot:
    """某一时刻的完整市场数据快照。

    这是系统的核心数据结构，采集层生成它，
    评分引擎消费它，存储层持久化它。
    """
    timestamp: datetime
    symbol: str
    price: PriceData
    technical: TechnicalData = field(default_factory=TechnicalData)
    derivatives: DerivativesData = field(default_factory=DerivativesData)
    options: OptionsData | None = None
    macro: MacroData | None = None
    events: list[UpcomingEvent] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典，用于 AI 输入和存储"""
        import dataclasses
        return _dataclass_to_dict(self)


# ══════════════════════════════════════
# 评分引擎模型 —— 分析结果
# ══════════════════════════════════════

@dataclass(frozen=True)
class FactorScore:
    """单个因子的评分结果"""
    name: FactorName
    score: float           # 负值=看空，正值=看多
    max_score: float       # 该因子的满分值
    direction: Direction
    details: str           # 人类可读的评分依据

    @property
    def normalized(self) -> float:
        """归一化到 [-1, 1] 区间，用于信心度计算"""
        if self.max_score == 0:
            return 0.0
        return self.score / self.max_score


@dataclass(frozen=True)
class KeyLevel:
    """关键价位"""
    price: float
    level_type: str      # support / resistance
    source: str          # 来源说明（如 "options_put_oi", "previous_low"）
    strength: str        # strong / medium / weak


@dataclass(frozen=True)
class KeyLevels:
    """关键价位集合"""
    supports: list[KeyLevel] = field(default_factory=list)
    resistances: list[KeyLevel] = field(default_factory=list)


@dataclass(frozen=True)
class TradeSuggestion:
    """交易建议（仅供参考）"""
    direction: Direction
    entry_price: float
    stop_loss: float
    take_profit_1: float
    take_profit_2: float | None = None
    risk_reward_ratio: float = 0.0
    reasoning: str = ""


@dataclass(frozen=True)
class SignalReport:
    """完整的分析报告，系统的最终输出。

    包含原始快照、各因子评分、综合判断、
    AI 分析文本和交易建议。
    """
    id: str                          # UUID
    timestamp: datetime
    symbol: str
    snapshot: MarketSnapshot
    factor_scores: list[FactorScore]
    total_score: float
    max_possible_score: float
    direction: Direction
    confidence: float                # 0-100%
    signal_strength: SignalStrength
    key_levels: KeyLevels
    ai_analysis: str = ""
    trade_suggestion: TradeSuggestion | None = None
    alert_type: AlertType = AlertType.HOURLY_REPORT

    @property
    def score_display(self) -> str:
        sign = "+" if self.total_score > 0 else ""
        return f"{sign}{self.total_score:.0f}/{self.max_possible_score:.0f}"

    @property
    def direction_label(self) -> str:
        labels = {
            Direction.BULLISH: "偏多",
            Direction.BEARISH: "偏空",
            Direction.NEUTRAL: "中性",
        }
        return labels.get(self.direction, "未知")


# ══════════════════════════════════════
# 辅助函数
# ══════════════════════════════════════

def _dataclass_to_dict(obj: Any) -> Any:
    """递归将 dataclass 转为可 JSON 序列化的字典"""
    import dataclasses
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        result = {}
        for f in dataclasses.fields(obj):
            value = getattr(obj, f.name)
            result[f.name] = _dataclass_to_dict(value)
        return result
    if isinstance(obj, list):
        return [_dataclass_to_dict(item) for item in obj]
    if isinstance(obj, dict):
        return {k: _dataclass_to_dict(v) for k, v in obj.items()}
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, Enum):
        return obj.value
    return obj
