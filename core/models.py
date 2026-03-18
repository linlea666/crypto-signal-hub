"""核心领域模型定义。

所有模块共享的数据结构。模型只承载数据和基础派生计算，
不包含 IO 操作或业务逻辑，确保可测试和序列化。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from core.constants import (
    AlertType,
    Direction,
    FactorName,
    FundingRateLevel,
    MarketState,
    OIPriceSignal,
    PositionSize,
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
    structure: str = "unknown"
    swing_highs: list[float] = field(default_factory=list)
    swing_lows: list[float] = field(default_factory=list)
    # ── 量价分析（从现有 K 线纯计算，无需额外 API） ──
    vwap: float | None = None          # 成交量加权平均价
    volume_ratio: float | None = None  # 当前成交量 / 近期均量，>1.5 放量，<0.5 缩量
    # ── MACD 动量（ta 库计算，零额外 API） ──
    macd_histogram: float | None = None  # MACD 柱状图值，正=多头动量，负=空头动量
    macd_cross: str = "none"             # "golden"=金叉, "death"=死叉, "none"=无交叉
    # ── 布林带（ta 库计算，零额外 API） ──
    bb_percent: float | None = None      # %B = (价格-下轨)/(上轨-下轨)，>1=突破上轨, <0=跌破下轨
    # ── 日线收盘分析（fetch_ohlcv 1d 计算） ──
    daily_close_strength: float | None = None  # (close-low)/(high-low)，>0.7强势，<0.3弱势
    daily_close_vs_ma20: str = "unknown"       # above / below / near
    # ── 成交量分布（Volume Profile 简化版，纯计算） ──
    volume_profile_levels: list[float] = field(default_factory=list)  # 高成交量价格节点
    # ── ATR 波动率（ta 库计算） ──
    atr_4h: float | None = None       # ATR(14) 绝对值（4h K 线）
    atr_pct: float | None = None      # ATR 占价格百分比
    # ── MA 均线交叉（金叉/死叉事件） ──
    ma_cross: str = "none"            # "golden"=MA20上穿MA60, "death"=MA20下穿MA60, "none"


@dataclass(frozen=True)
class FundingRateData:
    """多交易所资金费率"""
    rates: dict[str, float] = field(default_factory=dict)  # {exchange: rate}
    average: float = 0.0
    level: FundingRateLevel = FundingRateLevel.NORMAL
    # ── 期现基差（httpx 从 OKX premium 接口补充） ──
    basis_rate: float = 0.0  # (合约价-现货价)/现货价，正=期货升水，负=贴水


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
    iv_rank: float | None = None  # 0-100 百分位，None=未采集


@dataclass(frozen=True)
class MacroData:
    """宏观市场数据"""
    nasdaq_price: float | None = None
    nasdaq_change_pct: float = 0.0
    sp500_price: float | None = None
    sp500_change_pct: float = 0.0
    dxy_price: float | None = None
    dxy_change_pct: float = 0.0
    vix_value: float | None = None              # 新浪 VIX 恐慌指数实时值
    us10y_yield: float | None = None            # 美国 10 年期国债收益率
    us10y_change_pct: float = 0.0               # 10Y 收益率日涨跌幅 %
    gold_price: float | None = None             # COMEX 黄金价格（避险情绪参考）
    gold_change_pct: float = 0.0                # 黄金日涨跌幅 %
    btc_etf_flow_usd: float | None = None       # 当日 ETF 净流入（美元）
    btc_etf_flow_3d_trend: str = "unknown"      # inflow / outflow / mixed
    fear_greed_value: int | None = None          # 0-100
    fear_greed_label: str = "unknown"
    data_age_hours: float = 0.0                  # 数据距上次更新的小时数


@dataclass(frozen=True)
class NofxData:
    """NOFX 外部数据（AI300 信号 + 资金净流 + 订单簿热力图 + 查询热度）"""
    # AI300 量化信号
    ai300_signal: str = ""        # S / A / B / C / D / ""
    ai300_direction: str = ""     # long / short / ""
    ai300_rank: int = 0           # 排名位次，0=不在榜单
    # 资金净流
    netflow_total: float = 0.0    # 净流量（正=流入，负=流出）
    netflow_inst: float = 0.0     # 机构净流
    netflow_retail: float = 0.0   # 散户净流
    # 热力图 — 订单簿 delta
    heatmap_bid_total: float = 0.0   # 买盘总量
    heatmap_ask_total: float = 0.0   # 卖盘总量
    heatmap_delta: float = 0.0       # bid - ask
    heatmap_large_bids: list[float] = field(default_factory=list)
    heatmap_large_asks: list[float] = field(default_factory=list)
    # 社区查询热度
    query_rank: int = 0           # 查询排名位次，0=未上榜


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
    nofx: NofxData | None = None
    events: list[UpcomingEvent] = field(default_factory=list)
    orderbook_clusters: dict = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典，用于 AI 输入和存储"""
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
    strength: str        # critical / strong / medium / weak


