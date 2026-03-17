"""执行引擎。

管理待触发策略队列，匹配哨兵价格回调，协调风控和下单。
是信号层（生产者）和交易所（消费者）之间的唯一桥梁。

支持两种执行模式：
- 软件触发（PENDING）：哨兵价格回调触发后市价下单
- 限价单（LIMIT_PENDING）：直接挂限价单到交易所，附带 SL，TP 由移动止盈管理
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from config.schema import ExecutorConfig
from core.constants import MIN_RISK_REWARD_HYBRID, SignalStrength
from core.time_utils import now_beijing
from executor.exchange_client import ExchangeClient
from executor.models import OrderRecord, OrderStatus, PendingStrategy, RiskRejectReason
from executor.position_tracker import PositionTracker
from executor.risk_guard import RiskGuard

if TYPE_CHECKING:
    from core.models import SignalReport

logger = logging.getLogger(__name__)

STRENGTH_ORDER = {
    SignalStrength.STRONG.value: 3,
    SignalStrength.MODERATE.value: 2,
    SignalStrength.WEAK.value: 1,
    SignalStrength.CONFLICTING.value: 0,
}


class ExecutionEngine:
    """执行层核心引擎"""

    def __init__(self, config: ExecutorConfig, db_path: Path):
        self._config = config
        self._client = ExchangeClient(config)
        self._guard = RiskGuard(config)
        self._tracker = PositionTracker(db_path)

        self._pending: dict[str, PendingStrategy] = {}
        self._limit_orders: dict[str, dict] = {}
        self._initialized = False
        self._order_sync_task: asyncio.Task | None = None

    @property
    def is_enabled(self) -> bool:
        return self._config.enabled

    @property
    def client(self) -> ExchangeClient:
        return self._client

    @property
    def tracker(self) -> PositionTracker:
        return self._tracker

    @property
    def guard(self) -> RiskGuard:
        return self._guard

    @property
    def pending_count(self) -> int:
        return len(self._pending) + len(self._limit_orders)

    async def initialize(self) -> None:
        if not self._config.enabled:
            logger.info("执行层已禁用")
            return
        if not self._config.api_key:
            logger.warning("执行层已启用但 API Key 为空，跳过初始化")
            return

        await self._client.initialize()
        self._initialized = True

        await self._recover_limit_orders()

        self._order_sync_task = asyncio.create_task(self._order_sync_loop())
        logger.info(
            "执行引擎启动: %s | 自动执行=%s | 杠杆=%dx | 限价单=%s",
            self._client.mode_label,
            self._config.auto_execute,
            self._config.default_leverage,
            "开启" if self._config.enable_limit_orders else "关闭",
        )

    async def shutdown(self) -> None:
        if self._order_sync_task:
            self._order_sync_task.cancel()
            try:
                await self._order_sync_task
            except asyncio.CancelledError:
                pass
        await self._client.cleanup()
        logger.info("执行引擎已关闭")

    # ── 信号层接口 ──

    async def on_new_plan(self, symbol: str, report: "SignalReport") -> None:
        """接收新的交易计划（由 JobScheduler 在分析完成后调用）"""
        if not self._config.enabled or not self._initialized:
            return

        if not report.trade_plan or not report.trade_plan.strategies:
            return

        min_strength = STRENGTH_ORDER.get(self._config.min_signal_strength, 2)
        actual_strength = STRENGTH_ORDER.get(report.signal_strength.value, 0)
        if actual_strength < min_strength:
            logger.debug("信号强度不足 %s < %s，跳过", report.signal_strength.value, self._config.min_signal_strength)
            return

        if report.confidence < self._config.min_confidence:
            logger.debug("信心度不足 %.0f < %d，跳过", report.confidence, self._config.min_confidence)
            return

        # ── 1. 清理软件 pending 策略（尚未提交到交易所的） ──
        to_remove = [sid for sid, p in self._pending.items() if p.symbol == symbol]
        for sid in to_remove:
            self._pending.pop(sid)
            self._tracker.update_status(sid, OrderStatus.EXPIRED)

        # ── 2. 收集已有限价单信息，按 (symbol, side) 索引 ──
        existing_limits: dict[str, dict] = {}
        for sid, info in self._limit_orders.items():
            if info["symbol"] == symbol:
                existing_limits[info["side"]] = {"strat_id": sid, **info}

        # ── 3. 检查已有 OPEN 持仓方向 ──
        open_sides = set()
        for o in self._tracker.get_open_orders():
            if o["symbol"] == symbol:
                open_sides.add(o["side"])

        now = now_beijing()
        registered = 0
        placed_sides: set[str] = set()

        best_new: dict[str, float] = {}
        for s in report.trade_plan.strategies:
            side = "buy" if s.strategy_type in ("pullback_long", "breakout_long") else "sell"
            rr = max(s.risk_reward, s.rr_at_trigger)
            if side not in best_new or rr > best_new[side]:
                best_new[side] = rr

        for s in report.trade_plan.strategies:
            side = "buy" if s.strategy_type in ("pullback_long", "breakout_long") else "sell"

            if side in open_sides:
                logger.debug("跳过 %s %s: 已有同方向持仓", symbol, s.strategy_type)
                continue

            if side in placed_sides:
                logger.debug("跳过 %s %s: 本轮已有同方向单", symbol, s.strategy_type)
                continue

            # ── 限价单智能对比（只用本方向最优新策略的 R:R 对比） ──
            if side in existing_limits:
                old = existing_limits[side]
                old_rr = old.get("rr_at_trigger", 0)
                new_best_rr = best_new.get(side, 0)

                if new_best_rr > old_rr:
                    logger.info(
                        "新信号优于旧限价单 [%s] %s: R:R %.2f→%.2f，替换",
                        symbol, side, old_rr, new_best_rr,
                    )
                    await self._cancel_limit_order(old["strat_id"], f"新信号R:R更优({old_rr:.2f}→{new_best_rr:.2f})")
                    del existing_limits[side]
                else:
                    logger.info(
                        "旧限价单更优 [%s] %s: 旧R:R=%.2f >= 新最优R:R=%.2f，保留旧单",
                        symbol, side, old_rr, new_best_rr,
                    )
                    placed_sides.add(side)
                    continue

            # ── 反方向：取消对侧旧限价单 ──
            opposite = "sell" if side == "buy" else "buy"
            if opposite in existing_limits:
                old_opp = existing_limits.pop(opposite)
                await self._cancel_limit_order(
                    old_opp["strat_id"],
                    f"方向反转({opposite}→{side})",
                )

            effective_rr = max(s.risk_reward, s.rr_at_trigger)
            if effective_rr < MIN_RISK_REWARD_HYBRID:
                continue

            if self._config.enable_limit_orders:
                ok = await self._place_limit_order(s, report, side, now)
                if ok:
                    registered += 1
                    placed_sides.add(side)
            else:
                if s.risk_reward < self._config.min_risk_reward:
                    continue
                self._register_pending(s, report, side, now)
                registered += 1
                placed_sides.add(side)

        if registered:
            logger.info(
                "注册 %d 条策略 [%s] (信心度 %.0f%%, 强度 %s)",
                registered, symbol, report.confidence, report.signal_strength.value,
            )

        if self._config.enable_signal_export:
            self._auto_archive_signal(symbol, report)

    async def on_price_tick(self, symbol: str, price: float) -> None:
        """接收哨兵价格回调，检查是否触发条件单"""
        if not self._config.enabled or not self._initialized:
            return
        if not self._pending:
            return

        now = now_beijing()
        today_str = now.strftime("%Y-%m-%d")
        self._guard.reset_daily(today_str)

        triggered = []
        for sid, p in list(self._pending.items()):
            if p.symbol != symbol:
                continue
            if now >= p.valid_until:
                self._expire_order(sid, p)
                continue
            if self._is_triggered(p, price):
                triggered.append((sid, p))

        for sid, p in triggered:
            del self._pending[sid]
            await self._execute_strategy(p, price)

    def _is_triggered(self, p: PendingStrategy, price: float) -> bool:
        if p.side == "buy":
            return price <= p.trigger_price
        else:
            return price >= p.trigger_price

    # ── 策略注册（复用内部逻辑） ──

    def _register_pending(
        self, s, report: "SignalReport", side: str, now: datetime,
    ) -> None:
        """注册为 PENDING 策略，等哨兵价格触发后市价执行"""
        strat_id = str(uuid.uuid4())

        pending = PendingStrategy(
            id=strat_id,
            signal_id=report.id,
            symbol=report.symbol,
            strategy_type=s.strategy_type,
            side=side,
            trigger_price=s.trigger_price,
            entry_low=s.entry_low,
            entry_high=s.entry_high,
            stop_loss=s.stop_loss,
            take_profit_1=s.take_profit_1,
            take_profit_2=s.take_profit_2,
            risk_reward=s.risk_reward,
            leverage=self._config.default_leverage,
            confidence=report.confidence,
            signal_strength=report.signal_strength.value,
            valid_until=now + timedelta(hours=s.valid_hours),
            position_size_label=s.position_size.value,
            market_state=report.market_state.value,
            created_at=now,
            tp_mode=s.tp_mode,
            trailing_callback_pct=s.trailing_callback_pct,
            tp1_close_ratio=s.tp1_close_ratio,
        )
        self._pending[strat_id] = pending
        self._save_order_record(pending, OrderStatus.PENDING)

    async def _place_limit_order(
        self, s, report: "SignalReport", side: str, now: datetime,
    ) -> bool:
        """在交易所挂限价单，统一用于所有远离当前价的策略"""
        strat_id = str(uuid.uuid4())
        effective_rr = max(s.risk_reward, s.rr_at_trigger)

        pending = PendingStrategy(
            id=strat_id,
            signal_id=report.id,
            symbol=report.symbol,
            strategy_type=s.strategy_type,
            side=side,
            trigger_price=s.trigger_price,
            entry_low=s.entry_low,
            entry_high=s.entry_high,
            stop_loss=s.stop_loss,
            take_profit_1=s.take_profit_1,
            take_profit_2=s.take_profit_2,
            risk_reward=effective_rr,
            leverage=self._config.default_leverage,
            confidence=report.confidence,
            signal_strength=report.signal_strength.value,
            valid_until=now + timedelta(hours=s.valid_hours),
            position_size_label=s.position_size.value,
            market_state=report.market_state.value,
            created_at=now,
            tp_mode=s.tp_mode,
            trailing_callback_pct=s.trailing_callback_pct,
            tp1_close_ratio=s.tp1_close_ratio,
        )

        check = await self._guard.pre_trade_check(pending, self._client)
        if not check.passed:
            logger.info(
                "限价单风控拒绝 [%s] %s: %s",
                report.symbol, s.strategy_type,
                check.detail,
            )
            self._save_order_record(pending, OrderStatus.FAILED,
                                    reject_reason=f"{check.reason.value}: {check.detail}" if check.reason else check.detail)
            return False

        if not self._config.auto_execute:
            logger.info(
                "限价单策略就绪但未开启自动执行 [%s] %s @ %.2f (R:R@trigger=%.2f)",
                report.symbol, s.strategy_type, s.trigger_price, s.rr_at_trigger,
            )
            self._save_order_record(pending, OrderStatus.FAILED,
                                    reject_reason="auto_execute=false，仅记录")
            return False

        strength_offset = {"strong": 0.08, "medium": 0.15, "weak": 0.25, "critical": 0.05}
        offset_pct = strength_offset.get(
            getattr(s, "trigger_strength", "medium"), 0.15,
        ) / 100
        offset_pct = max(offset_pct, self._config.limit_order_price_buffer_pct / 100)

        if side == "buy":
            limit_price = round(s.trigger_price * (1 + offset_pct), 2)
        else:
            limit_price = round(s.trigger_price * (1 - offset_pct), 2)

        amount = await self._calc_order_amount(
            check.suggested_amount_usd, limit_price, pending.leverage, report.symbol,
        )

        result = await self._client.place_order_with_sl_tp(
            symbol=report.symbol,
            side=side,
            amount=amount,
            price=limit_price,
            stop_loss=s.stop_loss,
            take_profit=None,
            leverage=pending.leverage,
        )

        if result["ok"]:
            exchange_oid = result.get("order_id", "")
            self._save_order_record(
                pending, OrderStatus.LIMIT_PENDING,
                exchange_order_id=exchange_oid,
                entry_price=limit_price,
                quantity=amount,
            )
            self._limit_orders[strat_id] = {
                "exchange_order_id": exchange_oid,
                "symbol": report.symbol,
                "side": side,
                "valid_until": pending.valid_until,
                "trigger_price": s.trigger_price,
                "stop_loss": s.stop_loss,
                "tp_mode": s.tp_mode,
                "trailing_callback_pct": s.trailing_callback_pct,
                "tp1_close_ratio": s.tp1_close_ratio,
                "rr_at_trigger": s.rr_at_trigger,
            }
            logger.info(
                "限价单已挂出 [%s] %s %s qty=%.4f @ %.2f | SL=%.2f | R:R=%.2f",
                report.symbol, s.strategy_type, side, amount, limit_price,
                s.stop_loss, effective_rr,
            )
            return True
        else:
            self._save_order_record(
                pending, OrderStatus.FAILED,
                reject_reason=f"限价单下单失败: {result.get('error', '')}",
            )
            return False

    def _save_order_record(
        self, p: PendingStrategy, status: OrderStatus, **kwargs,
    ) -> None:
        """统一创建 OrderRecord 并持久化"""
        order = OrderRecord(
            id=p.id,
            signal_id=p.signal_id,
            symbol=p.symbol,
            strategy_type=p.strategy_type,
            side=p.side,
            status=status,
            trigger_price=p.trigger_price,
            stop_loss=p.stop_loss,
            take_profit_1=p.take_profit_1,
            take_profit_2=p.take_profit_2,
            risk_reward=p.risk_reward,
            leverage=p.leverage,
            created_at=now_beijing().isoformat(),
            tp_mode=p.tp_mode,
            trailing_callback_pct=p.trailing_callback_pct,
            tp1_close_ratio=p.tp1_close_ratio,
            exchange_order_id=kwargs.get("exchange_order_id", ""),
            entry_price=kwargs.get("entry_price", 0.0),
            quantity=kwargs.get("quantity", 0.0),
            reject_reason=kwargs.get("reject_reason", ""),
        )
        self._tracker.save_order(order)

    async def _calc_order_amount(
        self, suggested_usd: float, entry_price: float, leverage: int, symbol: str,
    ) -> float:
        """计算下单数量，确保满足交易所最低和配置最低"""
        amount = suggested_usd * leverage / entry_price

        min_exchange = await self._client.get_min_order_amount(symbol)
        min_config = self._config.min_order_usd * leverage / entry_price
        effective_min = max(min_exchange or 0, min_config)

        if amount < effective_min:
            amount = effective_min

        return round(amount, 6)

    # ── 执行逻辑 ──

    async def _execute_strategy(self, p: PendingStrategy, current_price: float) -> None:
        now_iso = now_beijing().isoformat()

        self._tracker.update_status(p.id, OrderStatus.TRIGGERED, triggered_at=now_iso)

        check = await self._guard.pre_trade_check(p, self._client)
        if not check.passed:
            logger.warning(
                "风控拒绝 [%s] %s %s: %s — %s",
                p.symbol, p.strategy_type, p.side, check.reason, check.detail,
            )
            self._tracker.update_status(
                p.id, OrderStatus.FAILED,
                reject_reason=f"{check.reason.value}: {check.detail}" if check.reason else check.detail,
            )
            return

        if not self._config.auto_execute:
            logger.info(
                "策略触发但未开启自动执行 [%s] %s %s @ %.2f",
                p.symbol, p.strategy_type, p.side, current_price,
            )
            self._tracker.update_status(
                p.id, OrderStatus.FAILED,
                reject_reason="auto_execute=false，仅记录",
            )
            return

        entry_price = (p.entry_low + p.entry_high) / 2
        amount = await self._calc_order_amount(
            check.suggested_amount_usd, entry_price, p.leverage, p.symbol,
        )

        result = await self._client.place_order_with_sl_tp(
            symbol=p.symbol,
            side=p.side,
            amount=amount,
            price=round(entry_price, 2),
            stop_loss=p.stop_loss,
            take_profit=p.take_profit_1,
            leverage=p.leverage,
        )

        if result["ok"]:
            self._tracker.update_status(
                p.id, OrderStatus.OPEN,
                entry_price=entry_price,
                quantity=amount,
                exchange_order_id=result.get("order_id", ""),
                opened_at=now_iso,
            )
            logger.info(
                "开仓成功 [%s] %s %s qty=%.4f @ %.2f | SL=%.2f TP=%.2f",
                p.symbol, p.strategy_type, p.side, amount, entry_price,
                p.stop_loss, p.take_profit_1,
            )
        else:
            self._tracker.update_status(
                p.id, OrderStatus.FAILED,
                reject_reason=f"下单失败: {result.get('error', '')}",
            )

    # ── 订单状态同步 ──

    async def _order_sync_loop(self) -> None:
        """定期同步交易所订单状态，检测限价单成交，执行移动止损"""
        await asyncio.sleep(30)
        while True:
            try:
                await self._sync_limit_orders()
                await self._sync_open_orders()
                await self._trailing_stop_check()
            except Exception as e:
                logger.warning("订单同步异常: %s", e)
            await asyncio.sleep(60)

    async def _sync_limit_orders(self) -> None:
        """检查限价单是否成交或需要超时取消"""
        if not self._limit_orders:
            return

        now = now_beijing()
        now_iso = now.isoformat()

        for sid, info in list(self._limit_orders.items()):
            if now >= info["valid_until"]:
                await self._cancel_limit_order(sid, "超时")
                continue

            status = await self._client.get_order_status(
                info["symbol"], info["exchange_order_id"],
            )

            if status["status"] == "closed":
                entry_price = status["avg_price"] or info["trigger_price"]
                filled = status["filled"]
                self._tracker.update_status(
                    sid, OrderStatus.OPEN,
                    entry_price=entry_price,
                    quantity=filled,
                    opened_at=now_iso,
                )
                del self._limit_orders[sid]
                logger.info(
                    "限价单成交 [%s] %s @ %.2f qty=%.4f",
                    info["symbol"], info["side"], entry_price, filled,
                )
                await self._set_tp_on_fill(sid, info)
            elif status["status"] == "canceled":
                self._tracker.update_status(
                    sid, OrderStatus.LIMIT_CANCELLED,
                    reject_reason="交易所侧取消",
                )
                del self._limit_orders[sid]

    async def _cancel_limit_order(self, strat_id: str, reason: str) -> None:
        """取消限价单（先检查是否已成交，防止竞态）"""
        info = self._limit_orders.pop(strat_id, None)
        if not info:
            return

        status = await self._client.get_order_status(
            info["symbol"], info["exchange_order_id"],
        )

        if status["status"] == "closed":
            entry_price = status["avg_price"] or info["trigger_price"]
            self._tracker.update_status(
                strat_id, OrderStatus.OPEN,
                entry_price=entry_price,
                quantity=status["filled"],
                opened_at=now_beijing().isoformat(),
            )
            logger.info(
                "限价单取消前已成交 [%s] @ %.2f",
                info["symbol"], entry_price,
            )
            await self._set_tp_on_fill(strat_id, info)
            return

        await self._client.cancel_order(info["symbol"], info["exchange_order_id"])
        self._tracker.update_status(
            strat_id, OrderStatus.LIMIT_CANCELLED,
            reject_reason=f"限价单取消: {reason}",
        )
        logger.info("取消限价单 [%s] %s: %s", info["symbol"], strat_id[:8], reason)

    async def _set_tp_on_fill(self, strat_id: str, info: dict) -> None:
        """限价单成交后，立即在交易所设置 TP1 止盈单。

        hybrid 模式：TP1 平 close_ratio 部分仓位，剩余由移动止盈管理。
        fixed 模式：TP1 平全部仓位。
        """
        order = self._tracker.get_order(strat_id)
        if not order:
            return

        tp1 = order.get("take_profit_1", 0)
        if not tp1 or tp1 <= 0:
            return

        symbol = info["symbol"]
        side = info["side"]
        tp_mode = info.get("tp_mode", "hybrid")
        close_ratio = info.get("tp1_close_ratio", 0.5)

        if tp_mode == "hybrid":
            ok = await self._client.set_take_profit(
                symbol, side, tp1, close_ratio=close_ratio,
            )
        else:
            ok = await self._client.set_take_profit(
                symbol, side, tp1, close_ratio=1.0,
            )

        if ok:
            logger.info(
                "限价单成交后设置TP1 [%s] %s TP=%.2f (平%.0f%%)",
                symbol, side, tp1, (close_ratio if tp_mode == "hybrid" else 1.0) * 100,
            )
        else:
            logger.warning(
                "限价单成交后设置TP1失败 [%s]，将由移动止盈程序兜底",
                symbol,
            )

    async def _recover_limit_orders(self) -> None:
        """启动时恢复或清理未完成的限价单"""
        pending_limits = self._tracker.get_orders_by_status("limit_pending")
        if not pending_limits:
            return

        logger.info("恢复 %d 条未完成限价单...", len(pending_limits))
        for order in pending_limits:
            eid = order.get("exchange_order_id", "")
            sid = order["id"]
            symbol = order["symbol"]

            if not eid:
                self._tracker.update_status(
                    sid, OrderStatus.LIMIT_CANCELLED,
                    reject_reason="系统重启: 无交易所订单号",
                )
                continue

            status = await self._client.get_order_status(symbol, eid)

            if status["status"] == "closed":
                entry_price = status["avg_price"] or order.get("trigger_price", 0)
                self._tracker.update_status(
                    sid, OrderStatus.OPEN,
                    entry_price=entry_price,
                    quantity=status["filled"],
                    opened_at=now_beijing().isoformat(),
                )
                logger.info("恢复限价单(已成交) [%s] @ %.2f", symbol, entry_price)
                recover_info = {
                    "symbol": symbol,
                    "side": order.get("side", "buy"),
                    "tp_mode": order.get("tp_mode", "hybrid"),
                    "tp1_close_ratio": order.get("tp1_close_ratio", 0.5),
                }
                await self._set_tp_on_fill(sid, recover_info)
            elif status["status"] == "open":
                valid_until_str = order.get("created_at", "")
                try:
                    created = datetime.fromisoformat(valid_until_str)
                    valid_until = created + timedelta(hours=24)
                except (ValueError, TypeError):
                    valid_until = now_beijing() + timedelta(hours=4)

                self._limit_orders[sid] = {
                    "exchange_order_id": eid,
                    "symbol": symbol,
                    "side": order.get("side", "buy"),
                    "valid_until": valid_until,
                    "trigger_price": order.get("trigger_price", 0),
                    "stop_loss": order.get("stop_loss", 0),
                    "tp_mode": order.get("tp_mode", "hybrid"),
                    "trailing_callback_pct": order.get("trailing_callback_pct", 1.0),
                    "tp1_close_ratio": order.get("tp1_close_ratio", 0.5),
                    "rr_at_trigger": order.get("risk_reward", 0),
                }
                logger.info("恢复限价单(仍挂出) [%s] eid=%s", symbol, eid[:12])
            else:
                await self._client.cancel_order(symbol, eid)
                self._tracker.update_status(
                    sid, OrderStatus.LIMIT_CANCELLED,
                    reject_reason="系统重启清理",
                )

    async def _sync_open_orders(self) -> None:
        """检查 open 状态的订单是否已被交易所平仓（SL/TP 触发）"""
        open_orders = self._tracker.get_open_orders()
        if not open_orders:
            return

        positions = await self._client.get_positions()
        pos_map = {(p["symbol"], p["side"]): p for p in positions}

        for order in open_orders:
            symbol_swap = order["symbol"].replace("/USDT", "/USDT:USDT")
            side = "long" if order["side"] == "buy" else "short"
            key = (symbol_swap, side)

            if key not in pos_map:
                await self._close_order_by_market(order)

    async def _close_order_by_market(self, order: dict) -> None:
        """交易所持仓已消失（SL/TP 被触发），基于阶段和成交记录判定平仓原因。"""
        now_iso = now_beijing().isoformat()
        entry = order.get("entry_price", 0) or 0
        sl = order.get("stop_loss", 0) or 0
        tp1 = order.get("take_profit_1", 0) or 0
        tp1_triggered = bool(order.get("tp1_triggered_at"))
        is_long = order["side"] == "buy"

        close_price = await self._get_actual_close_price(order)

        if entry <= 0:
            status = OrderStatus.CLOSED_MANUAL
        elif tp1_triggered:
            status = OrderStatus.CLOSED_TP2
        elif is_long:
            if close_price <= sl * 1.005:
                status = OrderStatus.CLOSED_SL
            elif close_price >= tp1 * 0.995:
                status = OrderStatus.CLOSED_TP1
            else:
                status = OrderStatus.CLOSED_MANUAL
        else:
            if close_price >= sl * 0.995:
                status = OrderStatus.CLOSED_SL
            elif close_price <= tp1 * 1.005:
                status = OrderStatus.CLOSED_TP1
            else:
                status = OrderStatus.CLOSED_MANUAL

        if entry > 0:
            pnl_pct = ((close_price - entry) / entry * 100) if is_long else ((entry - close_price) / entry * 100)
        else:
            pnl_pct = 0.0

        qty = order.get("quantity", 0) or 0
        pnl_usd = qty * entry * (pnl_pct / 100) if entry > 0 else 0
        won = pnl_usd > 0

        self._tracker.update_status(
            order["id"], status,
            pnl_usd=round(pnl_usd, 2),
            pnl_pct=round(pnl_pct, 2),
            close_price=round(close_price, 2),
            closed_at=now_iso,
        )
        self._tracker.update_daily_stats(pnl_usd, won)
        self._guard.record_pnl(pnl_usd)

        label = status.value.replace("closed_", "").upper()
        phase = "移动止盈阶段" if tp1_triggered else "TP1前阶段"
        logger.info(
            "平仓确认 [%s] %s %s → %s (%s) | 平仓价=%.2f 入场价=%.2f SL=%.2f | PnL: $%.2f (%.2f%%)",
            order["symbol"], order["strategy_type"], order["side"],
            label, phase, close_price, entry, sl, pnl_usd, pnl_pct,
        )

        if self._config.enable_signal_export:
            self._auto_archive_trade(order["id"])

    async def _get_actual_close_price(self, order: dict) -> float:
        """尝试从交易所成交记录获取实际平仓价，回退到当前市场价。"""
        try:
            fills = await self._client.get_recent_fills(order["symbol"], limit=10)
            if fills:
                close_side = "sell" if order["side"] == "buy" else "buy"
                close_fills = [f for f in fills if f["side"] == close_side]
                if close_fills:
                    latest = max(close_fills, key=lambda f: f["timestamp"])
                    return latest["price"]
        except Exception as e:
            logger.debug("获取平仓成交价失败 %s: %s", order["symbol"], e)

        return await self._client.get_market_price(order["symbol"])

    # ── 信号存档 ──

    def _auto_archive_signal(self, symbol: str, report: "SignalReport") -> None:
        """自动存档信号报告到 data/exports/signals/ 目录"""
        try:
            base = Path(__file__).parent.parent / "data" / "exports" / "signals"
            date_dir = base / now_beijing().strftime("%Y-%m-%d")
            date_dir.mkdir(parents=True, exist_ok=True)

            safe_sym = symbol.replace("/", "_")
            ts = now_beijing().strftime("%H%M%S")
            filepath = date_dir / f"{safe_sym}_{ts}.json"

            data = {
                "id": report.id,
                "symbol": symbol,
                "timestamp": now_beijing().isoformat(),
                "direction": report.direction.value,
                "confidence": report.confidence,
                "signal_strength": report.signal_strength.value,
                "market_state": report.market_state.value,
                "total_score": report.total_score,
                "max_score": report.max_possible_score,
                "strategies": [],
            }
            if report.trade_plan:
                for s in report.trade_plan.strategies:
                    data["strategies"].append({
                        "type": s.strategy_type,
                        "trigger": s.trigger_price,
                        "entry": [s.entry_low, s.entry_high],
                        "sl": s.stop_loss,
                        "tp1": s.take_profit_1,
                        "tp2": s.take_profit_2,
                        "rr": s.risk_reward,
                        "rr_at_trigger": s.rr_at_trigger,
                        "size": s.position_size.value,
                        "tp_mode": s.tp_mode,
                        "trailing_callback_pct": s.trailing_callback_pct,
                        "tp1_close_ratio": s.tp1_close_ratio,
                    })

            filepath.write_text(json.dumps(data, ensure_ascii=False, indent=2))
            logger.debug("信号已存档: %s", filepath)
        except Exception as e:
            logger.warning("信号存档失败: %s", e)

    def _auto_archive_trade(self, order_id: str) -> None:
        """自动存档执行记录到 data/exports/trades/ 目录"""
        try:
            base = Path(__file__).parent.parent / "data" / "exports" / "trades"
            date_dir = base / now_beijing().strftime("%Y-%m-%d")
            date_dir.mkdir(parents=True, exist_ok=True)
            filepath = date_dir / f"{order_id[:8]}.json"

            record = self._tracker.get_order(order_id)
            if record:
                filepath.write_text(json.dumps(record, ensure_ascii=False, indent=2))
                logger.debug("交易记录已存档: %s", filepath)
        except Exception as e:
            logger.warning("交易记录存档失败: %s", e)

    # ── 内部工具 ──

    async def _expire_symbol(self, symbol: str) -> None:
        """清理该 symbol 的所有 pending 策略和限价单"""
        to_remove = [sid for sid, p in self._pending.items() if p.symbol == symbol]
        for sid in to_remove:
            self._pending.pop(sid)
            self._tracker.update_status(sid, OrderStatus.EXPIRED)

        limit_to_cancel = [
            sid for sid, info in self._limit_orders.items()
            if info["symbol"] == symbol
        ]
        for sid in limit_to_cancel:
            await self._cancel_limit_order(sid, "新信号覆盖")

    def _expire_order(self, sid: str, p: PendingStrategy) -> None:
        self._pending.pop(sid, None)
        self._tracker.update_status(sid, OrderStatus.EXPIRED)
        logger.debug("策略过期: %s %s %s", p.symbol, p.strategy_type, sid[:8])

    async def _trailing_stop_check(self) -> None:
        """混合止盈移动止损逻辑：

        阶段一（TP1 前）：SL 不变，等待 TP1
        阶段二（TP1 触发时）：部分平仓 tp1_close_ratio，SL 移至入场价
        阶段三（TP1 后持续）：跟踪极值价格，按 trailing_callback_pct 更新 SL
        """
        if not self._config.enable_trailing_stop:
            return

        open_orders = self._tracker.get_open_orders()
        if not open_orders:
            return

        now_iso = now_beijing().isoformat()

        for order in open_orders:
            entry = order.get("entry_price", 0) or 0
            tp1 = order.get("take_profit_1", 0) or 0
            if entry <= 0 or tp1 <= 0:
                continue

            side = order["side"]
            price = await self._client.get_market_price(order["symbol"])
            if not price:
                continue

            tp_mode = order.get("tp_mode", "hybrid")
            callback_pct = order.get("trailing_callback_pct", 1.0) or 1.0
            close_ratio = order.get("tp1_close_ratio", 0.5) or 0.5
            tp1_triggered = bool(order.get("tp1_triggered_at"))
            highest = order.get("highest_price", 0) or 0

            is_long = side == "buy"
            tp1_reached = (is_long and price >= tp1) or (not is_long and price <= tp1)

            # ── 阶段一 → 阶段二：TP1 首次触发 ──
            # TP1 部分平仓由交易所 algo 自动执行（_set_tp_on_fill 已设好）
            # 程序只负责：① 移 SL 到入场价（盈亏平衡）② 开始追踪极值
            if tp1_reached and not tp1_triggered:
                new_sl = entry
                await self._amend_sl_on_exchange(order, new_sl)

                extreme = price
                self._tracker.update_status(
                    order["id"], OrderStatus.OPEN,
                    stop_loss=new_sl,
                    tp1_triggered_at=now_iso,
                    highest_price=extreme,
                    trailing_sl=new_sl,
                )
                pnl_at_tp1 = ((price - entry) / entry * 100) if is_long else ((entry - price) / entry * 100)
                logger.info(
                    "TP1触发 [%s] %s: 当前价=%.2f, 入场=%.2f, 浮盈=%.2f%% | "
                    "SL→%.2f(盈亏平衡) | TP1由交易所algo平%.0f%%, 剩余走移动止盈(回调%.1f%%)",
                    order["symbol"], side, price, entry, pnl_at_tp1,
                    new_sl, close_ratio * 100, callback_pct,
                )
                continue

            # ── 阶段三：TP1 之后的移动止盈 ──
            if tp1_triggered and tp_mode == "hybrid":
                if is_long:
                    new_highest = max(highest, price)
                    trail_sl = new_highest * (1 - callback_pct / 100)
                else:
                    new_highest = min(highest, price) if highest > 0 else price
                    trail_sl = new_highest * (1 + callback_pct / 100)

                current_trail_sl = order.get("trailing_sl", 0) or 0

                sl_improved = (
                    (is_long and trail_sl > current_trail_sl)
                    or (not is_long and (current_trail_sl == 0 or trail_sl < current_trail_sl))
                )

                pnl_now = ((price - entry) / entry * 100) if is_long else ((entry - price) / entry * 100)

                if sl_improved:
                    await self._amend_sl_on_exchange(order, trail_sl)
                    self._tracker.update_status(
                        order["id"], OrderStatus.OPEN,
                        highest_price=new_highest,
                        trailing_sl=round(trail_sl, 2),
                        stop_loss=round(trail_sl, 2),
                    )
                    logger.info(
                        "移动止盈更新 [%s] %s: 当前价=%.2f(浮盈%.2f%%) | "
                        "极值%.2f→%.2f, SL %.2f→%.2f",
                        order["symbol"], side, price, pnl_now,
                        highest, new_highest,
                        current_trail_sl, trail_sl,
                    )
                elif new_highest != highest:
                    self._tracker.update_status(
                        order["id"], OrderStatus.OPEN,
                        highest_price=new_highest,
                    )

    async def _amend_sl_on_exchange(self, order: dict, new_sl: float) -> None:
        """尝试在交易所修改止损单"""
        algo_orders = await self._client.fetch_algo_orders(order["symbol"])
        pos_side = "long" if order["side"] == "buy" else "short"
        sl_algo = next(
            (a for a in algo_orders
             if a.get("posSide") == pos_side and a.get("slTriggerPx")),
            None,
        )
        if sl_algo:
            ok = await self._client.amend_stop_loss(
                order["symbol"], sl_algo["algoId"], new_sl,
            )
            if not ok:
                logger.warning("交易所修改SL失败 %s → %.2f", order["symbol"], new_sl)
        else:
            logger.debug(
                "未找到交易所算法单，仅本地更新SL %s → %.2f", order["symbol"], new_sl,
            )

    def export_trade_log(self, limit: int = 200) -> list[dict]:
        """导出交易日志（用于前端下载）"""
        history = self._tracker.get_history(limit=limit)
        return [
            {
                "时间": o.get("closed_at", o.get("created_at", "")),
                "交易对": o.get("symbol", ""),
                "策略": o.get("strategy_type", ""),
                "方向": "多" if o.get("side") == "buy" else "空",
                "状态": o.get("status", ""),
                "入场价": o.get("entry_price", 0),
                "平仓价": o.get("close_price", 0),
                "止损": o.get("stop_loss", 0),
                "止盈": o.get("take_profit_1", 0),
                "盈亏比": o.get("risk_reward", 0),
                "盈亏(USD)": o.get("pnl_usd", 0),
                "盈亏(%)": o.get("pnl_pct", 0),
                "拒绝原因": o.get("reject_reason", ""),
            }
            for o in history
        ]

    def get_status(self) -> dict:
        """返回执行层整体状态"""
        today_stats = self._tracker.get_today_stats()
        overall = self._tracker.get_overall_stats()
        return {
            "enabled": self._config.enabled,
            "initialized": self._initialized,
            "mode": self._config.mode,
            "auto_execute": self._config.auto_execute,
            "exchange": self._config.exchange,
            "pending_strategies": len(self._pending),
            "limit_orders": len(self._limit_orders),
            "today": today_stats,
            "overall": overall,
        }
