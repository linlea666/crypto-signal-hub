"""SQLite 持久化层。

存储历史信号报告和系统运行状态。
使用纯 sqlite3 避免 ORM 依赖，SQL 语句集中管理。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Generator

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = Path(__file__).parent.parent / "data" / "signals.db"

# ── 建表语句 ──
_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS signal_reports (
    id TEXT PRIMARY KEY,
    timestamp TEXT NOT NULL,
    symbol TEXT NOT NULL,
    total_score REAL NOT NULL,
    max_score REAL NOT NULL,
    direction TEXT NOT NULL,
    confidence REAL NOT NULL,
    signal_strength TEXT NOT NULL,
    alert_type TEXT NOT NULL,
    ai_analysis TEXT DEFAULT '',
    snapshot_json TEXT NOT NULL,
    scores_json TEXT NOT NULL,
    levels_json TEXT NOT NULL,
    suggestion_json TEXT DEFAULT '',
    email_sent INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_reports_timestamp ON signal_reports(timestamp);
CREATE INDEX IF NOT EXISTS idx_reports_symbol ON signal_reports(symbol);
CREATE INDEX IF NOT EXISTS idx_reports_strength ON signal_reports(signal_strength);

CREATE TABLE IF NOT EXISTS notification_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id TEXT NOT NULL,
    channel TEXT NOT NULL,
    sent_at TEXT NOT NULL,
    success INTEGER NOT NULL,
    error_message TEXT DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_notif_sent_at ON notification_log(sent_at);
"""


class Database:
    """SQLite 数据库操作封装。

    非线程安全；Web 层通过 API 路由串行访问即可。
    如需并发可改用 WAL 模式 + 连接池。
    """

    def __init__(self, db_path: Path | None = None):
        self._path = db_path or DEFAULT_DB_PATH
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA_SQL)
            logger.info("数据库初始化完成: %s", self._path)

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

    def save_report(self, report_data: dict) -> None:
        """保存一份信号报告"""
        with self._connect() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO signal_reports
                   (id, timestamp, symbol, total_score, max_score, direction,
                    confidence, signal_strength, alert_type, ai_analysis,
                    snapshot_json, scores_json, levels_json, suggestion_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    report_data["id"],
                    report_data["timestamp"],
                    report_data["symbol"],
                    report_data["total_score"],
                    report_data["max_score"],
                    report_data["direction"],
                    report_data["confidence"],
                    report_data["signal_strength"],
                    report_data["alert_type"],
                    report_data.get("ai_analysis", ""),
                    json.dumps(report_data["snapshot"], ensure_ascii=False),
                    json.dumps(report_data["scores"], ensure_ascii=False),
                    json.dumps(report_data["levels"], ensure_ascii=False),
                    json.dumps(report_data.get("suggestion", ""), ensure_ascii=False),
                ),
            )

    def mark_email_sent(self, report_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE signal_reports SET email_sent = 1 WHERE id = ?",
                (report_id,),
            )

    def log_notification(
        self, report_id: str, channel: str, success: bool, error: str = ""
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO notification_log (report_id, channel, sent_at, success, error_message)
                   VALUES (?, ?, ?, ?, ?)""",
                (report_id, channel, datetime.now().isoformat(), int(success), error),
            )

    # ── 读操作 ──

    def get_recent_reports(
        self, symbol: str = "", limit: int = 50, offset: int = 0
    ) -> list[dict]:
        """获取最近的信号报告列表"""
        with self._connect() as conn:
            if symbol:
                rows = conn.execute(
                    """SELECT id, timestamp, symbol, total_score, max_score,
                              direction, confidence, signal_strength, alert_type,
                              ai_analysis, email_sent
                       FROM signal_reports WHERE symbol = ?
                       ORDER BY timestamp DESC LIMIT ? OFFSET ?""",
                    (symbol, limit, offset),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT id, timestamp, symbol, total_score, max_score,
                              direction, confidence, signal_strength, alert_type,
                              ai_analysis, email_sent
                       FROM signal_reports
                       ORDER BY timestamp DESC LIMIT ? OFFSET ?""",
                    (limit, offset),
                ).fetchall()
            return [dict(row) for row in rows]

    def get_report_detail(self, report_id: str) -> dict | None:
        """获取单份报告完整详情"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM signal_reports WHERE id = ?", (report_id,)
            ).fetchone()
            if row is None:
                return None
            result = dict(row)
            result["snapshot"] = json.loads(result.pop("snapshot_json"))
            result["scores"] = json.loads(result.pop("scores_json"))
            result["levels"] = json.loads(result.pop("levels_json"))
            suggestion_raw = result.pop("suggestion_json", "")
            result["suggestion"] = json.loads(suggestion_raw) if suggestion_raw else None
            return result

    def get_latest_report(self, symbol: str) -> dict | None:
        """获取指定交易对的最新报告"""
        with self._connect() as conn:
            row = conn.execute(
                """SELECT * FROM signal_reports WHERE symbol = ?
                   ORDER BY timestamp DESC LIMIT 1""",
                (symbol,),
            ).fetchone()
            if row is None:
                return None
            result = dict(row)
            result["snapshot"] = json.loads(result.pop("snapshot_json"))
            result["scores"] = json.loads(result.pop("scores_json"))
            result["levels"] = json.loads(result.pop("levels_json"))
            suggestion_raw = result.pop("suggestion_json", "")
            result["suggestion"] = json.loads(suggestion_raw) if suggestion_raw else None
            return result

    def count_emails_today(self) -> int:
        """统计今日已发送邮件数"""
        today = datetime.now().strftime("%Y-%m-%d")
        with self._connect() as conn:
            row = conn.execute(
                """SELECT COUNT(*) as cnt FROM notification_log
                   WHERE channel = 'email' AND success = 1
                   AND sent_at >= ?""",
                (today,),
            ).fetchone()
            return row["cnt"] if row else 0

    def get_last_signal_time(self, symbol: str, direction: str) -> str | None:
        """获取指定币种和方向的最后一次信号时间，用于去重"""
        with self._connect() as conn:
            row = conn.execute(
                """SELECT timestamp FROM signal_reports
                   WHERE symbol = ? AND direction = ?
                   AND signal_strength IN ('strong', 'moderate')
                   ORDER BY timestamp DESC LIMIT 1""",
                (symbol, direction),
            ).fetchone()
            return row["timestamp"] if row else None

    def get_signal_accuracy_stats(self, symbol: str = "", days: int = 7) -> dict:
        """统计近 N 天的信号准确率（需要后续价格验证）"""
        with self._connect() as conn:
            cutoff = datetime.now().isoformat()  # 简化版先返回基础统计
            query = """
                SELECT signal_strength, direction, COUNT(*) as cnt
                FROM signal_reports
                WHERE timestamp >= date('now', ?)
            """
            params: list = [f"-{days} days"]
            if symbol:
                query += " AND symbol = ?"
                params.append(symbol)
            query += " GROUP BY signal_strength, direction"

            rows = conn.execute(query, params).fetchall()
            return {
                "period_days": days,
                "breakdown": [dict(row) for row in rows],
                "total": sum(row["cnt"] for row in rows),
            }
