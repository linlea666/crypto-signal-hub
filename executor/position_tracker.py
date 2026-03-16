"""持仓状态追踪与持久化。

管理执行层订单的完整生命周期，独立使用 executor_orders / executor_daily_stats 表。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path

from typing import Generator

from core.time_utils import now_beijing
from executor.models import OrderRecord, OrderStatus

logger = logging.getLogger(__name__)

_EXECUTOR_SCHEMA = """
CREATE TABLE IF NOT EXISTS executor_orders (
    id TEXT PRIMARY KEY,
    signal_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    strategy_type TEXT NOT NULL,
    side TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    trigger_price REAL NOT NULL,
    entry_price REAL DEFAULT 0,
    stop_loss REAL DEFAULT 0,
    take_profit_1 REAL DEFAULT 0,
    take_profit_2 REAL DEFAULT 0,
    quantity REAL DEFAULT 0,
    leverage INTEGER DEFAULT 1,
    exchange_order_id TEXT DEFAULT '',
    risk_reward REAL DEFAULT 0,
    pnl_usd REAL DEFAULT 0,
    pnl_pct REAL DEFAULT 0,
    reject_reason TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    triggered_at TEXT DEFAULT '',
    opened_at TEXT DEFAULT '',
    closed_at TEXT DEFAULT '',
    tp_mode TEXT DEFAULT 'hybrid',
    trailing_callback_pct REAL DEFAULT 1.0,
    tp1_close_ratio REAL DEFAULT 0.5,
    highest_price REAL DEFAULT 0,
    trailing_sl REAL DEFAULT 0,
    tp1_triggered_at TEXT DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_exec_orders_symbol ON executor_orders(symbol);
CREATE INDEX IF NOT EXISTS idx_exec_orders_status ON executor_orders(status);
CREATE INDEX IF NOT EXISTS idx_exec_orders_created ON executor_orders(created_at);

CREATE TABLE IF NOT EXISTS executor_daily_stats (
    date TEXT PRIMARY KEY,
    total_pnl_usd REAL DEFAULT 0,
    trade_count INTEGER DEFAULT 0,
    win_count INTEGER DEFAULT 0,
    loss_count INTEGER DEFAULT 0
);
"""


class PositionTracker:
    """订单生命周期追踪器"""

    def __init__(self, db_path: Path):
        self._path = db_path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    _MIGRATION_COLUMNS = [
        ("tp_mode", "TEXT DEFAULT 'hybrid'"),
        ("trailing_callback_pct", "REAL DEFAULT 1.0"),
        ("tp1_close_ratio", "REAL DEFAULT 0.5"),
        ("highest_price", "REAL DEFAULT 0"),
        ("trailing_sl", "REAL DEFAULT 0"),
        ("tp1_triggered_at", "TEXT DEFAULT ''"),
    ]

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(_EXECUTOR_SCHEMA)
            for col_name, col_def in self._MIGRATION_COLUMNS:
                try:
                    conn.execute(f"ALTER TABLE executor_orders ADD COLUMN {col_name} {col_def}")
                except sqlite3.OperationalError:
                    pass  # column already exists
            logger.info("执行层数据库初始化完成")

    @contextmanager
    def _connect(self) -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(str(self._path))
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ── 写操作 ──

    def save_order(self, order: OrderRecord) -> None:
        with self._connect() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO executor_orders
                   (id, signal_id, symbol, strategy_type, side, status,
                    trigger_price, entry_price, stop_loss, take_profit_1, take_profit_2,
                    quantity, leverage, exchange_order_id, risk_reward,
                    pnl_usd, pnl_pct, reject_reason,
                    created_at, triggered_at, opened_at, closed_at,
                    tp_mode, trailing_callback_pct, tp1_close_ratio,
                    highest_price, trailing_sl, tp1_triggered_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    order.id, order.signal_id, order.symbol, order.strategy_type,
                    order.side, order.status.value,
                    order.trigger_price, order.entry_price,
                    order.stop_loss, order.take_profit_1, order.take_profit_2,
                    order.quantity, order.leverage, order.exchange_order_id,
                    order.risk_reward, order.pnl_usd, order.pnl_pct,
                    order.reject_reason,
                    order.created_at, order.triggered_at, order.opened_at, order.closed_at,
                    order.tp_mode, order.trailing_callback_pct, order.tp1_close_ratio,
                    order.highest_price, order.trailing_sl, order.tp1_triggered_at,
                ),
            )

    _ALLOWED_UPDATE_FIELDS = frozenset({
        "entry_price", "stop_loss", "take_profit_1", "take_profit_2",
        "quantity", "leverage", "exchange_order_id", "risk_reward",
        "pnl_usd", "pnl_pct", "reject_reason",
        "triggered_at", "opened_at", "closed_at",
        "tp_mode", "trailing_callback_pct", "tp1_close_ratio",
        "highest_price", "trailing_sl", "tp1_triggered_at",
    })

    def update_status(
        self, order_id: str, status: OrderStatus, **kwargs,
    ) -> None:
        sets = ["status = ?"]
        params: list = [status.value]
        for k, v in kwargs.items():
            if k not in self._ALLOWED_UPDATE_FIELDS:
                logger.warning("update_status: 忽略非法字段 %s", k)
                continue
            sets.append(f"{k} = ?")
            params.append(v)
        params.append(order_id)
        with self._connect() as conn:
            conn.execute(
                f"UPDATE executor_orders SET {', '.join(sets)} WHERE id = ?",
                params,
            )

    def update_daily_stats(self, pnl_usd: float, won: bool) -> None:
        today = now_beijing().strftime("%Y-%m-%d")
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO executor_daily_stats (date, total_pnl_usd, trade_count, win_count, loss_count)
                   VALUES (?, ?, 1, ?, ?)
                   ON CONFLICT(date) DO UPDATE SET
                     total_pnl_usd = total_pnl_usd + excluded.total_pnl_usd,
                     trade_count = trade_count + 1,
                     win_count = win_count + excluded.win_count,
                     loss_count = loss_count + excluded.loss_count""",
                (today, pnl_usd, 1 if won else 0, 0 if won else 1),
            )

    # ── 读操作 ──

    def get_active_orders(self) -> list[dict]:
        """获取活跃订单（pending + limit_pending + triggered + open）"""
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM executor_orders
                   WHERE status IN ('pending', 'limit_pending', 'triggered', 'open')
                   ORDER BY created_at DESC""",
            ).fetchall()
            return [dict(r) for r in rows]

    def get_orders_by_status(self, status: str) -> list[dict]:
        """按状态查询订单"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM executor_orders WHERE status = ? ORDER BY created_at DESC",
                (status,),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_open_orders(self) -> list[dict]:
        """获取已开仓的订单"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM executor_orders WHERE status = 'open' ORDER BY opened_at DESC",
            ).fetchall()
            return [dict(r) for r in rows]

    def get_pending_orders(self) -> list[dict]:
        """获取待触发的订单"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM executor_orders WHERE status = 'pending' ORDER BY created_at DESC",
            ).fetchall()
            return [dict(r) for r in rows]

    def get_history(self, limit: int = 50, offset: int = 0) -> list[dict]:
        """获取历史订单"""
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM executor_orders
                   WHERE status NOT IN ('pending', 'limit_pending', 'triggered', 'open')
                   ORDER BY closed_at DESC, created_at DESC
                   LIMIT ? OFFSET ?""",
                (limit, offset),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_order(self, order_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM executor_orders WHERE id = ?", (order_id,)
            ).fetchone()
            return dict(row) if row else None

    def get_daily_stats(self, days: int = 30) -> list[dict]:
        start_date = (now_beijing() - timedelta(days=days)).strftime("%Y-%m-%d")
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM executor_daily_stats
                   WHERE date >= ?
                   ORDER BY date DESC""",
                (start_date,),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_today_stats(self) -> dict:
        today = now_beijing().strftime("%Y-%m-%d")
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM executor_daily_stats WHERE date = ?", (today,)
            ).fetchone()
            if row:
                return dict(row)
            return {"date": today, "total_pnl_usd": 0, "trade_count": 0, "win_count": 0, "loss_count": 0}

    def count_by_status(self) -> dict:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) as cnt FROM executor_orders GROUP BY status"
            ).fetchall()
            return {r["status"]: r["cnt"] for r in rows}

    def get_overall_stats(self) -> dict:
        """全局执行统计"""
        with self._connect() as conn:
            row = conn.execute(
                """SELECT
                     COUNT(*) as total,
                     SUM(CASE WHEN status IN ('closed_tp1','closed_tp2') THEN 1 ELSE 0 END) as wins,
                     SUM(CASE WHEN status = 'closed_sl' THEN 1 ELSE 0 END) as losses,
                     SUM(CASE WHEN status IN ('closed_tp1','closed_tp2','closed_sl','closed_manual') THEN pnl_usd ELSE 0 END) as total_pnl,
                     AVG(CASE WHEN status IN ('closed_tp1','closed_tp2','closed_sl','closed_manual') THEN pnl_pct END) as avg_pnl_pct
                   FROM executor_orders
                   WHERE status NOT IN ('pending', 'limit_pending', 'triggered', 'expired', 'cancelled', 'limit_cancelled')"""
            ).fetchone()
            total = row["total"] or 0
            wins = row["wins"] or 0
            losses = row["losses"] or 0
            return {
                "total_trades": total,
                "wins": wins,
                "losses": losses,
                "win_rate": round(wins / (wins + losses) * 100, 1) if (wins + losses) > 0 else 0,
                "total_pnl_usd": round(row["total_pnl"] or 0, 2),
                "avg_pnl_pct": round(row["avg_pnl_pct"] or 0, 2),
            }
