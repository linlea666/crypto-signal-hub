"""服务健康检查系统。

定期探测所有外部依赖（交易所 API、宏观数据源、AI 服务、邮件服务）的可用性，
汇总为结构化的健康报告，供大屏展示和告警使用。

每个探针独立执行，单个探针超时不阻塞其他探针。
"""

from __future__ import annotations

import asyncio
import logging
import smtplib
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

import ccxt.async_support as ccxt
import httpx

from config.schema import AIConfig, EmailConfig, ExchangeConfig

logger = logging.getLogger(__name__)


class HealthStatus(str, Enum):
    OK = "ok"
    DEGRADED = "degraded"  # 响应慢但可用
    ERROR = "error"
    UNKNOWN = "unknown"


@dataclass
class ProbeResult:
    """单个探针的检测结果"""
    name: str
    status: HealthStatus
    latency_ms: float = 0.0
    message: str = ""
    last_check: str = ""

    @property
    def status_emoji(self) -> str:
        return {"ok": "🟢", "degraded": "🟡", "error": "🔴", "unknown": "⚪"}.get(
            self.status.value, "⚪"
        )


@dataclass
class HealthReport:
    """系统整体健康报告"""
    overall: HealthStatus = HealthStatus.UNKNOWN
    probes: list[ProbeResult] = field(default_factory=list)
    checked_at: str = ""

    @property
    def ok_count(self) -> int:
        return sum(1 for p in self.probes if p.status == HealthStatus.OK)

    @property
    def total_count(self) -> int:
        return len(self.probes)


# 探针超时阈值（秒）
PROBE_TIMEOUT = 10
# 慢响应阈值（毫秒），超过则标记为 degraded
SLOW_THRESHOLD_MS = 3000


