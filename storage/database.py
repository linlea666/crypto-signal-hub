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
                    json.dumps(report_data.get("trade") or "", ensure_ascii=False),
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
            result.update(self._parse_suggestion_json(result.pop("suggestion_json", "")))
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
            result.update(self._parse_suggestion_json(result.pop("suggestion_json", "")))
            return result

    @staticmethod
    def _parse_suggestion_json(raw: str) -> dict:
        """解析 suggestion_json，分离出 trade 建议和回测结果。

        suggestion_json 存储格式示例:
        {"direction": "bullish", "entry_low": ..., "4h": {"outcome": ...}, ...}

        返回 {"trade": {...} or None, "backtest_results": {...}}
        """
        if not raw:
            return {"trade": None}
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                return {"trade": None}
        except (json.JSONDecodeError, TypeError):
            return {"trade": None}

        # trade 建议字段标识：含 direction + entry_low
        if "direction" in data and "entry_low" in data:
            return {"trade": data}
        return {"trade": None}

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
        """统计近 N 天的信号分布和准确率"""
        with self._connect() as conn:
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

            accuracy = self._calculate_accuracy(conn, days, symbol)

            return {
                "period_days": days,
                "breakdown": [dict(row) for row in rows],
                "total": sum(row["cnt"] for row in rows),
                "accuracy": accuracy,
            }

    def update_signal_outcome(
        self, report_id: str, window: str,
        outcome: str, price_after: float,
        change_pct: float = 0.0, correct: bool = False,
    ) -> None:
        """记录单窗口回测结果。

        outcome 取值: tp1_hit / tp2_hit / sl_hit / expired / correct_dir / wrong_dir
        保留 correct 布尔值用于兼容统计查询。
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT suggestion_json FROM signal_reports WHERE id = ?",
                (report_id,),
            ).fetchone()
            existing = {}
            if row and row["suggestion_json"]:
                try:
                    existing = json.loads(row["suggestion_json"])
                    if not isinstance(existing, dict):
                        existing = {}
                except (json.JSONDecodeError, TypeError):
                    existing = {}

            existing[window] = {
                "outcome": outcome,
                "price_after": price_after,
                "correct": correct,
                "change_pct": round(change_pct, 3),
            }

            conn.execute(
                "UPDATE signal_reports SET suggestion_json = ? WHERE id = ?",
                (json.dumps(existing, ensure_ascii=False), report_id),
            )

    def get_unverified_signals(self, hours_ago: int = 4, window: str = "4h") -> list[dict]:
        """获取指定窗口下待验证的信号"""
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT id, symbol, direction, total_score, timestamp,
                          snapshot_json, suggestion_json
                   FROM signal_reports
                   WHERE signal_strength IN ('strong', 'moderate')
                   AND timestamp <= datetime('now', ?)
                   AND timestamp >= datetime('now', '-3 days')
                   AND (suggestion_json IS NULL OR suggestion_json = ''
                        OR suggestion_json = '""'
                        OR json_extract(suggestion_json, '$.' || ?) IS NULL)
                   ORDER BY timestamp DESC LIMIT 50""",
                (f"-{hours_ago} hours", window),
            ).fetchall()
            return [dict(row) for row in rows]

    def get_backtest_stats(self, days: int = 7, symbol: str = "") -> dict:
        """获取多窗口回测统计数据"""
        with self._connect() as conn:
            result = {}
            for win in ("4h", "12h", "24h"):
                result[win] = self._calc_window_accuracy(conn, win, days, symbol)
            result["summary"] = self._calculate_accuracy(conn, days, symbol)
            return result

    @staticmethod
    def _calc_window_accuracy(conn, window: str, days: int, symbol: str = "") -> dict:
        query = f"""
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN json_extract(suggestion_json, '$.{window}.correct') = 1 THEN 1 ELSE 0 END) as correct_cnt,
                AVG(CASE WHEN json_extract(suggestion_json, '$.{window}.change_pct') IS NOT NULL
                         THEN json_extract(suggestion_json, '$.{window}.change_pct') END) as avg_change
            FROM signal_reports
            WHERE signal_strength IN ('strong', 'moderate')
            AND timestamp >= date('now', ?)
            AND json_extract(suggestion_json, '$.{window}') IS NOT NULL
        """
        params: list = [f"-{days} days"]
        if symbol:
            query += " AND symbol = ?"
            params.append(symbol)
        row = conn.execute(query, params).fetchone()
        total = row["total"] if row else 0
        correct = row["correct_cnt"] if row else 0
        avg_chg = row["avg_change"] if row else 0
        return {
            "verified": total,
            "correct": correct,
            "rate": round((correct / total * 100), 1) if total > 0 else 0,
            "avg_change_pct": round(avg_chg or 0, 3),
        }

    @staticmethod
    def _calculate_accuracy(conn, days: int, symbol: str = "") -> dict:
        """以最长窗口（24h > 12h > 4h）的结果计算综合准确率"""
        query = """
            SELECT
                COUNT(*) as total,
                SUM(CASE
                    WHEN json_extract(suggestion_json, '$.24h.correct') = 1 THEN 1
                    WHEN json_extract(suggestion_json, '$.24h') IS NULL
                         AND json_extract(suggestion_json, '$.12h.correct') = 1 THEN 1
                    WHEN json_extract(suggestion_json, '$.24h') IS NULL
                         AND json_extract(suggestion_json, '$.12h') IS NULL
                         AND json_extract(suggestion_json, '$.4h.correct') = 1 THEN 1
                    WHEN json_extract(suggestion_json, '$.correct') = 1 THEN 1
                    ELSE 0
                END) as correct_cnt
            FROM signal_reports
            WHERE signal_strength IN ('strong', 'moderate')
            AND timestamp >= date('now', ?)
            AND (json_extract(suggestion_json, '$.4h') IS NOT NULL
                 OR json_extract(suggestion_json, '$.correct') IS NOT NULL)
        """
        params: list = [f"-{days} days"]
        if symbol:
            query += " AND symbol = ?"
            params.append(symbol)

        row = conn.execute(query, params).fetchone()
        total = row["total"] if row else 0
        correct = row["correct_cnt"] if row else 0
        return {
            "verified": total,
            "correct": correct,
            "rate": round((correct / total * 100), 1) if total > 0 else 0,
        }
