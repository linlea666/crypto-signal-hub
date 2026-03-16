"""执行层领域模型。

独立于信号层模型，仅通过 core.models.ConditionalStrategy 作为输入契约。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class OrderStatus(str, Enum):
    """执行订单状态机"""
    PENDING = "pending"          # 等待 trigger_price 触发
    TRIGGERED = "triggered"      # 价格已到达，正在下单
    OPEN = "open"                # 已成交，持仓中
    CLOSED_TP1 = "closed_tp1"   # 止盈1触发平仓
    CLOSED_TP2 = "closed_tp2"   # 止盈2触发平仓
    CLOSED_SL = "closed_sl"     # 止损触发平仓
    CLOSED_MANUAL = "closed_manual"  # 手动平仓
    EXPIRED = "expired"          # valid_hours 超时
    FAILED = "failed"            # 风控拒绝或下单失败
    CANCELLED = "cancelled"      # 用户取消


class RiskRejectReason(str, Enum):
    """风控拒绝原因"""
    INSUFFICIENT_BALANCE = "insufficient_balance"
    MAX_POSITIONS = "max_positions"
    POSITION_TOO_LARGE = "position_too_large"
    LOW_RISK_REWARD = "low_risk_reward"
    DAILY_LOSS_LIMIT = "daily_loss_limit"
    LOW_CONFIDENCE = "low_confidence"
    WEAK_SIGNAL = "weak_signal"
    EXECUTOR_DISABLED = "executor_disabled"


@dataclass
class PendingStrategy:
    """待触发的条件策略（内存队列项）"""
    id: str                      # UUID
    signal_id: str               # 来源 SignalReport.id
    symbol: str
    strategy_type: str           # pullback_long / breakout_long 等
    side: str                    # buy / sell
    trigger_price: float
    entry_low: float
    entry_high: float
    stop_loss: float
    take_profit_1: float
    take_profit_2: float
    risk_reward: float
    leverage: int
    confidence: float            # 来源信号的信心度
    signal_strength: str         # 来源信号强度
    valid_until: datetime        # 过期时间
    position_size_label: str = "normal"  # light/normal/heavy
    market_state: str = "ranging"        # 来源市场状态
    created_at: datetime = field(default_factory=datetime.now)
    tp_mode: str = "hybrid"              # fixed / hybrid
    trailing_callback_pct: float = 1.0   # 移动止盈回撤 %
    tp1_close_ratio: float = 0.5         # TP1 平仓比例


@dataclass
class OrderRecord:
    """订单记录（持久化到 DB）"""
    id: str
    signal_id: str
    symbol: str
    strategy_type: str
    side: str
    status: OrderStatus
    trigger_price: float
    entry_price: float = 0.0
    stop_loss: float = 0.0
    take_profit_1: float = 0.0
    take_profit_2: float = 0.0
    quantity: float = 0.0
    leverage: int = 1
    exchange_order_id: str = ""
    risk_reward: float = 0.0
    pnl_usd: float = 0.0
    pnl_pct: float = 0.0
    reject_reason: str = ""
    created_at: str = ""
    triggered_at: str = ""
    opened_at: str = ""
    closed_at: str = ""
    tp_mode: str = "hybrid"
    trailing_callback_pct: float = 1.0
    tp1_close_ratio: float = 0.5
    highest_price: float = 0.0       # 持仓期间极值价（多=最高 / 空=最低）
    trailing_sl: float = 0.0         # 当前移动止损位
    tp1_triggered_at: str = ""       # TP1 触发时间
