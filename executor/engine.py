"""执行引擎。

管理待触发策略队列，匹配哨兵价格回调，协调风控和下单。
是信号层（生产者）和交易所（消费者）之间的唯一桥梁。
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
from core.constants import SignalStrength
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
        return len(self._pending)

    async def initialize(self) -> None:
        if not self._config.enabled:
            logger.info("执行层已禁用")
            return
        if not self._config.api_key:
            logger.warning("执行层已启用但 API Key 为空，跳过初始化")
            return

        await self._client.initialize()
        self._initialized = True

        self._order_sync_task = asyncio.create_task(self._order_sync_loop())
        logger.info(
            "执行引擎启动: %s | 自动执行=%s | 杠杆=%dx",
            self._client.mode_label,
            self._config.auto_execute,
            self._config.default_leverage,
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

        # 清理旧的 pending（同 symbol）
        self._expire_symbol(symbol)

        now = now_beijing()
        registered = 0

        for s in report.trade_plan.strategies:
            if s.position_size.value == "skip":
                continue
            if s.risk_reward < self._config.min_risk_reward:
                continue

            side = "buy" if s.strategy_type in ("pullback_long", "breakout_long") else "sell"
            strat_id = str(uuid.uuid4())

            pending = PendingStrategy(
                id=strat_id,
                signal_id=report.id,
                symbol=symbol,
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
            )
            self._pending[strat_id] = pending

            order = OrderRecord(
                id=strat_id,
                signal_id=report.id,
                symbol=symbol,
                strategy_type=s.strategy_type,
                side=side,
                status=OrderStatus.PENDING,
                trigger_price=s.trigger_price,
                stop_loss=s.stop_loss,
                take_profit_1=s.take_profit_1,
                take_profit_2=s.take_profit_2,
                risk_reward=s.risk_reward,
                leverage=self._config.default_leverage,
                created_at=now.isoformat(),
            )
            self._tracker.save_order(order)
            registered += 1

        if registered:
            logger.info(
                "注册 %d 条待触发策略 [%s] (信心度 %.0f%%, 强度 %s)",
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
        amount_usd = check.suggested_amount_usd
        amount = amount_usd * p.leverage / entry_price

        min_amount = await self._client.get_min_order_amount(p.symbol)
        if min_amount and amount < min_amount:
            amount = min_amount

        result = await self._client.place_order_with_sl_tp(
            symbol=p.symbol,
            side=p.side,
            amount=round(amount, 6),
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
        """定期同步交易所订单状态，检测 SL/TP 触发，执行移动止损"""
        await asyncio.sleep(30)
        while True:
            try:
                await self._sync_open_orders()
                await self._trailing_stop_check()
            except Exception as e:
                logger.debug("订单同步异常: %s", e)
            await asyncio.sleep(60)

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
        """交易所持仓已消失（SL/TP 被触发），更新本地状态"""
        now_iso = now_beijing().isoformat()
        market_price = await self._client.get_market_price(order["symbol"])

        entry = order.get("entry_price", 0) or 0
        sl = order.get("stop_loss", 0) or 0
        tp1 = order.get("take_profit_1", 0) or 0

        if entry <= 0:
            status = OrderStatus.CLOSED_MANUAL
            pnl_pct = 0.0
        elif order["side"] == "buy":
            if market_price <= sl * 1.005:
                status = OrderStatus.CLOSED_SL
            elif market_price >= tp1 * 0.995:
                status = OrderStatus.CLOSED_TP1
            else:
                status = OrderStatus.CLOSED_MANUAL
            pnl_pct = (market_price - entry) / entry * 100
        else:
            if market_price >= sl * 0.995:
                status = OrderStatus.CLOSED_SL
            elif market_price <= tp1 * 1.005:
                status = OrderStatus.CLOSED_TP1
            else:
                status = OrderStatus.CLOSED_MANUAL
            pnl_pct = (entry - market_price) / entry * 100

        qty = order.get("quantity", 0) or 0
        pnl_usd = qty * entry * (pnl_pct / 100) if entry > 0 else 0
        won = pnl_usd > 0

        self._tracker.update_status(
            order["id"], status,
            pnl_usd=round(pnl_usd, 2),
            pnl_pct=round(pnl_pct, 2),
            closed_at=now_iso,
        )
        self._tracker.update_daily_stats(pnl_usd, won)
        self._guard.record_pnl(pnl_usd)

        label = status.value.replace("closed_", "").upper()
        logger.info(
            "平仓检测 [%s] %s %s → %s | PnL: $%.2f (%.2f%%)",
            order["symbol"], order["strategy_type"], order["side"],
            label, pnl_usd, pnl_pct,
        )

        if self._config.enable_signal_export:
            self._auto_archive_trade(order["id"])

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
                        "size": s.position_size.value,
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

            history = self._tracker.get_history(limit=500)
            record = next((h for h in history if h.get("id") == order_id), None)
            if record:
                filepath.write_text(json.dumps(record, ensure_ascii=False, indent=2))
                logger.debug("交易记录已存档: %s", filepath)
        except Exception as e:
            logger.warning("交易记录存档失败: %s", e)

    # ── 内部工具 ──

    def _expire_symbol(self, symbol: str) -> None:
        """清理该 symbol 的所有 pending 策略"""
        to_remove = [sid for sid, p in self._pending.items() if p.symbol == symbol]
        for sid in to_remove:
            p = self._pending.pop(sid)
            self._tracker.update_status(sid, OrderStatus.EXPIRED)

    def _expire_order(self, sid: str, p: PendingStrategy) -> None:
        self._pending.pop(sid, None)
        self._tracker.update_status(sid, OrderStatus.EXPIRED)
        logger.debug("策略过期: %s %s %s", p.symbol, p.strategy_type, sid[:8])

    async def _trailing_stop_check(self) -> None:
        """移动止损：当价格到达 TP1 后，将 SL 移动到入场价（盈亏平衡点）。
        通过交易所 API 修改真实止损单。
        """
        if not self._config.enable_trailing_stop:
            return

        open_orders = self._tracker.get_open_orders()
        if not open_orders:
            return

        for order in open_orders:
            entry = order.get("entry_price", 0) or 0
            tp1 = order.get("take_profit_1", 0) or 0
            sl = order.get("stop_loss", 0) or 0
            if entry <= 0 or tp1 <= 0:
                continue
            if entry > 0 and abs(sl - entry) / entry < 0.001:
                continue

            price = await self._client.get_market_price(order["symbol"])
            if not price:
                continue

            side = order["side"]
            tp1_reached = (side == "buy" and price >= tp1) or (side == "sell" and price <= tp1)

            if not tp1_reached:
                continue

            new_sl = entry
            algo_orders = await self._client.fetch_algo_orders(order["symbol"])
            pos_side = "long" if side == "buy" else "short"
            sl_algo = next(
                (a for a in algo_orders
                 if a.get("posSide") == pos_side and a.get("slTriggerPx")),
                None,
            )

            if sl_algo:
                ok = await self._client.amend_stop_loss(
                    order["symbol"], sl_algo["algoId"], new_sl,
                )
                if ok:
                    self._tracker.update_status(
                        order["id"], OrderStatus.OPEN, stop_loss=new_sl,
                    )
                    logger.info(
                        "移动止损成功: %s %s SL %.2f → %.2f (盈亏平衡)",
                        order["symbol"], side, sl, new_sl,
                    )
            else:
                self._tracker.update_status(
                    order["id"], OrderStatus.OPEN, stop_loss=new_sl,
                )
                logger.info(
                    "移动止损(本地): %s %s SL %.2f → %.2f (未找到交易所算法单)",
                    order["symbol"], side, sl, new_sl,
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
            "today": today_stats,
            "overall": overall,
        }
