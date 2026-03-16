"""风控守卫。

每次下单前执行 5 项独立检查，任一不通过则拒绝交易。
智能资金分配基于 position_size / confidence / risk_reward / market_state 多因子。
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

from config.schema import ExecutorConfig
from executor.models import PendingStrategy, RiskRejectReason

if TYPE_CHECKING:
    from executor.exchange_client import ExchangeClient

logger = logging.getLogger(__name__)


@dataclass
class RiskCheckResult:
    """风控检查结果"""
    passed: bool
    reason: RiskRejectReason | None = None
    detail: str = ""
    suggested_amount_usd: float = 0.0
    sizing_detail: str = ""


class RiskGuard:
    """下单前风控守卫 + 智能资金分配"""

    def __init__(self, config: ExecutorConfig):
        self._config = config
        self._daily_realized_pnl: float = 0.0
        self._daily_reset_date: str = ""
        self._consecutive_losses: int = 0

    def reset_daily(self, date_str: str) -> None:
        if self._daily_reset_date != date_str:
            self._daily_realized_pnl = 0.0
            self._daily_reset_date = date_str

    def record_pnl(self, pnl_usd: float) -> None:
        self._daily_realized_pnl += pnl_usd
        if pnl_usd < 0:
            self._consecutive_losses += 1
        elif pnl_usd > 0:
            self._consecutive_losses = 0
        # pnl_usd == 0 (breakeven) 不重置连亏计数

    def calculate_position_size(
        self,
        equity: float,
        strategy: PendingStrategy,
    ) -> tuple[float, str]:
        """多因子智能资金分配。

        Returns:
            (position_usd, detail_str)
        """
        cfg = self._config

        # 1) 基础档位（skip 标签的限价单策略按 normal 处理）
        size_label = strategy.position_size_label
        effective_label = "normal" if size_label == "skip" else size_label
        base_pct_map = {
            "light": cfg.light_position_pct,
            "normal": cfg.normal_position_pct,
            "heavy": cfg.heavy_position_pct,
        }
        base_pct = base_pct_map.get(effective_label, cfg.normal_position_pct) / 100.0
        parts = [f"base={size_label}({base_pct*100:.0f}%)"]

        if not cfg.enable_dynamic_sizing:
            amount = equity * base_pct
            amount = max(10.0, min(amount, equity * cfg.max_position_pct / 100))
            return amount, " | ".join(parts)

        # 2) 信心度因子: 50%->0.5x, 60%->0.7x, 75%->1.0x, 90%->1.3x
        conf = max(strategy.confidence, 50.0)
        conf_factor = 0.7 + (conf - 60) / 30 * 0.6
        conf_factor = max(0.5, min(conf_factor, 1.5))
        parts.append(f"conf={conf:.0f}%({conf_factor:.2f}x)")

        # 3) 盈亏比因子
        # hybrid/限价单模式 R:R 基准 1.0（对应 1.0x），固定模式基准 1.5
        rr = strategy.risk_reward
        tp_mode = getattr(strategy, "tp_mode", "fixed")
        rr_baseline = 1.0 if tp_mode == "hybrid" else 1.5
        rr_factor = 1.0 + (rr - rr_baseline) / rr_baseline * 0.5
        rr_factor = max(0.8, min(rr_factor, 1.8))
        parts.append(f"rr={rr:.1f}({rr_factor:.2f}x)")

        # 4) 市场状态因子
        state_map = {"strong_trend": 1.0, "ranging": 0.8, "extreme_divergence": 0.6}
        state_factor = state_map.get(strategy.market_state, 0.8)
        parts.append(f"state={strategy.market_state}({state_factor}x)")

        # 5) 连亏缩仓因子
        loss_factor = 1.0
        if cfg.consecutive_loss_shrink and self._consecutive_losses >= 2:
            loss_factor = max(0.3, 1.0 - self._consecutive_losses * 0.15)
            parts.append(f"losses={self._consecutive_losses}({loss_factor:.2f}x)")

        amount = equity * base_pct * conf_factor * rr_factor * state_factor * loss_factor
        amount = max(10.0, min(amount, equity * cfg.max_position_pct / 100))
        parts.append(f"→${amount:.1f}")

        return amount, " | ".join(parts)

    async def pre_trade_check(
        self,
        strategy: PendingStrategy,
        client: "ExchangeClient",
    ) -> RiskCheckResult:
        """执行全部风控检查，返回结果"""

        # 1. 盈亏比检查（hybrid 模式 R:R 已是真实值，门槛固定 1.0 与 trade_advisor 一致）
        from core.constants import MIN_RISK_REWARD_HYBRID
        min_rr = self._config.min_risk_reward
        if getattr(strategy, "tp_mode", "fixed") == "hybrid":
            min_rr = MIN_RISK_REWARD_HYBRID
        if strategy.risk_reward < min_rr:
            return RiskCheckResult(
                passed=False,
                reason=RiskRejectReason.LOW_RISK_REWARD,
                detail=f"盈亏比 {strategy.risk_reward:.2f} < 门槛 {min_rr:.1f} (tp_mode={getattr(strategy, 'tp_mode', 'fixed')})",
            )

        # 2. 获取账户信息
        balance = await client.get_balance()
        equity = balance["equity"]
        available = balance["available"]

        if equity <= 0 or not math.isfinite(equity):
            return RiskCheckResult(
                passed=False,
                reason=RiskRejectReason.INSUFFICIENT_BALANCE,
                detail=f"权益异常: {equity}",
            )

        # 3. 日亏上限检查
        if self._daily_realized_pnl < 0:
            loss_pct = abs(self._daily_realized_pnl) / equity * 100
            if loss_pct >= self._config.daily_loss_limit_pct:
                return RiskCheckResult(
                    passed=False,
                    reason=RiskRejectReason.DAILY_LOSS_LIMIT,
                    detail=f"当日亏损 {loss_pct:.1f}% >= 上限 {self._config.daily_loss_limit_pct}%",
                )

        # 4. 持仓数量检查
        positions = await client.get_positions()
        if len(positions) >= self._config.max_positions:
            return RiskCheckResult(
                passed=False,
                reason=RiskRejectReason.MAX_POSITIONS,
                detail=f"持仓数 {len(positions)} >= 上限 {self._config.max_positions}",
            )

        # 5. 智能资金分配
        position_usd, sizing_detail = self.calculate_position_size(equity, strategy)
        position_usd = min(position_usd, available * 0.9)

        if position_usd < 10:
            return RiskCheckResult(
                passed=False,
                reason=RiskRejectReason.INSUFFICIENT_BALANCE,
                detail=f"可用仓位金额 ${position_usd:.2f} < $10",
            )

        logger.info("资金分配: %s", sizing_detail)

        return RiskCheckResult(
            passed=True,
            suggested_amount_usd=position_usd,
            sizing_detail=sizing_detail,
        )
