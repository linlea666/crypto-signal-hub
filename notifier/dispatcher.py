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
    """默认的纯文本 HTML 渲染（后续由 Web 模块的模板替换）"""
    scores_html = ""
    for fs in report.factor_scores:
        emoji = "📈" if fs.score > 0 else ("📉" if fs.score < 0 else "➖")
        bar_pct = int((abs(fs.score) / fs.max_score) * 100) if fs.max_score else 0
        color = "#22c55e" if fs.score > 0 else ("#ef4444" if fs.score < 0 else "#6b7280")
        scores_html += f"""
        <tr>
            <td style="padding:6px 12px">{fs.name}</td>
            <td style="padding:6px 12px;color:{color};font-weight:bold">{fs.score:+.0f}</td>
            <td style="padding:6px 12px">{emoji}</td>
            <td style="padding:6px 12px;font-size:12px;color:#9ca3af">{fs.details[:60]}</td>
        </tr>"""

    supports_html = ""
    for lv in report.key_levels.supports[:3]:
        supports_html += f"🟢 {lv.price:,.0f} ({lv.source})<br>"
    resistances_html = ""
    for lv in report.key_levels.resistances[:3]:
        resistances_html += f"🔴 {lv.price:,.0f} ({lv.source})<br>"

    direction_color = "#22c55e" if report.direction.value == "bullish" else (
        "#ef4444" if report.direction.value == "bearish" else "#6b7280"
    )

    return f"""
    <div style="font-family:-apple-system,sans-serif;max-width:600px;margin:0 auto;
                background:#1a1a2e;color:#e0e0e0;border-radius:12px;overflow:hidden">
        <div style="background:linear-gradient(135deg,#16213e,#0f3460);padding:20px;text-align:center">
            <h1 style="margin:0;font-size:18px;color:#fff">🔮 CryptoSignal Hub</h1>
            <p style="margin:5px 0 0;color:#94a3b8;font-size:13px">{report.symbol} 市场分析报告</p>
        </div>
        <div style="padding:20px">
            <div style="display:flex;justify-content:space-between;align-items:center;
                        margin-bottom:20px;padding:15px;background:#16213e;border-radius:8px">
                <div>
                    <div style="font-size:24px;font-weight:bold;color:#fff">
                        ${report.snapshot.price.current:,.2f}
                    </div>
                    <div style="color:#94a3b8;font-size:13px">{report.timestamp.strftime('%Y-%m-%d %H:%M')}</div>
                </div>
                <div style="text-align:right">
                    <div style="font-size:20px;font-weight:bold;color:{direction_color}">
                        {report.score_display} {report.direction_label}
                    </div>
                    <div style="color:#94a3b8;font-size:13px">信心度 {report.confidence:.0f}%</div>
                </div>
            </div>

            <h3 style="color:#94a3b8;font-size:14px;margin:15px 0 10px">📊 各维度评分</h3>
            <table style="width:100%;border-collapse:collapse;font-size:13px">
                {scores_html}
            </table>

            <div style="display:flex;gap:15px;margin:20px 0">
                <div style="flex:1;padding:12px;background:#16213e;border-radius:8px">
                    <div style="color:#94a3b8;font-size:12px;margin-bottom:5px">支撑位</div>
                    {supports_html or '<span style="color:#6b7280">暂无</span>'}
                </div>
                <div style="flex:1;padding:12px;background:#16213e;border-radius:8px">
                    <div style="color:#94a3b8;font-size:12px;margin-bottom:5px">阻力位</div>
                    {resistances_html or '<span style="color:#6b7280">暂无</span>'}
                </div>
            </div>

            {"<h3 style='color:#94a3b8;font-size:14px;margin:15px 0 10px'>🤖 AI 分析</h3>"
             "<div style='padding:12px;background:#16213e;border-radius:8px;line-height:1.7;font-size:13px'>"
             + report.ai_analysis.replace(chr(10), '<br>') +
             "</div>" if report.ai_analysis else ""}
        </div>
        <div style="padding:15px 20px;background:#0f0f23;text-align:center;
                    font-size:11px;color:#6b7280">
            CryptoSignal Hub · 数据来源 OKX/Binance/Deribit/Yahoo · 仅供参考，不构成投资建议
        </div>
    </div>
    """