class HealthChecker:
    """健康检查器，管理所有探针并汇总结果"""

    def __init__(
        self,
        exchange_config: ExchangeConfig,
        email_config: EmailConfig,
        ai_config: AIConfig,
    ):
        self._exchange_config = exchange_config
        self._email_config = email_config
        self._ai_config = ai_config
        self._last_report: HealthReport | None = None

    @property
    def last_report(self) -> HealthReport | None:
        return self._last_report

    async def check_all(self) -> HealthReport:
        """并行执行所有探针，汇总健康报告"""
        probes = await asyncio.gather(
            self._probe_exchange(self._exchange_config.primary, "交易所-主"),
            self._probe_exchange(self._exchange_config.secondary, "交易所-辅"),
            self._probe_exchange(self._exchange_config.options_source, "期权数据源"),
            self._probe_yfinance(),
            self._probe_fear_greed(),
            self._probe_ai(),
            self._probe_smtp(),
            return_exceptions=True,
        )

        results: list[ProbeResult] = []
        for p in probes:
            if isinstance(p, ProbeResult):
                results.append(p)
            elif isinstance(p, Exception):
                results.append(ProbeResult(
                    name="未知", status=HealthStatus.ERROR,
                    message=str(p), last_check=datetime.now().isoformat(),
                ))

        overall = self._determine_overall(results)
        report = HealthReport(
            overall=overall,
            probes=results,
            checked_at=datetime.now().isoformat(),
        )
        self._last_report = report
        return report

    @staticmethod
    def _determine_overall(probes: list[ProbeResult]) -> HealthStatus:
        if not probes:
            return HealthStatus.UNKNOWN
        error_count = sum(1 for p in probes if p.status == HealthStatus.ERROR)
        degraded_count = sum(1 for p in probes if p.status == HealthStatus.DEGRADED)
        if error_count > len(probes) // 2:
            return HealthStatus.ERROR
        if error_count > 0 or degraded_count > 0:
            return HealthStatus.DEGRADED
        return HealthStatus.OK

    # ── 各探针实现 ──

    async def _probe_exchange(self, exchange_id: str, label: str) -> ProbeResult:
        """探测交易所 API 可用性（获取 BTC ticker）"""
        now = datetime.now().isoformat()
        try:
            exchange_class = getattr(ccxt, exchange_id, None)
            if not exchange_class:
                return ProbeResult(
                    name=label, status=HealthStatus.ERROR,
                    message=f"不支持的交易所: {exchange_id}", last_check=now,
                )
            ex = exchange_class({"enableRateLimit": True, "timeout": PROBE_TIMEOUT * 1000})
            start = time.monotonic()
            await ex.fetch_ticker("BTC/USDT")
            latency = (time.monotonic() - start) * 1000
            await ex.close()

            status = HealthStatus.OK if latency < SLOW_THRESHOLD_MS else HealthStatus.DEGRADED
            return ProbeResult(
                name=f"{label} ({exchange_id})",
                status=status,
                latency_ms=round(latency, 0),
                message=f"响应 {latency:.0f}ms",
                last_check=now,
            )
        except Exception as e:
            return ProbeResult(
                name=f"{label} ({exchange_id})",
                status=HealthStatus.ERROR,
                message=str(e)[:100],
                last_check=now,
            )

    async def _probe_yfinance(self) -> ProbeResult:
        """探测 yfinance（美股数据）可用性"""
        now = datetime.now().isoformat()
        try:
            start = time.monotonic()
            async with httpx.AsyncClient(timeout=PROBE_TIMEOUT) as client:
                resp = await client.get(
                    "https://query1.finance.yahoo.com/v8/finance/chart/%5EIXIC?range=1d&interval=1d"
                )
                resp.raise_for_status()
            latency = (time.monotonic() - start) * 1000
            status = HealthStatus.OK if latency < SLOW_THRESHOLD_MS else HealthStatus.DEGRADED
            return ProbeResult(
                name="美股数据 (Yahoo)",
                status=status,
                latency_ms=round(latency, 0),
                message=f"响应 {latency:.0f}ms",
                last_check=now,
            )
        except Exception as e:
            return ProbeResult(
                name="美股数据 (Yahoo)",
                status=HealthStatus.ERROR,
                message=str(e)[:100],
                last_check=now,
            )

    async def _probe_fear_greed(self) -> ProbeResult:
        """探测恐惧贪婪指数 API"""
        now = datetime.now().isoformat()
        try:
            start = time.monotonic()
            async with httpx.AsyncClient(timeout=PROBE_TIMEOUT) as client:
                resp = await client.get("https://api.alternative.me/fng/?limit=1")
                resp.raise_for_status()
                data = resp.json()
            latency = (time.monotonic() - start) * 1000
            value = data.get("data", [{}])[0].get("value", "?")
            return ProbeResult(
                name="恐惧贪婪指数",
                status=HealthStatus.OK if latency < SLOW_THRESHOLD_MS else HealthStatus.DEGRADED,
                latency_ms=round(latency, 0),
                message=f"当前值={value}, {latency:.0f}ms",
                last_check=now,
            )
        except Exception as e:
            return ProbeResult(
                name="恐惧贪婪指数",
                status=HealthStatus.ERROR,
                message=str(e)[:100],
                last_check=now,
            )

    async def _probe_ai(self) -> ProbeResult:
        """探测 AI 服务可用性"""
        now = datetime.now().isoformat()
        if not self._ai_config.enabled or not self._ai_config.api_key:
            return ProbeResult(
                name="AI 分析",
                status=HealthStatus.UNKNOWN,
                message="未配置",
                last_check=now,
            )
        try:
            from openai import AsyncOpenAI
            start = time.monotonic()
            client = AsyncOpenAI(
                api_key=self._ai_config.api_key,
                base_url=self._ai_config.base_url,
                timeout=PROBE_TIMEOUT,
            )
            resp = await client.chat.completions.create(
                model=self._ai_config.model,
                messages=[{"role": "user", "content": "ping"}],
                max_tokens=5,
            )
            latency = (time.monotonic() - start) * 1000
            return ProbeResult(
                name=f"AI ({self._ai_config.provider})",
                status=HealthStatus.OK if latency < 5000 else HealthStatus.DEGRADED,
                latency_ms=round(latency, 0),
                message=f"模型 {self._ai_config.model}, {latency:.0f}ms",
                last_check=now,
            )
        except Exception as e:
            return ProbeResult(
                name=f"AI ({self._ai_config.provider})",
                status=HealthStatus.ERROR,
                message=str(e)[:100],
                last_check=now,
            )

    async def _probe_smtp(self) -> ProbeResult:
        """探测邮件 SMTP 服务可用性（仅连接测试，不发送邮件）"""
        now = datetime.now().isoformat()
        cfg = self._email_config
        if not cfg.enabled or not cfg.smtp_user:
            return ProbeResult(
                name="邮件推送",
                status=HealthStatus.UNKNOWN,
                message="未配置",
                last_check=now,
            )
        try:
            start = time.monotonic()
            if cfg.use_ssl:
                server = smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, timeout=PROBE_TIMEOUT)
            else:
                server = smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=PROBE_TIMEOUT)
                server.starttls()
            server.login(cfg.smtp_user, cfg.smtp_pass)
            server.quit()
            latency = (time.monotonic() - start) * 1000
            return ProbeResult(
                name=f"邮件 ({cfg.smtp_host})",
                status=HealthStatus.OK,
                latency_ms=round(latency, 0),
                message=f"认证成功, {latency:.0f}ms",
                last_check=now,
            )
        except smtplib.SMTPAuthenticationError:
            return ProbeResult(
                name=f"邮件 ({cfg.smtp_host})",
                status=HealthStatus.ERROR,
                message="认证失败，请检查授权码",
                last_check=now,
            )
        except Exception as e:
            return ProbeResult(
                name=f"邮件 ({cfg.smtp_host})",
                status=HealthStatus.ERROR,
                message=str(e)[:100],
                last_check=now,
            )
