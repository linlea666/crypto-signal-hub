"""通知限频与防骚扰控制。

规则：
1. 每日邮件总数限制
2. 同方向重复信号冷却期
3. 静默时段控制（仅影响普通报告，不影响强信号）
4. 强信号不受静默时段限制，始终推送
"""

from __future__ import annotations

import logging
from datetime import timedelta

from config.schema import ScheduleConfig
from core.constants import SignalStrength
from core.models import SignalReport
from core.time_utils import now_beijing
from storage.database import Database

logger = logging.getLogger(__name__)


class NotificationThrottle:
    """通知限频器，决定某条信号是否应该推送"""

    def __init__(self, schedule_config: ScheduleConfig, db: Database):
        self._config = schedule_config
        self._db = db

    def should_send(self, report: SignalReport) -> bool:
        """判断该报告是否应该发送邮件通知。

        Returns:
            True = 应该发送, False = 应该跳过
        """
        # 强信号始终发送，不受任何限制
        if report.signal_strength == SignalStrength.STRONG:
            if self._is_daily_limit_reached():
                logger.warning("强信号但已达每日上限，仍然发送（强信号优先）")
            return True

        # 弱信号不推送，仅存档
        if report.signal_strength == SignalStrength.WEAK:
            logger.debug("弱信号，跳过推送")
            return False

        # 以下为中等信号的检查 ──

        # 检查静默时段
        if self._is_quiet_hours():
            logger.debug("静默时段，跳过普通信号推送")
            return False

        # 检查每日发送上限
        if self._is_daily_limit_reached():
            logger.info("已达每日邮件上限 %d 封", self._config.max_daily_emails)
            return False

        # 检查重复信号冷却
        if self._is_duplicate_signal(report):
            logger.debug("同方向信号冷却中，跳过")
            return False

        return True

    def _is_quiet_hours(self) -> bool:
        """是否处于静默时段"""
        now = now_beijing()
        try:
            start_parts = self._config.quiet_hours_start.split(":")
            end_parts = self._config.quiet_hours_end.split(":")
            start_h, start_m = int(start_parts[0]), int(start_parts[1])
            end_h, end_m = int(end_parts[0]), int(end_parts[1])

            current_minutes = now.hour * 60 + now.minute
            start_minutes = start_h * 60 + start_m
            end_minutes = end_h * 60 + end_m

            # 处理跨午夜的情况（如 23:00 ~ 07:00）
            if start_minutes <= end_minutes:
                return start_minutes <= current_minutes < end_minutes
            else:
                return current_minutes >= start_minutes or current_minutes < end_minutes

        except (ValueError, IndexError):
            return False

    def _is_daily_limit_reached(self) -> bool:
        count = self._db.count_emails_today()
        return count >= self._config.max_daily_emails

    def _is_duplicate_signal(self, report: SignalReport) -> bool:
        """检查近期是否已推送过同方向信号"""
        last_time_str = self._db.get_last_signal_time(
            report.symbol, report.direction.value
        )
        if not last_time_str:
            return False

        try:
            last_time = datetime.fromisoformat(last_time_str)
            cooldown = timedelta(hours=self._config.duplicate_signal_cooldown_hours)
            return now_beijing() - last_time < cooldown
        except (ValueError, TypeError):
            return False