@dataclass(frozen=True)
class KeyLevels:
    """关键价位集合"""
    supports: list[KeyLevel] = field(default_factory=list)
    resistances: list[KeyLevel] = field(default_factory=list)


@dataclass(frozen=True)
class TradeSuggestion:
    """交易建议——基于支撑阻力位自动推导（小亏大赚原则）。

    当盈亏比 < 1.5 时 position_size 为 SKIP，表示当前位置不适合开仓。
    所有价格字段均为 USD 绝对价格，非百分比。
    """
    direction: Direction            # 建议方向（与信号方向一致）
    entry_low: float                # 开仓区间下限（回调入场价）
    entry_high: float               # 开仓区间上限（当前价附近）
    stop_loss: float                # 止损价
    take_profit_1: float            # 保守止盈（最近阻力/支撑）
    take_profit_2: float            # 激进止盈（更远的强阻力/支撑）
    risk_reward_1: float            # 保守盈亏比
    risk_reward_2: float            # 激进盈亏比
    position_size: PositionSize     # 仓位建议
    sl_source: str = ""             # 止损参考来源（如 "60日均线下方1%"）
    tp1_source: str = ""            # 保守止盈参考来源
    tp2_source: str = ""            # 激进止盈参考来源
    reasoning: str = ""             # 综合建议理由


@dataclass(frozen=True)
class ConditionalStrategy:
    """单个条件挂单策略（小亏大赚原则）。

    无论当前方向如何，系统始终生成多个条件策略供选择。
    每个策略包含完整的挂单价、止损止盈、盈亏比及失效条件。
    """
    strategy_type: str      # pullback_long / bounce_short / breakout_long / breakout_short
    label: str              # 回调做多 / 反弹做空 / 突破追多 / 突破追空
    trigger_price: float    # 触发/挂单价格
    entry_low: float
    entry_high: float
    stop_loss: float
    take_profit_1: float
    take_profit_2: float
    risk_reward: float      # 基于入场中点的保守盈亏比（展示用）
    position_size: PositionSize
    sl_source: str
    tp1_source: str
    reasoning: str
    valid_hours: int = 24
    invalidation: str = ""  # 失效条件描述
    tp_mode: str = "hybrid"             # "fixed"=固定止盈 / "hybrid"=混合止盈
    trailing_callback_pct: float = 1.0  # 移动止盈回撤幅度 %
    tp1_close_ratio: float = 0.5        # TP1 平仓比例 (0.5=平50%)
    market_state: str = ""              # 传递给执行层用于风控
    rr_at_trigger: float = 0.0          # 按触发价精确计算的盈亏比（限价单判定用）
    trigger_strength: str = "medium"    # 触发位关键位强度: strong/medium/weak（挂单偏移用）


@dataclass(frozen=True)
class TradePlan:
    """完整交易计划——始终包含多个条件策略。

    即使当前不适合入场，也会给出"如果价格到达 X 则做 Y"的挂单方案，
    让用户提前布局而非追涨杀跌。
    """
    market_bias: Direction
    immediate_action: str               # "观望" / "可考虑入场" 等即时建议
    strategies: list[ConditionalStrategy] = field(default_factory=list)
    analysis_note: str = ""


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
    trade_suggestion: TradeSuggestion | None = None  # 旧版兼容，渐进弃用
    trade_plan: TradePlan | None = None              # 新版条件策略计划
    alert_type: AlertType = AlertType.HOURLY_REPORT
    market_state: MarketState = MarketState.RANGING
    trigger_reason: str = ""                         # 哨兵触发原因（空=定时分析）

    @property
    def is_actionable(self) -> bool:
        """默认可操作判定（使用固定 70% 门槛）。
        实际运行时由 JobScheduler._is_signal_actionable() 根据配置门槛判定。
        此属性保留作为序列化和无配置上下文时的兜底。
        """
        if self.confidence < 70.0:
            return False
        if self.trade_plan:
            return any(
                s.position_size.value != "skip" for s in self.trade_plan.strategies
            )
        return False

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
