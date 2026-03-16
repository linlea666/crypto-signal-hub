"""全局常量与枚举定义。

所有模块共享的枚举类型和常量值集中在此，
避免跨模块硬编码和魔法字符串。
"""

from enum import Enum, auto


class Direction(str, Enum):
    """交易方向信号"""
    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"


class SignalStrength(str, Enum):
    """信号强度等级，用于决定推送策略"""
    STRONG = "strong"        # 信心度 ≥ 80%，立即推送
    MODERATE = "moderate"    # 信心度 60-80%，按计划推送
    WEAK = "weak"            # 信心度 < 60%，仅存档
    CONFLICTING = "conflicting"  # 信号矛盾，建议观望


class FactorName(str, Enum):
    """评分因子标识，用于注册和配置映射"""
    TECHNICAL = "technical"
    FUNDING_RATE = "funding_rate"
    OPEN_INTEREST = "open_interest"
    LONG_SHORT_RATIO = "long_short_ratio"
    OPTIONS = "options"
    MACRO = "macro"
    SENTIMENT = "sentiment"


class MarketSession(str, Enum):
    """美股交易时段，影响加密市场的情绪传导"""
    PRE_MARKET = "pre_market"      # 盘前 (ET 04:00-09:30)
    REGULAR = "regular"            # 正常交易 (ET 09:30-16:00)
    AFTER_HOURS = "after_hours"    # 盘后 (ET 16:00-20:00)
    CLOSED = "closed"              # 休市


class FundingRateLevel(str, Enum):
    """资金费率等级"""
    EXTREME_HIGH = "extreme_high"  # > 0.1%
    HIGH = "high"                  # 0.05% ~ 0.1%
    NORMAL = "normal"              # -0.01% ~ 0.05%
    LOW = "low"                    # -0.05% ~ -0.01%
    EXTREME_LOW = "extreme_low"    # < -0.05%


class OIPriceSignal(str, Enum):
    """OI + 价格变动的组合信号"""
    NEW_LONGS = "new_longs"            # 价涨 + OI涨 → 新多头入场
    SHORT_COVERING = "short_covering"  # 价涨 + OI跌 → 空头平仓
    NEW_SHORTS = "new_shorts"          # 价跌 + OI涨 → 新空头入场
    LONG_LIQUIDATION = "long_liquidation"  # 价跌 + OI跌 → 多头平仓


class AlertType(str, Enum):
    """告警类型，决定推送行为"""
    DAILY_REPORT = "daily_report"
    HOURLY_REPORT = "hourly_report"
    STRONG_SIGNAL = "strong_signal"
    EVENT_WARNING = "event_warning"
    ANOMALY = "anomaly"
    BREAKOUT = "breakout"              # 哨兵：价格突破/跌破关键位
    OI_ANOMALY = "oi_anomaly"          # 哨兵：OI 异动
    FUNDING_EXTREME = "funding_extreme"  # 哨兵：资金费率极端
    RAPID_MOVE = "rapid_move"          # 哨兵：短时大幅波动


class MarketState(str, Enum):
    """市场状态，决定策略生成约束"""
    STRONG_TREND = "strong_trend"            # 强趋势：只生成顺势策略
    RANGING = "ranging"                      # 震荡/弱趋势：双向策略，标注风险
    EXTREME_DIVERGENCE = "extreme_divergence"  # 极端背离：允许逆势但限轻仓


class TradeOutcome(str, Enum):
    """回测结果：信号最终触达了哪个目标"""
    TP1_HIT = "tp1_hit"            # 保守止盈命中
    TP2_HIT = "tp2_hit"            # 激进止盈命中
    SL_HIT = "sl_hit"              # 止损命中
    EXPIRED = "expired"            # 窗口期内未触及任何目标
    PENDING = "pending"            # 尚未回测


class PositionSize(str, Enum):
    """仓位建议等级"""
    SKIP = "skip"      # 盈亏比不达标，不建议开仓
    LIGHT = "light"    # 轻仓（盈亏比 1.5~2）
    NORMAL = "normal"  # 标准（盈亏比 2~3）
    HEAVY = "heavy"    # 重仓（盈亏比 > 3 且信心度高）


# 盈亏比阈值——低于此值不给出交易建议（小亏大赚原则）
MIN_RISK_REWARD_RATIO = 1.5

# ── 评分边界常量 ──
# 各因子默认满分，可在配置中覆盖
DEFAULT_FACTOR_WEIGHTS: dict[FactorName, float] = {
    FactorName.TECHNICAL: 20.0,
    FactorName.FUNDING_RATE: 15.0,
    FactorName.OPEN_INTEREST: 15.0,
    FactorName.LONG_SHORT_RATIO: 15.0,
    FactorName.OPTIONS: 20.0,
    FactorName.MACRO: 20.0,
    FactorName.SENTIMENT: 15.0,
}

# 信心度阈值
CONFIDENCE_STRONG_THRESHOLD = 80.0
CONFIDENCE_MODERATE_THRESHOLD = 60.0

# 美股交易时间 (Eastern Time, 24h format)
US_MARKET_OPEN_ET = (9, 30)   # 09:30 ET
US_MARKET_CLOSE_ET = (16, 0)  # 16:00 ET
US_PRE_MARKET_ET = (4, 0)     # 04:00 ET
US_AFTER_HOURS_END_ET = (20, 0)  # 20:00 ET

# 版本号
VERSION = "0.3.0"
APP_NAME = "CryptoSignal Hub"
DEFAULT_PORT = 8686
