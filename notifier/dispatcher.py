"""通知分发调度器。

统一管理所有通知渠道的分发逻辑：
1. 渲染报告为 HTML
2. 通过 Throttle 判断是否发送
3. 分发到各启用的渠道
4. 记录发送日志
"""

from __future__ import annotations

import logging

from core.constants import AlertType
from core.interfaces import Notifier
from core.models import SignalReport
from notifier.throttle import NotificationThrottle
from storage.database import Database

logger = logging.getLogger(__name__)


class NotificationDispatcher:
    """通知分发器"""

    def __init__(
        self,
        throttle: NotificationThrottle,
        db: Database,
        render_fn=None,
    ):
        self._throttle = throttle
        self._db = db
        self._channels: list[Notifier] = []
        # 报告渲染函数，延迟注入避免循环依赖
        self._render_fn = render_fn or _default_render

    def register_channel(self, channel: Notifier) -> None:
        self._channels.append(channel)
        logger.info("注册通知渠道: %s (enabled=%s)", channel.name, channel.enabled)

    def update_channel_configs(self, **configs_by_name) -> None:
        """按渠道名更新配置，如 update_channel_configs(email=new_email_cfg)"""
        for channel in self._channels:
            if channel.name in configs_by_name and hasattr(channel, "update_config"):
                channel.update_config(configs_by_name[channel.name])

    async def dispatch(self, report: SignalReport) -> None:
        """决定是否发送并分发到所有渠道"""
        should_send = self._throttle.should_send(report)

        if not should_send:
            logger.debug(
                "信号 %s 未通过限频检查 (强度=%s, 信心度=%.0f%%)",
                report.id[:8], report.signal_strength.value, report.confidence,
            )
            return

        html_content = self._render_fn(report)

        for channel in self._channels:
            if not channel.enabled:
                continue
            try:
                success = await channel.send(report, html_content)
                self._db.log_notification(
                    report_id=report.id,
                    channel=channel.name,
                    success=success,
                )
                if success:
                    self._db.mark_email_sent(report.id)
                    logger.info(
                        "通知已发送 [%s]: %s %s 信心度%.0f%%",
                        channel.name, report.symbol,
                        report.direction_label, report.confidence,
                    )
            except Exception as e:
                logger.error("渠道 %s 发送失败: %s", channel.name, e)
                self._db.log_notification(
                    report_id=report.id,
                    channel=channel.name,
                    success=False,
                    error=str(e),
                )

    async def dispatch_daily_report(self, report: SignalReport) -> None:
        """日报：跳过 throttle 检查（日报是计划任务），直接发送"""
        html_content = self._render_fn(report)
        for channel in self._channels:
            if not channel.enabled:
                continue
            try:
                success = await channel.send(report, html_content)
                self._db.log_notification(
                    report_id=report.id, channel=channel.name, success=success,
                )
                if success:
                    self._db.mark_email_sent(report.id)
            except Exception as e:
                logger.error("日报发送失败 [%s]: %s", channel.name, e)


def _default_render(report: SignalReport) -> str:
    """使用 Jinja2 模板渲染邮件 HTML"""
    from pathlib import Path
    from jinja2 import Environment, FileSystemLoader

    template_dir = Path(__file__).parent.parent / "web" / "templates"
    env = Environment(loader=FileSystemLoader(str(template_dir)))

    try:
        tpl = env.get_template("email_report.html")
        return tpl.render(
            symbol=report.symbol,
            snapshot=report.snapshot,
            direction=report.direction.value,
            direction_label=report.direction_label,
            score_display=report.score_display,
            confidence=report.confidence,
            signal_strength=report.signal_strength.value,
            scores=[
                {"name": fs.name, "score": fs.score, "max_score": fs.max_score, "details": fs.details}
                for fs in report.factor_scores
            ],
            levels=report.key_levels,
            ai_analysis=report.ai_analysis or "",
            timestamp=report.timestamp.strftime("%Y-%m-%d %H:%M"),
        )
    except Exception as e:
        logger.error("邮件模板渲染失败，使用简单格式: %s", e)
        return f"""
        <div style="font-family:sans-serif;padding:20px;background:#111;color:#eee">
            <h2>CryptoSignal Hub - {report.symbol}</h2>
            <p>评分: {report.score_display} | {report.direction_label} | 信心度 {report.confidence:.0f}%</p>
            <p>{report.ai_analysis or ''}</p>
        </div>"""
