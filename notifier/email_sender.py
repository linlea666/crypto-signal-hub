"""邮件发送模块。

使用标准 smtplib 通过 SMTP 发送 HTML 邮件。
支持 163 / QQ / Gmail 等主流邮箱。
"""

from __future__ import annotations

import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from config.schema import EmailConfig
from core.interfaces import Notifier
from core.models import SignalReport

logger = logging.getLogger(__name__)


class EmailNotifier(Notifier):
    """SMTP 邮件通知器"""

    def __init__(self, config: EmailConfig):
        self._config = config

    @property
    def name(self) -> str:
        return "email"

    def update_config(self, config: EmailConfig) -> None:
        """热更新配置"""
        self._config = config

    @property
    def enabled(self) -> bool:
        return (
            self._config.enabled
            and bool(self._config.smtp_user)
            and bool(self._config.smtp_pass)
            and bool(self._config.to)
        )

    async def send(self, report: SignalReport, html_content: str) -> bool:
        """发送分析报告邮件"""
        if not self.enabled:
            logger.debug("邮件未启用或配置不完整，跳过发送")
            return False

        subject = self._build_subject(report)
        return self._send_html_email(subject, html_content)

    async def send_test(self) -> bool:
        """发送测试邮件"""
        html = """
        <div style="font-family: sans-serif; padding: 20px;">
            <h2>🔮 CryptoSignal Hub 测试邮件</h2>
            <p>如果你收到这封邮件，说明邮箱配置正确！</p>
            <p style="color: #888;">此邮件由 CryptoSignal Hub 自动发送</p>
        </div>
        """
        return self._send_html_email("✅ CryptoSignal Hub 邮件测试", html)

    def _build_subject(self, report: SignalReport) -> str:
        """根据信号强度生成邮件标题"""
        emoji_map = {
            "strong": "🔴",
            "moderate": "📊",
            "weak": "📋",
        }
        emoji = emoji_map.get(report.signal_strength.value, "📊")
        direction_text = report.direction_label
        score_text = report.score_display

        if report.signal_strength.value == "strong":
            return f"{emoji} [强信号] {report.symbol} 评分{score_text} {direction_text} | 信心度{report.confidence:.0f}%"
        return f"{emoji} [{report.symbol}] 评分{score_text} {direction_text} | 信心度{report.confidence:.0f}%"

    def _send_html_email(self, subject: str, html_content: str) -> bool:
        """底层 SMTP 发送逻辑"""
        cfg = self._config
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = f"{cfg.from_name} <{cfg.smtp_user}>"
        msg["To"] = ", ".join(cfg.to)
        msg.attach(MIMEText(html_content, "html", "utf-8"))

        try:
            if cfg.use_ssl:
                with smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, timeout=15) as server:
                    server.login(cfg.smtp_user, cfg.smtp_pass)
                    server.sendmail(cfg.smtp_user, cfg.to, msg.as_string())
            else:
                with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=15) as server:
                    server.starttls()
                    server.login(cfg.smtp_user, cfg.smtp_pass)
                    server.sendmail(cfg.smtp_user, cfg.to, msg.as_string())

            logger.info("邮件发送成功: %s -> %s", subject, cfg.to)
            return True

        except smtplib.SMTPAuthenticationError:
            logger.error("邮箱认证失败，请检查 SMTP 用户名和授权码")
            return False
        except smtplib.SMTPException as e:
            logger.error("邮件发送失败: %s", e)
            return False
        except Exception as e:
            logger.error("邮件发送异常: %s", e)
            return False
